"""Fetch an iCalendar source and expand it into concrete event instances.

Pure functions, no RPyC: backend.py drives them, and tests/test_ics_source.py exercises them
directly. Requires `icalendar`, `recurring-ical-events` and `requests` (backend venv only).
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta, timezone

import icalendar
import recurring_ical_events
import requests

# Make `internal.events` importable whether we're run from the plugin dir (backend.py adds it
# to sys.path) or from the repo root by the test runner.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from internal.events import (  # noqa: E402
    STATUS_CONFIRMED,
    CalendarEvent,
    as_utc,
    extract_meeting_link,
)
# Re-exported: callers (and tests) have always imported SourceError from this module.
from backend.source_errors import SourceError  # noqa: E402,F401

DEFAULT_TIMEOUT = 20
USER_AGENT = "StreamController-CalendarInfo/0.1 (+https://github.com/red-tux/calendar_info)"


def normalize_source(source: str) -> str:
    source = (source or "").strip()
    if source.lower().startswith("webcal://"):
        return "https://" + source[len("webcal://"):]
    return source


def fetch_ics(source: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Raw iCalendar text from a URL (http/https/webcal), a file:// URL, or a local path."""
    source = normalize_source(source)
    if not source:
        raise SourceError("No calendar URL or file path configured")

    lowered = source.lower()
    if lowered.startswith(("http://", "https://")):
        try:
            response = requests.get(source, timeout=timeout, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
        except requests.RequestException as e:
            raise SourceError(_describe_request_error(e)) from e
        return response.text

    path = source[len("file://"):] if lowered.startswith("file://") else source
    path = os.path.expanduser(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        raise SourceError(f"Cannot read {path}: {e.strerror or e}") from e


def _describe_request_error(e: requests.RequestException) -> str:
    response = getattr(e, "response", None)
    if response is not None:
        if response.status_code in (401, 403):
            return f"HTTP {response.status_code}: the calendar address is not accessible (private address expired or wrong?)"
        if response.status_code == 404:
            return "HTTP 404: calendar not found at that address"
        return f"HTTP {response.status_code} from calendar server"
    if isinstance(e, requests.Timeout):
        return "Timed out fetching the calendar"
    if isinstance(e, requests.ConnectionError):
        return "Could not connect to the calendar server"
    return str(e) or e.__class__.__name__


def _to_text(value) -> str:
    if value is None:
        return ""
    return str(value)


def _coerce_datetime(value, all_day: bool) -> datetime:
    """icalendar hands back `date` for all-day events and `datetime` (naive or aware) for timed
    ones. All-day dates become naive midnight datetimes (from_dict pins them to local midnight
    on the foreground); timed values become aware UTC."""
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time(0))
    raise SourceError(f"Unexpected date value {value!r}")


def _event_bounds(component) -> tuple[datetime, datetime, bool]:
    dtstart = component.decoded("DTSTART", None)
    if dtstart is None:
        raise SourceError("VEVENT without DTSTART")
    all_day = not isinstance(dtstart, datetime)

    dtend = component.decoded("DTEND", None)
    if dtend is None:
        duration = component.decoded("DURATION", None)
        if isinstance(duration, timedelta):
            dtend = dtstart + duration
        elif all_day:
            dtend = dtstart + timedelta(days=1)   # RFC 5545 3.6.1: date-only DTSTART = one day
        else:
            dtend = dtstart                        # zero-length instant

    start = _coerce_datetime(dtstart, all_day)
    end = _coerce_datetime(dtend, all_day)
    if end < start:
        end = start
    return start, end, all_day


def _event_tzid(component, dtstart) -> str:
    """The zone the event was written in: the DTSTART TZID parameter when there is one, else
    whatever tzinfo icalendar attached (a ZoneInfo stringifies to its IANA name). A UTC
    (`...Z`) DTSTART reports "UTC"; a floating time reports nothing.
    """
    prop = component.get("DTSTART")
    tzid = ""
    params = getattr(prop, "params", None)
    if params is not None:
        tzid = str(params.get("TZID") or "").strip()
    if not tzid and isinstance(dtstart, datetime) and dtstart.tzinfo is not None:
        key = getattr(dtstart.tzinfo, "key", None)        # zoneinfo
        tzid = str(key or dtstart.tzinfo)
    return "UTC" if tzid.upper() in ("UTC", "Z", "GMT") else tzid


def _conference_url(component) -> str | None:
    # RFC 7986 CONFERENCE (may repeat) and Google's X-GOOGLE-CONFERENCE come before the
    # free-text fields; they're the authoritative "join" address when present.
    for key in ("CONFERENCE", "X-GOOGLE-CONFERENCE"):
        value = component.get(key)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            text = _to_text(v).strip()
            if text.lower().startswith(("http://", "https://")):
                return text
    return None


def expand_events(ics_text: str, calendar_id: str, window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
    """Every event instance (recurrences expanded) overlapping [window_start, window_end).

    window_start/window_end must be aware datetimes. Cancelled instances are included with
    status CANCELLED so the foreground can decide; instances without a UID are skipped.
    """
    try:
        calendar = icalendar.Calendar.from_ical(ics_text)
    except Exception as e:  # icalendar raises a mix of ValueError subclasses
        raise SourceError(f"Not a valid iCalendar file: {e}") from e

    try:
        query = recurring_ical_events.of(calendar, components=["VEVENT"])
        occurrences = query.between(window_start, window_end)
    except Exception as e:
        raise SourceError(f"Could not expand recurring events: {e}") from e

    events: list[CalendarEvent] = []
    seen: set[str] = set()
    for component in occurrences:
        series_uid = _to_text(component.get("UID")).strip()
        if not series_uid:
            continue
        try:
            start, end, all_day = _event_bounds(component)
        except SourceError:
            continue
        tzid = "" if all_day else _event_tzid(component, component.decoded("DTSTART", None))

        instance_uid = f"{series_uid}::{start.date().isoformat() if all_day else as_utc(start).isoformat()}"
        if instance_uid in seen:
            continue
        seen.add(instance_uid)

        location = _to_text(component.get("LOCATION")).strip()
        description = _to_text(component.get("DESCRIPTION")).strip()
        url_prop = _to_text(component.get("URL")).strip() or None
        status = (_to_text(component.get("STATUS")).strip().upper() or STATUS_CONFIRMED)

        event = CalendarEvent(
            uid=instance_uid,
            series_uid=series_uid,
            calendar_id=calendar_id,
            title=_to_text(component.get("SUMMARY")).strip() or "(no title)",
            start=start if not all_day else start.replace(tzinfo=timezone.utc),
            end=end if not all_day else end.replace(tzinfo=timezone.utc),
            all_day=all_day,
            location=location,
            description=description,
            url=url_prop,
            meeting_link=extract_meeting_link(_conference_url(component), location, description, url_prop),
            status=status,
            tzid=tzid,
        )
        events.append(event)

    events.sort(key=lambda e: (e.start, e.end, e.title))
    return events


def load_source(source: str, calendar_id: str, window_start: datetime, window_end: datetime, timeout: float = DEFAULT_TIMEOUT) -> list[CalendarEvent]:
    return expand_events(fetch_ics(source, timeout=timeout), calendar_id, window_start, window_end)
