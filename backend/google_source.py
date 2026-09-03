"""Google Calendar API source: token storage, calendar listing and event fetching.

Backend-only (needs `requests`). The API expands recurrences server-side with
`singleEvents=true`, so unlike the .ics path there is no local recurrence engine involved -
`map_event()` turns one API item into the same `CalendarEvent` every other source produces,
which is why nothing downstream (event_store, the actions, rendering) knows the difference.

Tokens live in one JSON file per linked account under <plugin>/credentials/, mode 0600, never
in the plugin settings JSON (that file is rewritten wholesale by the settings UI).
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone

import requests

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from backend.google_oauth import AuthFlowError, refresh_access_token, revoke  # noqa: E402
from backend.source_errors import AuthError, SourceError  # noqa: E402
from internal.events import (  # noqa: E402
    STATUS_CONFIRMED,
    CalendarEvent,
    as_utc,
    extract_meeting_link,
)

API_BASE = "https://www.googleapis.com/calendar/v3"
DEFAULT_TIMEOUT = 20
MAX_RESULTS = 250
MAX_PAGES = 10
_EXPIRY_SKEW_SECONDS = 60

_STATUS_MAP = {"confirmed": "CONFIRMED", "tentative": "TENTATIVE", "cancelled": "CANCELLED"}


class TokenStore:
    """One JSON file per linked account: {refresh_token, access_token, expires_at, email}."""

    def __init__(self, credentials_dir: str):
        self.credentials_dir = credentials_dir

    def path_for(self, account_id: str) -> str:
        safe = "".join(c for c in account_id if c.isalnum() or c in "-_")
        return os.path.join(self.credentials_dir, f"google-{safe}.json")

    def load(self, account_id: str) -> dict:
        try:
            with open(self.path_for(account_id), "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise AuthError("This Google account is not connected. Connect it in the plugin settings.") from None
        except (OSError, ValueError) as e:
            raise AuthError(f"Stored Google credentials are unreadable: {e}") from e
        if not data.get("refresh_token"):
            raise AuthError("Stored Google credentials have no refresh token. Reconnect the account.")
        return data

    def save(self, account_id: str, data: dict) -> None:
        path = self.path_for(account_id)
        os.makedirs(self.credentials_dir, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        # Create the file private from the start - never widen it after the token is inside.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def delete(self, account_id: str) -> None:
        try:
            os.remove(self.path_for(account_id))
        except OSError:
            pass


@dataclass
class GoogleClient:
    """Authorized access to one linked account. Refreshes the access token as needed."""
    client_id: str
    client_secret: str
    account_id: str
    store: TokenStore

    def _access_token(self, force_refresh: bool = False) -> str:
        data = self.store.load(self.account_id)
        token = data.get("access_token") or ""
        expires_at = float(data.get("expires_at") or 0)
        if token and not force_refresh and time.time() < expires_at - _EXPIRY_SKEW_SECONDS:
            return token
        try:
            fresh = refresh_access_token(self.client_id, self.client_secret, data["refresh_token"])
        except AuthFlowError as e:
            raise AuthError(str(e)) from e
        data["access_token"] = fresh.get("access_token", "")
        data["expires_at"] = time.time() + float(fresh.get("expires_in") or 3600)
        # A rotated refresh token is only sent sometimes; keep the old one otherwise.
        if fresh.get("refresh_token"):
            data["refresh_token"] = fresh["refresh_token"]
        self.store.save(self.account_id, data)
        return data["access_token"]

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API_BASE}{path}"
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._access_token(force_refresh=attempt == 1)}"}
            try:
                response = requests.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as e:
                raise SourceError(f"Could not reach the Google Calendar API: {e}") from e
            # One retry with a forced refresh: the cached token can be revoked server-side
            # (password change, "remove access") while its expiry still looks fine.
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code != 200:
                raise _api_error(response)
            try:
                return response.json()
            except ValueError as e:
                raise SourceError(f"Google Calendar API returned unparsable JSON: {e}") from e
        raise AuthError("Google rejected the stored credentials. Reconnect the account.")

    def account_email(self) -> str:
        """The primary calendar's id is the account's own address - which is why linking an
        account needs no extra profile/email scope on top of calendar.readonly."""
        return str(self._get("/users/me/calendarList/primary").get("id") or "")

    def list_calendars(self) -> list[dict]:
        items: list[dict] = []
        page_token = None
        for _ in range(MAX_PAGES):
            params = {"maxResults": MAX_RESULTS, "showHidden": False}
            if page_token:
                params["pageToken"] = page_token
            payload = self._get("/users/me/calendarList", params)
            for item in payload.get("items", []):
                items.append({
                    "id": item.get("id", ""),
                    "name": item.get("summaryOverride") or item.get("summary") or item.get("id", ""),
                    "primary": bool(item.get("primary")),
                    "color": item.get("backgroundColor") or "",
                    "access_role": item.get("accessRole") or "",
                })
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        items.sort(key=lambda c: (not c["primary"], c["name"].lower()))
        return items

    def fetch_events(self, google_calendar_id: str, calendar_key: str,
                     window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        if not google_calendar_id:
            raise SourceError("No Google calendar selected")
        events: list[CalendarEvent] = []
        page_token = None
        path = f"/calendars/{requests.utils.quote(google_calendar_id, safe='')}/events"
        for _ in range(MAX_PAGES):
            params = {
                "timeMin": as_utc(window_start).isoformat().replace("+00:00", "Z"),
                "timeMax": as_utc(window_end).isoformat().replace("+00:00", "Z"),
                "singleEvents": "true",       # expand recurrences server-side
                "orderBy": "startTime",
                "showDeleted": "false",
                "maxResults": MAX_RESULTS,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(path, params)
            for item in payload.get("items", []):
                try:
                    events.append(map_event(item, calendar_key))
                except SourceError:
                    continue                  # one malformed item must not lose the calendar
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return events


def _api_error(response) -> SourceError:
    """Turn an API error body into something that names the fix.

    Google's 403s are the interesting ones: `accessNotConfigured` means the Calendar API was
    never enabled on the project, and the body carries the exact console URL to enable it.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    message = str(error.get("message") or "").strip()
    details = error.get("errors") or []
    reason = ""
    help_url = ""
    if details and isinstance(details[0], dict):
        reason = str(details[0].get("reason") or "")
        help_url = str(details[0].get("extendedHelp") or "")

    if response.status_code == 401:
        return AuthError("Google rejected the stored credentials. Reconnect the account in the plugin settings.")
    if reason == "accessNotConfigured":
        where = f" Enable it here: {help_url}" if help_url else ""
        return SourceError(f"The Google Calendar API is not enabled on this Cloud project.{where}")
    if reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
        return SourceError("Google is rate limiting this client; the next refresh will retry.")
    if response.status_code == 404:
        return SourceError("That calendar no longer exists on this account.")
    if response.status_code == 403:
        return SourceError(f"Google denied the request: {message or 'forbidden'}")
    return SourceError(f"Google Calendar API error {response.status_code}: {message or 'unknown'}")


def _parse_endpoint(value: dict | None) -> tuple[datetime, bool]:
    """A Google start/end object: {"date": "2026-09-02"} or {"dateTime": "...", "timeZone": ...}.

    All-day dates become naive midnight (the foreground pins them to *local* midnight in
    CalendarEvent.from_dict); timed values become aware UTC. Same contract as ics_source.
    """
    if not isinstance(value, dict):
        raise SourceError("Event without start/end")
    if value.get("date"):
        try:
            return datetime.combine(date.fromisoformat(value["date"]), dtime(0)), True
        except ValueError as e:
            raise SourceError(f"Unparsable all-day date {value['date']!r}") from e
    raw = value.get("dateTime")
    if not raw:
        raise SourceError("Event without start/end")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as e:
        raise SourceError(f"Unparsable dateTime {raw!r}") from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return as_utc(parsed), False


def _conference_link(item: dict) -> str | None:
    if item.get("hangoutLink"):
        return str(item["hangoutLink"])
    conference = item.get("conferenceData") or {}
    for entry in conference.get("entryPoints") or []:
        if isinstance(entry, dict) and entry.get("entryPointType") == "video" and entry.get("uri"):
            return str(entry["uri"])
    return None


def map_event(item: dict, calendar_key: str) -> CalendarEvent:
    """One `events.list` item (with singleEvents=true) as a CalendarEvent.

    `calendar_key` is our own configured-calendar id, not Google's - the rest of the plugin
    keys colors, names and per-calendar status off it.
    """
    start, all_day = _parse_endpoint(item.get("start"))
    end, end_all_day = _parse_endpoint(item.get("end"))
    if all_day != end_all_day:
        # Google never mixes the two, but comparing a naive all-day bound with an aware timed
        # one would raise - collapse to the all-day form rather than take down the whole poll.
        all_day = True
        start = datetime.combine(start.date(), dtime(0))
        end = datetime.combine(end.date(), dtime(0))
    if end < start:
        end = start

    location = str(item.get("location") or "")
    description = str(item.get("description") or "")
    # conferenceData/hangoutLink is authoritative, so it is checked before the text fields
    # the .ics path has to guess from.
    meeting_link = _conference_link(item) or extract_meeting_link(location, description)

    # Google puts the event's own zone on the start object; all-day events have none.
    start_field = item.get("start") if isinstance(item.get("start"), dict) else {}
    tzid = "" if all_day else str(start_field.get("timeZone") or "")

    event_id = str(item.get("id") or "")
    return CalendarEvent(
        uid=event_id,
        # Recurring instances share recurringEventId; one-off events are their own series.
        series_uid=str(item.get("recurringEventId") or event_id),
        calendar_id=calendar_key,
        title=str(item.get("summary") or "(no title)"),
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        description=description,
        url=str(item.get("htmlLink") or "") or None,
        meeting_link=meeting_link,
        status=_STATUS_MAP.get(str(item.get("status") or "").lower(), STATUS_CONFIRMED),
        tzid=tzid,
    )


def disconnect(client_id: str, client_secret: str, account_id: str, store: TokenStore) -> None:
    """Revoke what we can, then drop the stored token either way."""
    try:
        data = store.load(account_id)
    except AuthError:
        data = {}
    token = data.get("refresh_token") or data.get("access_token")
    if token:
        revoke(token)
    store.delete(account_id)
