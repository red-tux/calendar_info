# Import StreamController modules
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.DeckManagement.InputIdentifier import Input

# Import python modules
import json
import os
import threading
import uuid
from dataclasses import dataclass

from loguru import logger as log

# Import plugin internals
from .internal.events import CalendarEvent, CalendarStatus
from .internal.event_store import EventStore
from .settings_area import CalendarSettingsGroup

# Import actions
from .actions.NextEvent.NextEvent import NextEvent
from .actions.Agenda.Agenda import Agenda
from .actions.UpcomingDial.UpcomingDial import UpcomingDial
from .actions.common.calendar_action_base import ICON_ASSET_DEFAULTS, COLOR_ASSET_DEFAULTS

KEY_ONLY_SUPPORT = {
    Input.Key: ActionInputSupport.SUPPORTED,
    Input.Dial: ActionInputSupport.UNSUPPORTED,
    Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
    Input.TouchKey: ActionInputSupport.UNSUPPORTED,
    Input.Screen: ActionInputSupport.UNSUPPORTED,
}

DIAL_ONLY_SUPPORT = {
    Input.Key: ActionInputSupport.UNSUPPORTED,
    Input.Dial: ActionInputSupport.SUPPORTED,
    Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
    Input.TouchKey: ActionInputSupport.UNSUPPORTED,
    Input.Screen: ActionInputSupport.UNSUPPORTED,
}

DEFAULT_REFRESH_MINUTES = 5
DEFAULT_DAYS_AHEAD = 7
DEFAULT_TIME_FORMAT = "auto"
TIME_FORMATS = ["auto", "12", "24"]
DEFAULT_CALENDAR_COLOR = (66, 133, 244, 255)


@dataclass
class PluginOptions:
    """Plugin-level options, cached in memory so actions don't re-read the settings JSON on
    every tick. Refreshed by `CalendarInfoPlugin.reload_options()` whenever settings change."""
    time_format: str = DEFAULT_TIME_FORMAT
    hide_all_day: bool = False
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES
    days_ahead: int = DEFAULT_DAYS_AHEAD


class CalendarInfoPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        # Single source of truth for events + shared dismiss/skip state, read by every action.
        self.event_store = EventStore()
        self.options = PluginOptions()
        self.reload_options()
        # Backend-side copy of every calendar's last good download, so a network blip
        # doesn't blank the deck.
        self.cache_dir = os.path.join(self.PATH, "cache", "calendars")

        # Icons/colors as user-customizable assets (Settings > Assets / Colors). PluginBase
        # applies these as defaults only; a user override always wins.
        for key, filename in ICON_ASSET_DEFAULTS.items():
            self.add_icon(key, os.path.join(self.PATH, "assets", "icons", "material", filename))
        for key, rgba in COLOR_ASSET_DEFAULTS.items():
            self.add_color(key, rgba)

        self.add_action_holders([
            ActionHolder(
                plugin_base=self,
                action_base=NextEvent,
                action_id_suffix="NextEvent",
                action_name="Next Event",
                action_support=KEY_ONLY_SUPPORT,
                description=(
                    "Shows your next (or currently running) event with a live countdown. The "
                    "background turns to the warning and urgent colors as the start approaches and "
                    "can flash in the final minutes. Open Meeting Link, Dismiss Alert, Skip Event "
                    "and Refresh Calendars are separately assignable via the Event Assigner."
                ),
                settings_schema={
                    "top_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "countdown"},
                    "middle_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "none"},
                    "bottom_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "title"},
                    "title_chars": {"type": "int", "default": 12},
                    "warn_minutes": {"type": "int", "default": 15},
                    "urgent_minutes": {"type": "int", "default": 5},
                    "flash_urgent": {"type": "bool", "default": True},
                    "show_in_progress": {"type": "bool", "default": True},
                    "include_all_day": {"type": "bool", "default": False},
                    "show_stripe": {"type": "bool", "default": True},
                    "show_icon": {"type": "bool", "default": True},
                    "lookahead_hours": {"type": "int", "description": "0 = unlimited", "default": 0},
                },
            ),
            ActionHolder(
                plugin_base=self,
                action_base=Agenda,
                action_id_suffix="Agenda",
                action_name="Agenda",
                action_support=KEY_ONLY_SUPPORT,
                description=(
                    "Step through today's (or all upcoming) events on one key. Next Event, "
                    "Previous Event, Back To First, Open Meeting Link, Dismiss Alert and Refresh "
                    "Calendars are separately assignable via the Event Assigner (default: press = "
                    "next, hold = previous). Add a 'position' label to see e.g. 2/5."
                ),
                settings_schema={
                    "top_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "time"},
                    "middle_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "position"},
                    "bottom_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "title"},
                    "title_chars": {"type": "int", "default": 12},
                    "scope": {"type": "string", "values": ["upcoming", "today"], "default": "upcoming"},
                    "max_events": {"type": "int", "default": 10},
                    "warn_minutes": {"type": "int", "default": 15},
                    "urgent_minutes": {"type": "int", "default": 5},
                    "flash_urgent": {"type": "bool", "default": True},
                    "show_in_progress": {"type": "bool", "default": True},
                    "include_all_day": {"type": "bool", "default": False},
                    "show_stripe": {"type": "bool", "default": True},
                    "show_icon": {"type": "bool", "default": True},
                },
            ),
            ActionHolder(
                plugin_base=self,
                action_base=UpcomingDial,
                action_id_suffix="UpcomingDial",
                action_name="Upcoming (Dial)",
                action_support=DIAL_ONLY_SUPPORT,
                description=(
                    "Upcoming events on a dial: turn to browse, press to open the meeting link, "
                    "hold to jump back to the next event, tap the screen to dismiss an alert (all "
                    "rebindable via the Event Assigner). A bar fills as the start approaches and "
                    "then tracks the running event's progress."
                ),
                settings_schema={
                    "top_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "title"},
                    "middle_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "countdown"},
                    "bottom_label": {"type": "string", "values": ["none", "title", "countdown", "time", "day", "calendar", "location", "position"], "default": "time"},
                    "title_chars": {"type": "int", "default": 12},
                    "scope": {"type": "string", "values": ["upcoming", "today"], "default": "upcoming"},
                    "max_events": {"type": "int", "default": 10},
                    "warn_minutes": {"type": "int", "default": 15},
                    "urgent_minutes": {"type": "int", "default": 5},
                    "flash_urgent": {"type": "bool", "default": True},
                    "show_in_progress": {"type": "bool", "default": True},
                    "include_all_day": {"type": "bool", "default": False},
                    "show_stripe": {"type": "bool", "default": True},
                    "show_icon": {"type": "bool", "default": True},
                    "show_bar": {"type": "bool", "default": True},
                },
            ),
        ])

        # name/github/version/app-version all come from manifest.json.
        self.register()

        # launch_backend() can block on first run (building the venv) - keep it off the UI thread.
        threading.Thread(target=self._launch_backend, name="calendar_launch_backend", daemon=True).start()

    # --- settings ---------------------------------------------------------------------------

    def get_settings_area(self):
        return CalendarSettingsGroup(self)

    def reload_options(self) -> None:
        settings = self.get_settings()
        time_format = settings.get("time_format", DEFAULT_TIME_FORMAT)
        self.options = PluginOptions(
            time_format=time_format if time_format in TIME_FORMATS else DEFAULT_TIME_FORMAT,
            hide_all_day=bool(settings.get("hide_all_day", False)),
            refresh_minutes=max(1, int(settings.get("refresh_minutes", DEFAULT_REFRESH_MINUTES) or DEFAULT_REFRESH_MINUTES)),
            days_ahead=max(1, int(settings.get("days_ahead", DEFAULT_DAYS_AHEAD) or DEFAULT_DAYS_AHEAD)),
        )

    def get_calendars(self) -> list[dict]:
        """Configured calendars, each normalized to {id, name, source, enabled, color}."""
        calendars = []
        for raw in self.get_settings().get("calendars", []) or []:
            if not isinstance(raw, dict):
                continue
            color = raw.get("color") or list(DEFAULT_CALENDAR_COLOR)
            calendars.append({
                "id": str(raw.get("id") or uuid.uuid4().hex),
                "name": str(raw.get("name") or "Calendar"),
                "source": str(raw.get("source") or ""),
                "enabled": bool(raw.get("enabled", True)),
                "color": [int(c) for c in color][:4] if len(color) >= 4 else list(DEFAULT_CALENDAR_COLOR),
            })
        return calendars

    def set_calendars(self, calendars: list[dict]) -> None:
        settings = self.get_settings()
        settings["calendars"] = calendars
        self.set_settings(settings)
        self.on_settings_changed()

    def on_settings_changed(self) -> None:
        """Called by the settings UI after any plugin-level setting changes."""
        self.reload_options()
        self._push_backend_config()
        # Calendar names/colors are attached on the foreground, so re-decorate what we have.
        self._redecorate_events()

    # --- backend --------------------------------------------------------------------------

    def _launch_backend(self) -> None:
        self.launch_backend(
            backend_path=os.path.join(self.PATH, "backend", "backend.py"),
            venv_path=os.path.join(self.PATH, ".venv"),
        )

    def register_backend(self, port: int) -> None:
        super().register_backend(port)
        self.event_store.set_backend_connected(True)
        self._push_backend_config()

    def _backend_config_json(self) -> str:
        return json.dumps({
            "calendars": [
                {"id": c["id"], "name": c["name"], "source": c["source"], "enabled": c["enabled"]}
                for c in self.get_calendars()
            ],
            "refresh_seconds": self.options.refresh_minutes * 60,
            "days_back": 1,
            "days_ahead": self.options.days_ahead,
            "cache_dir": self.cache_dir,
        })

    def _push_backend_config(self) -> None:
        if self.backend is None:
            return
        try:
            self.backend.configure(self._backend_config_json())
        except Exception as e:
            log.error(f"Failed to push calendar configuration to backend: {e}")

    def refresh_now(self) -> None:
        if self.backend is None:
            log.info("Calendar backend not connected yet; refresh ignored")
            return
        try:
            self.backend.refresh_now()
        except Exception as e:
            log.error(f"Failed to request a calendar refresh: {e}")

    def test_calendar_source(self, source: str) -> dict:
        """Blocking fetch+parse of one source via the backend, for the settings UI's Test
        button. Call from a worker thread. Returns {"ok", "count", "error", "sample"}."""
        if self.backend is None:
            return {"ok": False, "count": 0, "error": "Calendar backend is still starting, try again in a moment", "sample": []}
        try:
            return json.loads(self.backend.test_source(source))
        except Exception as e:
            return {"ok": False, "count": 0, "error": str(e), "sample": []}

    # --- called by the backend over RPyC -----------------------------------------------------

    def on_events_update(self, payload: str) -> None:
        """`payload` is JSON text (see backend.py for why it isn't a dict)."""
        try:
            data = json.loads(payload)
        except ValueError as e:
            log.error(f"Bad events payload from backend: {e}")
            return
        events = []
        for raw in data.get("events", []):
            try:
                events.append(CalendarEvent.from_dict(raw))
            except (KeyError, ValueError) as e:
                log.warning(f"Skipping malformed event {raw.get('uid')!r}: {e}")
        statuses = [CalendarStatus.from_dict(s) for s in data.get("statuses", [])]
        self._decorate(events)
        self._raw_events = events
        self.event_store.update(events, statuses)

    def _decorate(self, events: list[CalendarEvent]) -> None:
        by_id = {c["id"]: c for c in self.get_calendars()}
        for event in events:
            calendar = by_id.get(event.calendar_id)
            if calendar is None:
                continue
            event.calendar_name = calendar["name"]
            event.color = tuple(calendar["color"])

    def _redecorate_events(self) -> None:
        events = getattr(self, "_raw_events", None)
        if not events:
            return
        self._decorate(events)
        self.event_store.update(events, self.event_store.get_statuses().values())
