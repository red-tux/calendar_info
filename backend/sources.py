"""Calendar sources: where events come from, keyed by a calendar entry's `type`.

Each source turns one configured calendar into `CalendarEvent`s and keeps a last-good copy
under the cache dir so a network blip doesn't blank the deck. Authentication is not a source's
concern: it asks the calendar's account provider (see accounts/) for a credential.

Adding one: a class here (or a module it wraps), one entry in SOURCES. The poll loop, the
event model and the RPyC surface stay as they are.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger as log

from backend.accounts.base import AccountProvider
from backend.accounts.registry import DEFAULT_PROVIDER
from backend.google_source import GoogleClient
from backend.ics_source import expand_events, fetch_ics
from backend.source_errors import AuthError, SourceError
from internal.events import CalendarEvent

DEFAULT_SOURCE = "ics"


@dataclass
class SourceContext:
    """What a source needs besides the calendar entry: the providers, and where to cache."""
    providers: dict[str, AccountProvider] = field(default_factory=dict)
    cache_dir: str | None = None

    def provider_for(self, calendar: dict) -> AccountProvider:
        name = str(calendar.get("account_provider") or DEFAULT_PROVIDER)
        provider = self.providers.get(name)
        if provider is None:
            raise AuthError(f"Unknown account provider {name!r}")
        return provider


class CalendarSource:
    type_id = ""

    def fetch(self, calendar: dict, window_start: datetime, window_end: datetime,
              ctx: SourceContext) -> list[CalendarEvent]:
        raise NotImplementedError

    def load_cached(self, calendar: dict, window_start: datetime, window_end: datetime,
                    ctx: SourceContext) -> list[CalendarEvent]:
        return []

    def list_calendars(self, provider: AccountProvider, account_id: str) -> list[dict]:
        """The calendars an account exposes, for the picker. Not every source has such a thing."""
        raise SourceError(f"{self.type_id} calendars are addressed directly; there is nothing to list")


class IcsSource(CalendarSource):
    type_id = "ics"

    def fetch(self, calendar, window_start, window_end, ctx):
        calendar_id = str(calendar.get("id") or "")
        text = fetch_ics(calendar.get("source") or "")
        events = expand_events(text, calendar_id, window_start, window_end)
        _write_text_cache(ctx.cache_dir, calendar_id, text)
        return events

    def load_cached(self, calendar, window_start, window_end, ctx):
        calendar_id = str(calendar.get("id") or "")
        path = _cache_path(ctx.cache_dir, calendar_id, ".ics")
        if path is None or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return expand_events(f.read(), calendar_id, window_start, window_end)
        except (OSError, SourceError) as e:
            log.warning(f"Could not use cached calendar {path}: {e}")
            return []


class GoogleSource(CalendarSource):
    """Google Calendar API. Which account provider hands over the token is the calendar's
    `account_provider`; the fetch is the same either way."""
    type_id = "google"

    def fetch(self, calendar, window_start, window_end, ctx):
        calendar_id = str(calendar.get("id") or "")
        client = GoogleClient(ctx.provider_for(calendar), str(calendar.get("account_id") or ""))
        events = client.fetch_events(str(calendar.get("google_calendar") or ""), calendar_id,
                                     window_start, window_end)
        _write_json_cache(ctx.cache_dir, calendar_id, events)
        return events

    def load_cached(self, calendar, window_start, window_end, ctx):
        return _read_json_cache(ctx.cache_dir, str(calendar.get("id") or ""), window_start, window_end)

    def list_calendars(self, provider, account_id):
        return GoogleClient(provider, account_id).list_calendars()


SOURCES: dict[str, CalendarSource] = {source.type_id: source for source in (IcsSource(), GoogleSource())}


def source_for(calendar: dict) -> CalendarSource:
    name = str(calendar.get("type") or DEFAULT_SOURCE)
    source = SOURCES.get(name)
    if source is None:
        raise SourceError(f"Unknown calendar type {name!r}")
    return source


# --- last-good-copy cache -------------------------------------------------------------------

def _cache_path(cache_dir: str | None, calendar_id: str, suffix: str) -> str | None:
    if not cache_dir:
        return None
    digest = hashlib.sha1(calendar_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}{suffix}")


def _write_text_cache(cache_dir: str | None, calendar_id: str, text: str) -> None:
    path = _cache_path(cache_dir, calendar_id, ".ics")
    if path is None:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f"Could not write calendar cache {path}: {e}")


def _write_json_cache(cache_dir: str | None, calendar_id: str, events: list[CalendarEvent]) -> None:
    """For sources with no raw document to keep, the mapped events are the cache."""
    path = _cache_path(cache_dir, calendar_id, ".json")
    if path is None:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([e.to_dict() for e in events], f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f"Could not write calendar cache {path}: {e}")


def _read_json_cache(cache_dir: str | None, calendar_id: str,
                     window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
    path = _cache_path(cache_dir, calendar_id, ".json")
    if path is None or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        log.warning(f"Could not use cached calendar {path}: {e}")
        return []
    events = []
    for item in raw:
        try:
            event = CalendarEvent.from_dict(item)
        except (KeyError, ValueError):
            continue
        # The cache outlives the window it was written for; re-filter to the current one.
        if event.end > window_start and event.start < window_end:
            events.append(event)
    return events
