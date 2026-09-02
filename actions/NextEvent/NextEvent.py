from datetime import timedelta

from src.backend.PluginManager.InputBases import KeyAction
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.DeckManagement.InputIdentifier import Input
from GtkHelper.GenerativeUI.SpinRow import SpinRow

from ..common.calendar_action_base import CalendarActionMixin, LEVEL_URGENT

DEFAULT_LOOKAHEAD_HOURS = 0   # 0 = unlimited (bounded only by the plugin's fetch window)


class NextEvent(CalendarActionMixin, KeyAction):
    """The next (or currently running) event: countdown + title labels over a background that
    turns to the warning/urgent color as the start approaches, and a calendar color bar.

    Open Meeting Link, Dismiss Alert, Skip Event and Refresh Calendars are separately
    assignable via the Event Assigner. Dismiss silences the color/flash for this one event;
    Skip hides it so the following event shows instead (until it would have ended).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, default_events=False, **kwargs)
        self.setup_label_rows(self._on_setting_changed)
        self.setup_alert_rows(self._on_setting_changed)
        self.lookahead_row = SpinRow(
            self, "lookahead_hours", DEFAULT_LOOKAHEAD_HOURS, min=0, max=336, step=1, digits=0,
            title="Look Ahead (hours)", subtitle="0 = no limit; otherwise only events starting within this many hours",
            on_change=self._on_setting_changed,
        )
        self._last_render_key = None

        self.add_event_assigner(EventAssigner(
            id="Open Meeting Link", ui_label="Open Meeting Link",
            default_events=[Input.Key.Events.SHORT_UP], callback=self._do_open_link,
        ))
        self.add_event_assigner(EventAssigner(
            id="Dismiss Alert", ui_label="Dismiss Alert",
            default_events=[Input.Key.Events.HOLD_START], callback=self._do_dismiss,
        ))
        self.add_event_assigner(EventAssigner(id="Skip Event", ui_label="Skip Event", callback=self._do_skip))
        self.add_event_assigner(EventAssigner(id="Refresh Calendars", ui_label="Refresh Calendars", callback=self._do_refresh))

    def _on_setting_changed(self, widget, new_value, old_value) -> None:
        self._last_render_key = None
        self.render()

    def current_event(self, now):
        hours = int(self.lookahead_row.get_value(fallback=DEFAULT_LOOKAHEAD_HOURS))
        return self.store.get_next(
            now,
            include_all_day=self.include_all_day(),
            include_in_progress=self.show_in_progress(),
            horizon=timedelta(hours=hours) if hours > 0 else None,
        )

    def render(self) -> None:
        if not self.can_render():
            return
        now = self.now()
        event = self.current_event(now)
        level = self.alert_level(event, now)
        flashing = level == LEVEL_URGENT and self.flash_enabled()
        flash_on = self.flash_phase() if flashing else True
        size = self.get_display_size()
        icon = self.icon_for(event) if self.show_icon() else None
        stripe = self.show_stripe()
        labels = self.label_values(event, now)

        key = (event.uid if event else None, level, flash_on, size, icon, stripe, event.color if event else None, labels)
        if key == self._last_render_key:
            return
        self._last_render_key = key

        image = self.compose(size, level, event, flash_on, icon, stripe=stripe)
        self.ui(self.set_media, image=image, size=1.0)
        self.push_labels(labels)

    # --- assigner callbacks (event threads) ---------------------------------------------

    def _do_open_link(self, data=None) -> None:
        event = self.current_event(self.now())
        self.open_link(event.meeting_link if event else None)

    def _do_dismiss(self, data=None) -> None:
        event = self.current_event(self.now())
        if event is not None:
            self.store.dismiss(event.uid)

    def _do_skip(self, data=None) -> None:
        event = self.current_event(self.now())
        if event is not None:
            self.store.skip(event.uid)

    def _do_refresh(self, data=None) -> None:
        self.refresh_calendars()
