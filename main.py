# Import StreamController modules
import globals as gl
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
from .internal.events import CalendarEvent, CalendarStatus, TZ_EVENT, TZ_LOCAL, TZ_UTC, zone_by_name
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
DEFAULT_DISPLAY_TIMEZONE = TZ_LOCAL
DEFAULT_CALENDAR_COLOR = (66, 133, 244, 255)
CALENDAR_TYPE_ICS = "ics"
CALENDAR_TYPE_GOOGLE = "google"
CALENDAR_TYPES = (CALENDAR_TYPE_ICS, CALENDAR_TYPE_GOOGLE)
# The provider of a calendar's `account_id` (backend/accounts/registry.py). Entries written
# before providers existed have none and are OAuth ones.
DEFAULT_ACCOUNT_PROVIDER = "oauth"


@dataclass
class PluginOptions:
    """Plugin-level options, cached in memory so actions don't re-read the settings JSON on
    every tick. Refreshed by `CalendarInfoPlugin.reload_options()` whenever settings change."""
    time_format: str = DEFAULT_TIME_FORMAT
    # TZ_LOCAL / TZ_EVENT / TZ_UTC, or an IANA zone name.
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE
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
        # Google refresh tokens, one file per linked account, written by the backend only.
        self.credentials_dir = os.path.join(self.PATH, "credentials")

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
                    "calendar_filter": {"type": "string", "values": ["all", "selected"], "default": "all", "description": "Which calendars this key follows"},
                    "calendar_ids": {"type": "list[string]", "default": [], "description": "Configured calendar ids to show when calendar_filter is 'selected'"},
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
                    "calendar_filter": {"type": "string", "values": ["all", "selected"], "default": "all", "description": "Which calendars this key follows"},
                    "calendar_ids": {"type": "list[string]", "default": [], "description": "Configured calendar ids to show when calendar_filter is 'selected'"},
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
                    "calendar_filter": {"type": "string", "values": ["all", "selected"], "default": "all", "description": "Which calendars this key follows"},
                    "calendar_ids": {"type": "list[string]", "default": [], "description": "Configured calendar ids to show when calendar_filter is 'selected'"},
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
            display_timezone=self._valid_timezone(settings.get("display_timezone")),
            hide_all_day=bool(settings.get("hide_all_day", False)),
            refresh_minutes=max(1, int(settings.get("refresh_minutes", DEFAULT_REFRESH_MINUTES) or DEFAULT_REFRESH_MINUTES)),
            days_ahead=max(1, int(settings.get("days_ahead", DEFAULT_DAYS_AHEAD) or DEFAULT_DAYS_AHEAD)),
        )

    @staticmethod
    def _valid_timezone(value) -> str:
        """Keep an unresolvable zone out of `options`: without tzdata a stale name would
        otherwise silently display every time in the machine's zone with no explanation."""
        mode = str(value or DEFAULT_DISPLAY_TIMEZONE)
        if mode in (TZ_LOCAL, TZ_EVENT, TZ_UTC):
            return mode
        if zone_by_name(mode) is not None:
            return mode
        log.warning(f"Unknown display timezone {mode!r}; falling back to the machine's timezone")
        return DEFAULT_DISPLAY_TIMEZONE

    def get_calendars(self) -> list[dict]:
        """Configured calendars, normalized to
        {id, name, type, source, account_provider, account_id, google_calendar, enabled, color}.

        `type` is "ics" (an address or file in `source`) or "google" (the Google calendar id in
        `google_calendar`, read through the account `account_id` of `account_provider`). Entries
        written before the Google source existed have no type and default to "ics".
        """
        calendars = []
        for raw in self.get_settings().get("calendars", []) or []:
            if not isinstance(raw, dict):
                continue
            color = raw.get("color") or list(DEFAULT_CALENDAR_COLOR)
            calendar_type = str(raw.get("type") or CALENDAR_TYPE_ICS)
            calendars.append({
                "id": str(raw.get("id") or uuid.uuid4().hex),
                "name": str(raw.get("name") or "Calendar"),
                "type": calendar_type if calendar_type in CALENDAR_TYPES else CALENDAR_TYPE_ICS,
                "source": str(raw.get("source") or ""),
                "account_provider": str(raw.get("account_provider") or DEFAULT_ACCOUNT_PROVIDER),
                "account_id": str(raw.get("account_id") or ""),
                "google_calendar": str(raw.get("google_calendar") or ""),
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
        credentials = self.get_google_credentials()
        return json.dumps({
            "calendars": [
                {"id": c["id"], "name": c["name"], "type": c["type"], "source": c["source"],
                 "account_provider": c["account_provider"], "account_id": c["account_id"],
                 "google_calendar": c["google_calendar"], "enabled": c["enabled"]}
                for c in self.get_calendars()
            ],
            "refresh_seconds": self.options.refresh_minutes * 60,
            "days_back": 1,
            "days_ahead": self.options.days_ahead,
            "cache_dir": self.cache_dir,
            "credentials_dir": self.credentials_dir,
            "google": {"client_id": credentials["client_id"], "client_secret": credentials["client_secret"]},
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
            return json.loads(self.backend.test_source(
                json.dumps({"type": CALENDAR_TYPE_ICS, "source": source})))
        except Exception as e:
            return {"ok": False, "count": 0, "error": str(e), "sample": []}

    # --- accounts ----------------------------------------------------------------------------
    #
    # An account is {provider, id, label, email}; the provider (backend/accounts/) is what turns
    # the id into a credential. Secrets are never in the settings: OAuth refresh tokens live in
    # `credentials_dir` (written by the backend), desktop providers keep theirs in the desktop.
    #
    # The OAuth provider is modelled on Home Assistant's application_credentials: the plugin
    # ships no OAuth client of its own, each user registers one in their own Google Cloud project
    # and pastes it in here. That keeps every install outside Google's verification, brand
    # review and shared quota.

    def get_google_credentials(self) -> dict:
        """{"client_id", "client_secret"} - the user's own Cloud project OAuth client."""
        google = self.get_settings().get("google") or {}
        return {
            "client_id": str(google.get("client_id") or "").strip(),
            "client_secret": str(google.get("client_secret") or "").strip(),
        }

    def set_google_credentials(self, client_id: str, client_secret: str) -> None:
        settings = self.get_settings()
        google = dict(settings.get("google") or {})
        google["client_id"] = (client_id or "").strip()
        google["client_secret"] = (client_secret or "").strip()
        settings["google"] = google
        self.set_settings(settings)
        self.on_settings_changed()

    def get_accounts(self) -> list[dict]:
        """Linked accounts as [{"provider", "id", "label", "email", "calendar_type"}].

        `calendar_type` is which source reads this account's calendars, so the UI knows what
        an account is good for without re-running discovery. Accounts saved before providers
        existed sit under google.accounts, and accounts saved before this field existed can
        only be Google ones - both are read here and rewritten in the new shape on the next
        change."""
        settings = self.get_settings()
        raw_accounts = list(settings.get("accounts") or [])
        legacy = (settings.get("google") or {}).get("accounts") or []
        raw_accounts += [dict(a, provider=DEFAULT_ACCOUNT_PROVIDER) for a in legacy if isinstance(a, dict)]
        accounts, seen = [], set()
        for raw in raw_accounts:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            key = (str(raw.get("provider") or DEFAULT_ACCOUNT_PROVIDER), str(raw["id"]))
            if key in seen:
                continue
            seen.add(key)
            accounts.append({"provider": key[0], "id": key[1],
                             "label": str(raw.get("label") or ""), "email": str(raw.get("email") or ""),
                             "calendar_type": str(raw.get("calendar_type") or CALENDAR_TYPE_GOOGLE)})
        return accounts

    def _write_accounts(self, settings: dict, accounts: list[dict]) -> None:
        settings["accounts"] = accounts
        google = dict(settings.get("google") or {})
        google.pop("accounts", None)
        settings["google"] = google

    def add_account(self, provider: str, account_id: str, label: str = "", email: str = "",
                    calendar_type: str = CALENDAR_TYPE_GOOGLE) -> None:
        # Re-linking the same OAuth account gets a fresh id but the same address; desktop
        # providers' ids are stable. Either way the old entry goes.
        accounts = [a for a in self.get_accounts()
                    if a["provider"] != provider or (a["id"] != account_id and not (email and a["email"] == email))]
        accounts.append({"provider": provider, "id": account_id, "label": label, "email": email,
                         "calendar_type": calendar_type})
        settings = self.get_settings()
        self._write_accounts(settings, accounts)
        self.set_settings(settings)
        self.on_settings_changed()

    def remove_account(self, provider: str, account_id: str) -> None:
        """Drop the account, whatever its provider stored for it, and every calendar that was
        reading through it."""
        if self.backend is not None:
            try:
                self.backend.forget_account(provider, account_id)
            except Exception as e:
                log.warning(f"Could not forget {provider} account {account_id}: {e}")
        remaining = [c for c in self.get_calendars()
                     if not (c["account_provider"] == provider and c["account_id"] == account_id)]
        accounts = [a for a in self.get_accounts()
                    if not (a["provider"] == provider and a["id"] == account_id)]
        settings = self.get_settings()
        settings["calendars"] = remaining
        self._write_accounts(settings, accounts)
        self.set_settings(settings)
        self.on_settings_changed()

    def describe_sources(self) -> list[dict]:
        """The calendar types and how each is added, for the settings UI's add-a-source
        choices. Blocking; call from a worker thread."""
        if self.backend is None:
            return []
        try:
            return json.loads(self.backend.describe_sources()).get("sources") or []
        except Exception as e:
            log.warning(f"Could not read the calendar source descriptions: {e}")
            return []

    def list_desktop_accounts(self) -> dict:
        """Accounts the desktop providers can offer to link, each classified (`supported`,
        `detail`). Blocking; call from a worker thread. {"ok", "accounts", "error"}."""
        if self.backend is None:
            return {"ok": False, "accounts": [], "error": "Calendar backend is still starting"}
        try:
            return json.loads(self.backend.list_accounts(""))
        except Exception as e:
            return {"ok": False, "accounts": [], "error": str(e)}

    def list_calendars(self, calendar_type: str, provider: str, account_id: str) -> dict:
        """Blocking; call from a worker thread. {"ok", "calendars", "error"}."""
        if self.backend is None:
            return {"ok": False, "calendars": [], "error": "Calendar backend is still starting"}
        try:
            return json.loads(self.backend.list_calendars(calendar_type, provider, account_id))
        except Exception as e:
            return {"ok": False, "calendars": [], "error": str(e)}

    # --- Flatpak sandbox permissions ----------------------------------------------------------
    #
    # A desktop provider reaches outside the sandbox, to the session-bus service that hands out
    # the desktop's logins. That is a `flatpak override --user` grant; the app's manifest is not
    # involved. What is needed comes from each provider's own required_permissions(), never from
    # a name written down here, so a second provider is covered as soon as it registers.
    #
    # The plugin shows its own dialog rather than calling PluginBase.request_dbus_permission():
    # the app's has a "mark as solved" button that only closes the window, which says a
    # permission is fixed without looking. Ours rechecks, and can tell a granted-but-not-yet-
    # applied override from a working one.

    def is_flatpak(self) -> bool:
        manager = getattr(gl, "flatpak_permission_manager", None)
        return bool(manager is not None and manager.get_is_flatpak())

    def provider_permissions(self, provider: str) -> dict:
        """{"dbus": [...], "filesystem": [...]} the provider needs beyond the app's own."""
        if self.backend is None:
            return {"dbus": [], "filesystem": []}
        try:
            return json.loads(self.backend.provider_permissions(provider))
        except Exception as e:
            log.warning(f"Could not read {provider} permissions: {e}")
            return {"dbus": [], "filesystem": []}

    def list_providers(self) -> list[dict]:
        """Every account provider the backend registered, so nothing in the UI has to name one.
        Blocking; call from a worker thread."""
        if self.backend is None:
            return []
        try:
            return json.loads(self.backend.list_providers()).get("providers") or []
        except Exception as e:
            log.warning(f"Could not list the account providers: {e}")
            return []

    def desktop_providers(self) -> list[str]:
        """Providers whose accounts come from the desktop - the ones that can need a sandbox
        permission. Derived, never hardcoded, so a new one is covered as soon as it registers."""
        return [p["id"] for p in self.list_providers() if p.get("discoverable")]

    def permission_status(self, provider: str = "") -> dict:
        """What the sandbox still owes one provider, or every desktop one.

        Returns {"state", "commands", "message"} where state is:
          "ok"      - granted, and this process can reach the service
          "restart" - granted, but a `flatpak override` only applies when the sandbox is next
                      set up, so this session still cannot use it
          "missing" - not granted; `commands` says what to run

        Reports only - nothing is requested and no dialog shown - so it is safe on a worker
        thread, which it needs to be: it shells out to `flatpak info` and calls the backend.

        The "restart" state is the reason the provider is asked whether it can reach its
        service *now*, rather than trusting the override alone: `flatpak info` reflects the
        override file the moment it is written, so checking that by itself would report a
        permission as working while the running app still cannot use it.
        """
        if not self.is_flatpak():
            return {"state": "ok", "commands": [], "message": ""}
        manager = gl.flatpak_permission_manager
        commands, blocked = [], []
        for name in ([provider] if provider else self.desktop_providers()):
            needed = self.provider_permissions(name)
            for bus_name in needed.get("dbus") or []:
                if manager.has_dbus_permission(bus_name, "session"):
                    continue
                command = manager.get_dbus_permission_add_command(bus_name, "session")
                if command not in commands:
                    commands.append(command)
            paths = needed.get("filesystem") or []
            if paths:
                command = ("flatpak override --user "
                           + " ".join(f"--filesystem={p}" for p in paths) + f" {manager.app_id}")
                if command not in commands:
                    commands.append(command)
            if not self._provider_reachable(name):
                blocked.append(name)

        if commands:
            return {"state": "missing", "commands": commands,
                    "message": f"Run {'this' if len(commands) == 1 else 'these'} in a terminal, "
                               "then restart StreamController. A permission granted while the "
                               "app is running does not reach it until the next start."}
        if blocked:
            return {"state": "restart", "commands": [],
                    "message": "The permission is granted, but this session started without it. "
                               "Restart StreamController to pick it up."}
        return {"state": "ok", "commands": [],
                "message": "StreamController can reach your desktop's login service."}

    def _provider_reachable(self, provider: str) -> bool:
        if self.backend is None:
            return False
        try:
            return bool(json.loads(self.backend.check_provider_access(provider)).get("ok"))
        except Exception as e:
            log.warning(f"Could not check {provider} access: {e}")
            return False

    def google_start_auth(self, client_id: str, client_secret: str) -> dict:
        """Ask the backend to open a consent flow. Returns {"ok", "flow_id", "auth_url", "error"}."""
        if self.backend is None:
            return {"ok": False, "error": "Calendar backend is still starting, try again in a moment"}
        try:
            return json.loads(self.backend.google_start_auth(
                json.dumps({"client_id": client_id, "client_secret": client_secret})))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def google_poll_auth(self, flow_id: str) -> dict:
        if self.backend is None:
            return {"state": "error", "error": "Calendar backend went away"}
        try:
            return json.loads(self.backend.google_poll_auth(flow_id))
        except Exception as e:
            return {"state": "error", "error": str(e)}

    def google_cancel_auth(self, flow_id: str) -> None:
        if self.backend is None:
            return
        try:
            self.backend.google_cancel_auth(flow_id)
        except Exception as e:
            log.warning(f"Could not cancel the Google authorization: {e}")

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
