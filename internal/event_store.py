"""Thread-safe holder of the latest expanded events, with pub/sub for actions.

Fed by the backend relay (`CalendarInfoPlugin.on_events_update`), read by every action so
they don't each keep their own copy. Also owns the two bits of shared per-instance user
state - dismissed alerts and skipped events - so every key/dial on the deck agrees.

Fan-out runs through `dispatch` (GLib.idle_add in the app, so subscriber callbacks land on
the GTK main thread; tests pass a synchronous callable instead).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable, Iterable

from .events import CalendarEvent, CalendarStatus

Subscriber = Callable[[], None]


def _glib_dispatch(callback, *args) -> None:
    from gi.repository import GLib  # imported lazily so tests don't need PyGObject
    GLib.idle_add(callback, *args)


class EventStore:
    def __init__(self, dispatch: Callable | None = None):
        self._lock = threading.Lock()
        self._dispatch = dispatch or _glib_dispatch
        self._events: list[CalendarEvent] = []
        self._statuses: dict[str, CalendarStatus] = {}
        self._last_updated: datetime | None = None
        self._backend_connected = False
        self._dismissed: set[str] = set()   # alert silenced, event still shown
        self._skipped: set[str] = set()     # hidden from "next event" until it ends
        self._subscribers: dict[int, Subscriber] = {}
        self._next_token = 1

    # --- ingestion -----------------------------------------------------------------

    def update(self, events: Iterable[CalendarEvent], statuses: Iterable[CalendarStatus] = (), now: datetime | None = None) -> None:
        events = sorted(events, key=lambda e: (e.start, e.end, e.title))
        with self._lock:
            self._events = events
            self._statuses = {s.calendar_id: s for s in statuses}
            self._last_updated = now or datetime.now().astimezone()
            self._prune_locked(now or self._last_updated)
        self._notify()

    def set_backend_connected(self, connected: bool) -> None:
        with self._lock:
            changed = self._backend_connected != connected
            self._backend_connected = connected
        if changed:
            self._notify()

    def clear(self) -> None:
        with self._lock:
            self._events = []
            self._statuses = {}
        self._notify()

    # --- queries (all take an aware `now`) ------------------------------------------

    def get_events(self) -> list[CalendarEvent]:
        with self._lock:
            return list(self._events)

    def get_statuses(self) -> dict[str, CalendarStatus]:
        with self._lock:
            return dict(self._statuses)

    def get_last_updated(self) -> datetime | None:
        with self._lock:
            return self._last_updated

    def is_backend_connected(self) -> bool:
        with self._lock:
            return self._backend_connected

    def has_errors(self) -> bool:
        with self._lock:
            return any(not s.ok for s in self._statuses.values())

    def get_upcoming(
        self,
        now: datetime,
        limit: int | None = None,
        include_all_day: bool = True,
        include_in_progress: bool = True,
        include_cancelled: bool = False,
        include_skipped: bool = False,
        horizon: timedelta | None = None,
        calendar_ids: set[str] | None = None,
    ) -> list[CalendarEvent]:
        """Events that haven't ended yet, soonest first. In-progress events come first when
        included (they started earliest). `horizon` caps how far ahead to look, and
        `calendar_ids` restricts the result to those configured calendars (None = all)."""
        with self._lock:
            events = list(self._events)
            skipped = set(self._skipped)
        cutoff = now + horizon if horizon else None
        result = []
        for event in events:
            if calendar_ids is not None and event.calendar_id not in calendar_ids:
                continue
            if event.is_over(now):
                continue
            if not include_in_progress and event.is_in_progress(now):
                continue
            if not include_all_day and event.all_day:
                continue
            if not include_cancelled and event.is_cancelled:
                continue
            if not include_skipped and event.uid in skipped:
                continue
            if cutoff and event.start >= cutoff:
                continue
            result.append(event)
            if limit is not None and len(result) >= limit:
                break
        return result

    def get_next(self, now: datetime, **kwargs) -> CalendarEvent | None:
        upcoming = self.get_upcoming(now, limit=1, **kwargs)
        return upcoming[0] if upcoming else None

    def get_today(self, now: datetime, include_past: bool = True,
                  calendar_ids: set[str] | None = None, **kwargs) -> list[CalendarEvent]:
        """Every event whose start falls on `now`'s local calendar day."""
        tz = now.tzinfo
        today = now.astimezone(tz).date()
        with self._lock:
            events = list(self._events)
        result = []
        for event in events:
            if calendar_ids is not None and event.calendar_id not in calendar_ids:
                continue
            if event.start.astimezone(tz).date() != today:
                continue
            if not include_past and event.is_over(now):
                continue
            if not kwargs.get("include_all_day", True) and event.all_day:
                continue
            if not kwargs.get("include_cancelled", False) and event.is_cancelled:
                continue
            result.append(event)
        return result

    # --- shared per-instance user state ----------------------------------------------

    def dismiss(self, uid: str) -> None:
        with self._lock:
            self._dismissed.add(uid)
        self._notify()

    def is_dismissed(self, uid: str) -> bool:
        with self._lock:
            return uid in self._dismissed

    def skip(self, uid: str) -> None:
        with self._lock:
            self._skipped.add(uid)
        self._notify()

    def unskip_all(self) -> None:
        with self._lock:
            self._skipped.clear()
        self._notify()

    def is_skipped(self, uid: str) -> bool:
        with self._lock:
            return uid in self._skipped

    def _prune_locked(self, now: datetime) -> None:
        """Forget dismiss/skip marks for instances that have ended (or vanished)."""
        live = {e.uid for e in self._events if not e.is_over(now)}
        self._dismissed &= live
        self._skipped &= live

    # --- pub/sub ---------------------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> int:
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._subscribers[token] = callback
        return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subscribers.pop(token, None)

    def _notify(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers.values())
        for callback in subscribers:
            self._dispatch(callback)
