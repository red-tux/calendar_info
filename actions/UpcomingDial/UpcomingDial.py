from src.backend.PluginManager.InputBases import DialAction
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.DeckManagement.InputIdentifier import Input
from GtkHelper.GenerativeUI.SwitchRow import SwitchRow

from ..common.calendar_action_base import LEVEL_URGENT, LEVEL_IN_PROGRESS
from ..common.event_browser import EventBrowserMixin


class UpcomingDial(EventBrowserMixin, DialAction):
    """Upcoming events on a Stream Deck + dial: turn to browse, press to join.

    Shows the selected event's icon, calendar color bar, labels and a bar that fills as the
    start approaches (inside the warning window) and then tracks the event's progress while
    it runs. Every gesture is assignable via the Event Assigner.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, default_events=False, **kwargs)
        self.setup_label_rows(self._on_setting_changed, top_default="title", middle_default="countdown", bottom_default="time")
        self.setup_browser_rows(self._on_setting_changed)
        self.setup_alert_rows(self._on_setting_changed)
        self.show_bar_row = SwitchRow(
            self, "show_bar", True, title="Show Progress Bar",
            subtitle="Fills during the warning window, then tracks the running event",
            on_change=self._on_setting_changed,
        )
        self._last_render_key = None

        self.add_event_assigner(EventAssigner(
            id="Next Event", ui_label="Next Event",
            default_events=[Input.Dial.Events.TURN_CW], callback=self._do_next,
        ))
        self.add_event_assigner(EventAssigner(
            id="Previous Event", ui_label="Previous Event",
            default_events=[Input.Dial.Events.TURN_CCW], callback=self._do_previous,
        ))
        self.add_event_assigner(EventAssigner(
            id="Open Meeting Link", ui_label="Open Meeting Link",
            default_events=[Input.Dial.Events.SHORT_UP], callback=self._do_open_link,
        ))
        self.add_event_assigner(EventAssigner(
            id="Back To First", ui_label="Back To First",
            default_events=[Input.Dial.Events.HOLD_START], callback=self._do_first,
        ))
        self.add_event_assigner(EventAssigner(
            id="Dismiss Alert", ui_label="Dismiss Alert",
            default_events=[Input.Dial.Events.SHORT_TOUCH_PRESS], callback=self._do_dismiss,
        ))
        self.add_event_assigner(EventAssigner(id="Refresh Calendars", ui_label="Refresh Calendars", callback=self._do_refresh))

    def _on_setting_changed(self, widget, new_value, old_value) -> None:
        self._last_render_key = None
        self.render()

    def bar_fraction(self, event, level, now) -> float | None:
        if event is None or not self.show_bar_row.get_value(fallback=True):
            return None
        if level == LEVEL_IN_PROGRESS:
            return event.progress_fraction(now)
        warn = self.warn_minutes() * 60
        if warn <= 0 or event.all_day:
            return 0.0
        remaining = event.seconds_until_start(now)
        return max(0.0, min(1.0, 1 - remaining / warn))

    def render(self) -> None:
        if not self.can_render():
            return
        now = self.now()
        event, index, total = self.selection(now)
        level = self.alert_level(event, now)
        flashing = level == LEVEL_URGENT and self.flash_enabled()
        flash_on = self.flash_phase() if flashing else True
        size = self.get_display_size(fallback=(200, 100))
        icon = self.icon_for(event) if self.show_icon() else None
        stripe = self.show_stripe()
        labels = self.label_values(event, now)
        fraction = self.bar_fraction(event, level, now)
        # Quantize so the bar only repaints when it visibly moves (~1px on a 200px-wide slot).
        bar_step = None if fraction is None else round(fraction * size[0])

        key = (event.uid if event else None, index, total, level, flash_on, size, icon, stripe, event.color if event else None, labels, bar_step)
        if key == self._last_render_key:
            return
        self._last_render_key = key

        width, height = size
        icon_box = (0, 0, height, height)  # square slot at the left; labels use the rest
        image = self.compose(size, level, event, flash_on, icon, icon_box=icon_box, icon_margin=0.2, stripe=stripe)
        if fraction is not None:
            self.draw_bar(image, fraction)
        self.ui(self.set_media, image=image, size=1.0)
        self.push_labels(labels)
