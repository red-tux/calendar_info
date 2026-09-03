# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

A StreamController plugin (`net_red-tux_calendar_info`, "Calendar Info") that shows calendar
events on a Stream Deck: next event with countdown and color alerts, a browsable agenda, and a
dial variant. Events come from iCalendar (`.ics`) feeds - Google Calendar's private address is
the primary target - or, optionally, from the Google Calendar API; both are fetched and
recurrence-expanded in an isolated backend process.

It is modelled on the sibling plugin [ytmd_controller](https://github.com/red-tux/ytmd_controller);
the framework conventions below are the same ones that plugin documents.

## Running / testing

A plugin only runs inside StreamController. This repo ships a dev container
(`.devcontainer/README.md`) that clones StreamController to `/workspaces/StreamController`,
bind-mounts this repo at `data/plugins/net_red-tux_calendar_info` under it, and persists
`data/` in a volume. Inside it:

```sh
/workspaces/StreamController/run_dev.sh     # == python3 main.py --devel --data data --close-running, with logs teed
```

Plugins are imported once at startup (`importlib.import_module("plugins.<folder>.main")`) - no
hot reload, restart the app after code changes. Logs: `data/logs/logs.log` and
`data/logs/run-console.log`.

The pure-Python parts have real unit tests that need no GTK/StreamController:

```sh
pip install icalendar recurring-ical-events requests    # e.g. in a throwaway venv
python3 -m unittest discover -s tests -t .
```

`tests/` covers `internal/events.py`, `internal/event_store.py`, `backend/ics_source.py` and the
pure parts of the Google path (`map_event`, `TokenStore`, the auth-URL construction and the OAuth
error messages) - the consent flow itself needs a browser and a real client.
Anything that touches `src.backend...` / `GtkHelper` / `gi` (actions, main.py, settings_area.py)
can only be exercised in the app.

## Architecture

### Two processes

- **Foreground** (`main.py`, `actions/`, `settings_area.py`, `internal/event_store.py`) runs in
  StreamController's own Python env. It may only use what the app's `requirements.txt` provides
  (PIL, requests, python-dateutil, GTK via gi, loguru...).
- **Backend** (`backend/backend.py`, `backend/ics_source.py`, `backend/google_source.py`,
  `backend/google_oauth.py`) runs in this plugin's own venv (`__install__.py` +
  `backend_requirements.txt`: icalendar, recurring-ical-events, requests), because the app
  doesn't ship an iCalendar parser. It polls every configured calendar, expands recurrences
  into instances over a window (yesterday .. N days ahead), caches each one's last good result
  under `cache/calendars/` (`.ics` text for feeds, mapped events as `.json` for the API), and
  relays the result to the foreground.
- `internal/events.py` is shared by both and must stay free of gi/StreamController imports.
- **Two calendar sources, one event model.** A calendar entry's `type` is `ics` (an address or
  file in `source`) or `google` (`account_id` + `google_calendar`). `backend.py::_poll_once`
  dispatches on it; both paths produce `CalendarEvent`s, so nothing downstream - event_store,
  the actions, rendering - knows which source an event came from. Add a source by adding a
  module that returns `list[CalendarEvent]`, not by touching anything past the backend.

**Everything crossing RPyC is JSON text** (`backend.configure(json)`, `frontend.on_events_update(json)`,
`backend.test_source(...) -> json`): rpyc proxies dict/list arguments by reference, so field
access on the other side would round-trip back across the connection.

### Google Calendar API

Modelled on Home Assistant's `application_credentials`: **the plugin ships no OAuth client**.
Each user registers a Desktop-app client in their own Google Cloud project and pastes the id and
secret into the settings, which keeps every install clear of Google's verification, brand review,
shared quota and the 100-user cap on unverified apps. Do not add a bundled client ID without
re-reading that trade-off - a shipped unverified client would be capped at 100 users total, and a
verified one commits the project to brand review and re-verification.

- `backend/google_oauth.py` - authorization-code + PKCE against a loopback listener on
  `127.0.0.1:<ephemeral>`. Home Assistant needs its hosted `my.home-assistant.io/redirect/oauth`
  bounce because the browser is on a different machine than the instance; here it isn't, so the
  installed-app loopback flow applies and no redirect URI has to be registered at all.
  `describe_token_error()` maps Google's OAuth errors onto the setup step that was missed.
- `backend/google_source.py` - `TokenStore` (one 0600 JSON file per account under
  `credentials/`, created 0600 rather than chmod-ed afterwards), `GoogleClient` (access-token
  refresh, one forced-refresh retry on a 401, error mapping) and `map_event()`, which turns one
  `events.list` item into a `CalendarEvent`. `singleEvents=true` means Google expands
  recurrences, so `recurring-ical-events` is not involved on this path.
- Account naming uses the primary calendar's id (which is the account's address) instead of
  adding a profile/email scope: the consent screen stays at `calendar.readonly` alone.
- The flow is asynchronous across RPyC: `google_start_auth` returns a `flow_id` plus the URL to
  open, and the settings UI polls `google_poll_auth` until it reports `ok`/`error`. Tokens never
  cross to the foreground - only the account id and address do.

### Entry points

- `main.py` - `CalendarInfoPlugin(PluginBase)`. Builds the shared `EventStore`, caches plugin
  options in `self.options` (`PluginOptions`), registers icon/color assets, adds the three
  `ActionHolder`s, `register()`s, launches the backend on a daemon thread. `register_backend()`
  pushes the calendar config once the backend connects. `on_events_update()` is the single
  ingestion point: parse JSON → `CalendarEvent.from_dict` → attach calendar name/color →
  `event_store.update()`.
- `actions/common/calendar_action_base.py` - `CalendarActionMixin`, mixed in ahead of
  `KeyAction`/`DialAction`. Subscribe/unsubscribe, `render()` scheduling, label/alert settings
  rows, alert-level logic, and all PIL compositing (background color by alert level, calendar
  color stripe, tinted icon assets, progress bar). It also owns the per-action **calendar
  filter** (`calendar_filter` = `all`/`selected` plus `calendar_ids`), which `selected_calendar_ids()`
  turns into the `calendar_ids` argument every `EventStore` query takes. That row is hand-built
  in `get_config_rows()` rather than a GenerativeUI row: the choices are the user's calendars,
  which change at runtime, while GenerativeUI binds one static settings key per widget.
  `get_config_rows()` is re-called each time the sidebar opens the action, so the list stays
  current; a selection naming calendars that no longer exist falls back to "all".
- `actions/common/event_browser.py` - `EventBrowserMixin` (selection by uid, next/previous
  stepping) shared by `Agenda` and `UpcomingDial`.
- `actions/NextEvent`, `actions/Agenda`, `actions/UpcomingDial` - the actions. Each `render()`
  builds a tuple of everything that affects the display, compares it with `_last_render_key`,
  and only pushes to the hardware on change.
- `settings_area.py` - `CalendarSettingsGroup(Adw.PreferencesGroup)`: option rows, the Google
  account section (client id/secret, Connect, linked accounts, the setup-guide dialog and the
  `calendarList`-driven calendar picker), then the calendar list of `CalendarRow(Adw.ExpanderRow)`;
  must stay a single `PreferencesGroup` because the app adds it to an `Adw.PreferencesPage`.
  `CalendarRow` renders a Google entry without the address/Test rows - there is nothing to type.

### Threading model

- `EventStore` fans out to subscribers through `GLib.idle_add`, so `on_events_changed()` runs on
  the GTK main thread. `on_ready()` is main-thread. `on_tick()` runs once a second on a
  fresh thread. Event-assigner callbacks run on per-event threads.
- Every `set_media()`/`set_label()` goes through `CalendarActionMixin.ui()` (`GLib.idle_add`).
  Actions render from `on_tick()` (countdowns change every minute, the urgent flash every
  second) and from `on_events_changed()`; the only shared mutable state is the render-dedup
  key, a single attribute assignment, so no locks. A duplicate push in a race is harmless.
- `render()` must bail while `on_ready_called` is False (`can_render()`) - the framework's
  `set_media`/`set_label` raise before `on_ready()`.
- All-day events are pinned to *local* midnight on the foreground (`CalendarEvent.from_dict`);
  timed events are UTC-aware throughout. Never compare naive and aware datetimes.
- **Display timezone.** Events carry `tzid` (the `.ics` `TZID` / Google `timeZone` of the
  instance) purely so the `display_timezone` plugin option can offer "the event's own zone"
  alongside local, UTC and any IANA name; `resolve_tz(mode, event)` turns the option into a
  tzinfo and `CalendarActionMixin.display_tz()` is what every label path uses. Because all-day
  events are pinned to *local* midnight, converting them into another display zone would move
  them a day - format them with `format_event_day()`, which keeps their own date and only
  evaluates "today" in the display zone. Countdowns are durations and take no zone at all.

### Lifecycle handling

`on_ready()` re-fires on every page (re)load on the *same* action instance, and the core
clears the input's image just before. So the mixin subscribes idempotently (`_store_token`
guard), resets the render cache, and repaints. `on_disconnect()` unsubscribes and calls
`super().on_disconnect()` (the base does the RPyC teardown).

### Settings scopes

- **Plugin-level** (`PluginBase.get_settings()/set_settings()`): `calendars` (list of
  `{id, name, type, source, account_id, google_calendar, enabled, color}`), `google`
  (`{client_id, client_secret, accounts: [{id, email}]}` - credentials and account *names* only,
  never tokens), `refresh_minutes`, `days_ahead`, `time_format`, `hide_all_day`. UI is hand-built in `settings_area.py`; any change goes through
  `CalendarInfoPlugin.on_settings_changed()` which refreshes `self.options`, re-pushes the
  backend config, and re-decorates cached events. Actions read `plugin_base.options`, never
  the JSON file, on the tick path.
- **Per-action** (page JSON, via `GtkHelper/GenerativeUI` rows): labels, alert thresholds,
  browse scope, etc. `row.get_value(fallback=...)` returns the literal fallback when unset -
  always pass the row's own default (the typed accessors in the mixin do).

### Shared per-instance state

`EventStore` owns `dismiss(uid)` (silence the alert, keep showing the event) and `skip(uid)`
(hide it from "next"), so every key/dial agrees; marks are pruned when the instance ends.

### Assets

Icons are pre-rendered 512x512 PNGs of Material Icons (`assets/icons/material/`, see
`NOTICE.md`), registered via `add_icon()`; colors via `add_color()`. Both are user-overridable
in the plugin's Settings → Assets/Colors. PNGs rather than SVGs because the app's SVG loader
rasterizes icon assets to a squashed 1024x96 (a known core bug; see ytmd_controller's
`docs/observed-core-bugs.md`). The icon's alpha is used as a mask and tinted with the
`icon_color` asset at render time (`paste_asset_icon`).

Text on keys is done with the framework's labels (`set_top_label` etc.), never drawn with PIL,
to keep fonts/outline/size user-editable and to avoid the PIL/FreeType conflict the dev
container works around.

## Conventions

- Plugin id (`manifest.json` `id`) and folder name must match: `net_red-tux_calendar_info`.
  Action ids are `<plugin_id>::<ActionHolder action_id_suffix>`.
- Keep `action_name` free of `&`, `<`, `>` (core bug: unescaped Pango markup in the sidebar).
  The same applies to every `Adw` row title/subtitle built by hand - in `settings_area.py` and
  in the actions' `get_config_rows()` - which libadwaita parses as markup: a literal
  `<your app>`, or a calendar named "Personal & Family", silently blanks the row's text. Run
  user-supplied names through `GLib.markup_escape_text()`.
- Don't add foreground imports beyond the app's `requirements.txt`; new third-party needs go
  in `backend_requirements.txt` and run in the backend.
- Framework source lives in the StreamController checkout, not here: chiefly
  `src/backend/PluginManager/` (`PluginBase.py`, `ActionCore.py`, `InputBases.py`,
  `EventAssigner.py`) and `GtkHelper/GenerativeUI/`.
