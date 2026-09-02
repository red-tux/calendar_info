from src.backend.PluginManager.InputBases import KeyAction
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.DeckManagement.InputIdentifier import Input

from ..common.calendar_action_base import LEVEL_URGENT
from ..common.event_browser import EventBrowserMixin


class Agenda(EventBrowserMixin, KeyAction):
    """Step through today's (or all upcoming) events on one key.

    Next Event / Previous Event / Back To First / Open Meeting Link / Dismiss Alert / Refresh
    Calendars are separately assignable via the Event Assigner (default: press = next, hold =
    previous). A "position" label shows where you are, e.g. 2/5.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, default_events=False, **kwargs)
        self.setup_label_rows(self._on_setting_changed, top_default="time", middle_default="position", bottom_default="title")
        self.setup_browser_rows(self._on_setting_changed)
        self.setup_alert_rows(self._on_setting_changed)
        self._last_render_key = None

        self.add_event_assigner(EventAssigner(
            id="Next Event", ui_label="Next Event",
            default_events=[Input.Key.Events.DOWN], callback=self._do_next,
        ))
        self.add_event_assigner(EventAssigner(
            id="Previous Event", ui_label="Previous Event",
            default_events=[Input.Key.Events.HOLD_START], callback=self._do_previous,
        ))
        self.add_event_assigner(EventAssigner(id="Back To First", ui_label="Back To First", callback=self._do_first))
        self.add_event_assigner(EventAssigner(id="Open Meeting Link", ui_label="Open Meeting Link", callback=self._do_open_link))
        self.add_event_assigner(EventAssigner(id="Dismiss Alert", ui_label="Dismiss Alert", callback=self._do_dismiss))
        self.add_event_assigner(EventAssigner(id="Refresh Calendars", ui_label="Refresh Calendars", callback=self._do_refresh))

    def _on_setting_changed(self, widget, new_value, old_value) -> None:
        self._last_render_key = None
        self.render()

    def render(self) -> None:
        if not self.can_render():
            return
        now = self.now()
        event, index, total = self.selection(now)
        level = self.alert_level(event, now)
        flashing = level == LEVEL_URGENT and self.flash_enabled()
        flash_on = self.flash_phase() if flashing else True
        size = self.get_display_size()
        icon = self.icon_for(event) if self.show_icon() else None
        stripe = self.show_stripe()
        labels = self.label_values(event, now)

        key = (event.uid if event else None, index, total, level, flash_on, size, icon, stripe, event.color if event else None, labels)
        if key == self._last_render_key:
            return
        self._last_render_key = key

        image = self.compose(size, level, event, flash_on, icon, stripe=stripe)
        self.ui(self.set_media, image=image, size=1.0)
        self.push_labels(labels)
