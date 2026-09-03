"""Calendar event model shared by the backend process and the foreground plugin.

Deliberately free of GTK/GLib/StreamController imports: the backend runs this in its own
venv (see backend/backend.py) and the unit tests run it with nothing but the stdlib.

Wire format (what the backend sends the foreground, JSON-encoded): a list of dicts as
produced by `CalendarEvent.to_dict()`. Timed events carry ISO-8601 UTC timestamps; all-day
events carry plain ISO dates (they have no timezone - "Sept 2nd" is the same day everywhere),
and the foreground pins them to local midnight in `CalendarEvent.from_dict()`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Display timezone modes for the `display_timezone` plugin option. Anything else is taken as
# an IANA zone name ("America/New_York").
TZ_LOCAL = "local"
TZ_EVENT = "event"
TZ_UTC = "utc"

STATUS_CONFIRMED = "CONFIRMED"
STATUS_TENTATIVE = "TENTATIVE"
STATUS_CANCELLED = "CANCELLED"

# Meeting-link providers, most specific first. Each pattern must match the full URL we want
# to open. Anything not matched here falls back to the first http(s) URL found at all.
_MEETING_PATTERNS = [
    re.compile(r"https?://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(?:\?[^\s<>\"']*)?", re.I),
    re.compile(r"https?://[\w.-]*zoom\.(?:us|com)/(?:j|my|s|w)/[^\s<>\"']+", re.I),
    re.compile(r"https?://teams\.(?:microsoft|live)\.com/(?:l/meetup-join|meet)/[^\s<>\"']+", re.I),
    re.compile(r"https?://[\w.-]*webex\.com/[^\s<>\"']+", re.I),
    re.compile(r"https?://[\w.-]*whereby\.com/[^\s<>\"']+", re.I),
    re.compile(r"https?://[\w.-]*gotomeeting\.com/[^\s<>\"']+", re.I),
    re.compile(r"https?://[\w.-]*bluejeans\.com/[^\s<>\"']+", re.I),
    re.compile(r"https?://[\w.-]*jitsi[\w.-]*/[^\s<>\"']+", re.I),
    re.compile(r"https?://meet\.jit\.si/[^\s<>\"']+", re.I),
    re.compile(r"https?://discord(?:app)?\.com/channels/[^\s<>\"']+", re.I),
]
_ANY_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def _clean_url(url: str) -> str:
    # Strip the punctuation that tends to trail a URL pasted into prose.
    return url.rstrip(".,;:!?)]}>'\"")


def extract_meeting_link(*texts: str | None) -> str | None:
    """Best-guess conferencing URL from the given text fields, checked in order.

    Provider-specific patterns win over a generic URL, and an earlier field's provider match
    wins over a later field's - so pass the most authoritative field (a CONFERENCE property)
    first, then location, then description.
    """
    candidates = [t for t in texts if t]
    for text in candidates:
        for pattern in _MEETING_PATTERNS:
            match = pattern.search(text)
            if match:
                return _clean_url(match.group(0))
    for text in candidates:
        match = _ANY_URL.search(text)
        if match:
            return _clean_url(match.group(0))
    return None


def local_tz() -> tzinfo:
    """The machine's current local timezone as a tzinfo (honours TZ / DST at call time)."""
    return datetime.now().astimezone().tzinfo


@lru_cache(maxsize=64)
def zone_by_name(name: str) -> tzinfo | None:
    """An IANA zone, or None if the name is unknown (missing tzdata, or a bad TZID)."""
    if not name:
        return None
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def resolve_tz(mode: str | None, event: "CalendarEvent | None" = None) -> tzinfo:
    """The timezone times should be displayed in.

    `mode` is TZ_LOCAL (the machine's zone), TZ_UTC, TZ_EVENT (the zone the event was created
    in, from its ics TZID or Google timeZone) or an IANA name. Anything unresolvable falls back
    to the machine's zone rather than silently showing the wrong hour in UTC.
    """
    if mode == TZ_UTC:
        return timezone.utc
    if mode == TZ_EVENT:
        zone = zone_by_name(event.tzid) if event is not None else None
        return zone or local_tz()
    if mode and mode != TZ_LOCAL:
        return zone_by_name(mode) or local_tz()
    return local_tz()


def as_utc(value: datetime) -> datetime:
    """Aware UTC datetime. Naive input is taken as local time (floating iCalendar times)."""
    if value.tzinfo is None:
        value = value.astimezone()  # naive -> local
    return value.astimezone(timezone.utc)


@dataclass
class CalendarEvent:
    uid: str                  # unique per *instance* (recurrence-expanded), not per series
    calendar_id: str
    title: str
    start: datetime           # aware; timed events in UTC, all-day at local midnight
    end: datetime             # aware, exclusive
    all_day: bool = False
    location: str = ""
    description: str = ""
    url: str | None = None
    meeting_link: str | None = None
    status: str = STATUS_CONFIRMED
    series_uid: str = ""      # the iCalendar UID this instance came from
    tzid: str = ""            # IANA zone the event was authored in, for the "event" display mode
    # Filled in by the foreground from the calendar's configuration, never by the backend.
    calendar_name: str = ""
    color: tuple[int, int, int, int] | None = None

    # --- time helpers ------------------------------------------------------------

    def seconds_until_start(self, now: datetime) -> float:
        return (self.start - now).total_seconds()

    def seconds_until_end(self, now: datetime) -> float:
        return (self.end - now).total_seconds()

    def is_in_progress(self, now: datetime) -> bool:
        return self.start <= now < self.end

    def is_over(self, now: datetime) -> bool:
        return now >= self.end

    def is_upcoming(self, now: datetime) -> bool:
        return now < self.start

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def is_cancelled(self) -> bool:
        return self.status == STATUS_CANCELLED

    def progress_fraction(self, now: datetime) -> float:
        """0.0 at start, 1.0 at end; clamped."""
        total = self.duration.total_seconds()
        if total <= 0:
            return 1.0 if now >= self.start else 0.0
        return max(0.0, min(1.0, (now - self.start).total_seconds() / total))

    # --- (de)serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        if self.all_day:
            start, end = self.start.date().isoformat(), self.end.date().isoformat()
        else:
            start, end = as_utc(self.start).isoformat(), as_utc(self.end).isoformat()
        return {
            "uid": self.uid,
            "series_uid": self.series_uid,
            "calendar_id": self.calendar_id,
            "title": self.title,
            "start": start,
            "end": end,
            "all_day": self.all_day,
            "location": self.location,
            "description": self.description,
            "url": self.url,
            "meeting_link": self.meeting_link,
            "status": self.status,
            "tzid": self.tzid,
        }

    @classmethod
    def from_dict(cls, data: dict, tz: tzinfo | None = None) -> "CalendarEvent":
        tz = tz or local_tz()
        all_day = bool(data.get("all_day"))
        if all_day:
            start = datetime.combine(date.fromisoformat(data["start"]), time(0), tzinfo=tz)
            end = datetime.combine(date.fromisoformat(data["end"]), time(0), tzinfo=tz)
        else:
            start = datetime.fromisoformat(data["start"])
            end = datetime.fromisoformat(data["end"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
        return cls(
            uid=data["uid"],
            series_uid=data.get("series_uid", ""),
            calendar_id=data.get("calendar_id", ""),
            title=data.get("title") or "(no title)",
            start=start,
            end=end,
            all_day=all_day,
            location=data.get("location") or "",
            description=data.get("description") or "",
            url=data.get("url"),
            meeting_link=data.get("meeting_link"),
            status=data.get("status") or STATUS_CONFIRMED,
            tzid=data.get("tzid") or "",
        )


@dataclass
class CalendarStatus:
    """Per-calendar fetch status, relayed alongside the events."""
    calendar_id: str
    ok: bool = True
    error: str | None = None
    fetched_at: datetime | None = None
    from_cache: bool = False
    event_count: int = 0
    needs_reauth: bool = False   # the source's stored authorization was rejected

    def to_dict(self) -> dict:
        return {
            "calendar_id": self.calendar_id,
            "ok": self.ok,
            "error": self.error,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "from_cache": self.from_cache,
            "event_count": self.event_count,
            "needs_reauth": self.needs_reauth,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CalendarStatus":
        fetched = data.get("fetched_at")
        return cls(
            calendar_id=data["calendar_id"],
            ok=bool(data.get("ok", True)),
            error=data.get("error"),
            fetched_at=datetime.fromisoformat(fetched) if fetched else None,
            from_cache=bool(data.get("from_cache")),
            event_count=int(data.get("event_count", 0)),
            needs_reauth=bool(data.get("needs_reauth")),
        )


# --- display formatting --------------------------------------------------------------

def format_countdown(seconds: float, compact: bool = True) -> str:
    """Human countdown for a key label. Negative input means 'already started'.

    compact=True keeps it to one short token so it fits a Stream Deck key:
    "now", "<1m", "12m", "1h05", "3d". compact=False reads "in 1h 05m".
    """
    if seconds < 0:
        return "now"
    if seconds < 60:
        return "<1m" if compact else "in <1 min"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m" if compact else f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}" if compact else f"in {hours}h {minutes:02d}m"
    days = hours // 24
    return f"{days}d" if compact else f"in {days} day{'s' if days != 1 else ''}"


def format_remaining(seconds: float) -> str:
    """Time left in an in-progress event: "25m left", "1h05 left", "ending"."""
    if seconds < 60:
        return "ending"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m left"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d} left"


def prefers_12_hour_clock() -> bool:
    """Heuristic for the 'auto' time format: en_US-style locales get a 12-hour clock."""
    for var in ("LC_ALL", "LC_TIME", "LANG"):
        value = os.environ.get(var)
        if value:
            return value.lower().startswith(("en_us", "en_ca", "en_au", "en_nz", "en_in", "en_ph"))
    return False


def format_clock(value: datetime, time_format: str = "auto", tz: tzinfo | None = None) -> str:
    """Wall-clock time of `value` in local time. time_format: "auto" | "12" | "24"."""
    local = value.astimezone(tz or local_tz())
    use_12 = time_format == "12" or (time_format == "auto" and prefers_12_hour_clock())
    if use_12:
        hour = local.hour % 12 or 12
        return f"{hour}:{local.minute:02d}{'am' if local.hour < 12 else 'pm'}"
    return f"{local.hour:02d}:{local.minute:02d}"


def _day_label(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if 1 < delta < 7:
        return day.strftime("%a")
    return day.strftime("%b %-d")


def format_day(value: datetime, now: datetime, tz: tzinfo | None = None) -> str:
    """"Today", "Tomorrow", else weekday abbreviation ("Mon"), else "Sep 14" beyond a week."""
    tz = tz or local_tz()
    return _day_label(value.astimezone(tz).date(), now.astimezone(tz).date())


def format_event_day(event: CalendarEvent, now: datetime, tz: tzinfo | None = None) -> str:
    """Like format_day, but an all-day event keeps its own date.

    All-day events are pinned to midnight in the *machine's* zone by `from_dict`, so
    converting them into a different display zone would shift them a day. Their date is
    absolute anyway - only "today" has to be evaluated in the display zone.
    """
    tz = tz or local_tz()
    day = event.start.date() if event.all_day else event.start.astimezone(tz).date()
    return _day_label(day, now.astimezone(tz).date())


def format_start(event: CalendarEvent, now: datetime, time_format: str = "auto", tz: tzinfo | None = None) -> str:
    """Start time for a label: today's timed events show the clock time, later ones the day.
    All-day events show the day ("Today", "Tomorrow", "Fri")."""
    tz = tz or local_tz()
    if event.all_day:
        return format_event_day(event, now, tz)
    same_day = event.start.astimezone(tz).date() == now.astimezone(tz).date()
    if same_day:
        return format_clock(event.start, time_format, tz)
    return f"{format_day(event.start, now, tz)} {format_clock(event.start, time_format, tz)}"


def truncate(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1].rstrip() + "…"
