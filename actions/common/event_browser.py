"""Shared "step through a list of events" behaviour for Agenda (key) and Upcoming (dial).

Selection is tracked by event uid rather than index, so the selection survives the list
shifting underneath it (an event ending, a refresh adding one) and falls back to the first
entry when the selected event disappears.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from GtkHelper.GenerativeUI.ComboRow import ComboRow
from GtkHelper.GenerativeUI.SpinRow import SpinRow

from .calendar_action_base import CalendarActionMixin
from ...internal.events import CalendarEvent

SCOPE_UPCOMING = "upcoming"
SCOPE_TODAY = "today"
SCOPE_CHOICES = [SCOPE_UPCOMING, SCOPE_TODAY]
DEFAULT_MAX_EVENTS = 10


class EventBrowserMixin(CalendarActionMixin):
    def setup_browser_rows(self, on_change) -> None:
        self._selected_uid: str | None = None
        self.scope_row = ComboRow(
            self, "scope", SCOPE_UPCOMING, items=SCOPE_CHOICES, title="Events To Browse",
            subtitle="upcoming = everything not yet over, today = the rest of today only",
            on_change=on_change,
        )
        self.max_events_row = SpinRow(
            self, "max_events", DEFAULT_MAX_EVENTS, min=1, max=50, step=1, digits=0,
            title="Max Events", on_change=on_change,
        )

    def browse_list(self, now: datetime) -> list[CalendarEvent]:
        limit = int(self.max_events_row.get_value(fallback=DEFAULT_MAX_EVENTS))
        scope = self.scope_row.get_value(fallback=SCOPE_UPCOMING)
        # Browsing shows everything, including events skipped on a Next Event key.
        kwargs = dict(include_all_day=self.include_all_day(), include_in_progress=self.show_in_progress(), include_skipped=True)
        if scope == SCOPE_TODAY:
            end_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            return self.store.get_upcoming(now, limit=limit, horizon=end_of_day - now, **kwargs)
        return self.store.get_upcoming(now, limit=limit, **kwargs)

    def selection(self, now: datetime) -> tuple[CalendarEvent | None, int, int]:
        """(selected event, 0-based index, list length). Falls back to the first event."""
        events = self.browse_list(now)
        if not events:
            return None, 0, 0
        for index, event in enumerate(events):
            if event.uid == self._selected_uid:
                return event, index, len(events)
        self._selected_uid = events[0].uid
        return events[0], 0, len(events)

    def position_text(self, event: CalendarEvent | None, now: datetime) -> str:
        if event is None:
            return ""
        _, index, total = self.selection(now)
        return f"{index + 1}/{total}"

    def _step(self, delta: int) -> None:
        now = self.now()
        events = self.browse_list(now)
        if not events:
            return
        _, index, _ = self.selection(now)
        self._selected_uid = events[(index + delta) % len(events)].uid
        self._last_render_key = None
        self.render()

    # --- assigner callbacks (event threads) ---------------------------------------------

    def _do_next(self, data=None) -> None:
        self._step(+1)

    def _do_previous(self, data=None) -> None:
        self._step(-1)

    def _do_first(self, data=None) -> None:
        self._selected_uid = None
        self._last_render_key = None
        self.render()

    def _do_open_link(self, data=None) -> None:
        event, _, _ = self.selection(self.now())
        self.open_link(event.meeting_link if event else None)

    def _do_dismiss(self, data=None) -> None:
        event, _, _ = self.selection(self.now())
        if event is not None:
            self.store.dismiss(event.uid)

    def _do_refresh(self, data=None) -> None:
        self.refresh_calendars()
