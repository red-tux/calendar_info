# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

A StreamController plugin (`net_red-tux_calendar_info`, "Calendar Info") that shows calendar
events on a Stream Deck: next event with countdown and color alerts, a browsable agenda, and a
dial variant. Events come from iCalendar (`.ics`) feeds - Google Calendar's private address is
the primary target - fetched and recurrence-expanded in an isolated backend process.

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

`tests/` covers `internal/events.py`, `internal/event_store.py` and `backend/ics_source.py`.
Anything that touches `src.backend...` / `GtkHelper` / `gi` (actions, main.py, settings_area.py)
can only be exercised in the app.

## Architecture

### Two processes

- **Foreground** (`main.py`, `actions/`, `settings_area.py`, `internal/event_store.py`) runs in
  StreamController's own Python env. It may only use what the app's `requirements.txt` provides
  (PIL, requests, python-dateutil, GTK via gi, loguru...).
- **Backend** (`backend/backend.py`, `backend/ics_source.py`) runs in this plugin's own venv
  (`__install__.py` + `backend_requirements.txt`: icalendar, recurring-ical-events, requests),
  because the app doesn't ship an iCalendar parser. It polls every configured feed, expands
  recurrences into instances over a window (yesterday .. N days ahead), caches each feed's
  last good download under `cache/calendars/`, and relays the result to the foreground.
- `internal/events.py` is shared by both and must stay free of gi/StreamController imports.

**Everything crossing RPyC is JSON text** (`backend.configure(json)`, `frontend.on_events_update(json)`,
`backend.test_source(...) -> json`): rpyc proxies dict/list arguments by reference, so field
access on the other side would round-trip back across the connection.

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
  color stripe, tinted icon assets, progress bar).
- `actions/common/event_browser.py` - `EventBrowserMixin` (selection by uid, next/previous
  stepping) shared by `Agenda` and `UpcomingDial`.
- `actions/NextEvent`, `actions/Agenda`, `actions/UpcomingDial` - the actions. Each `render()`
  builds a tuple of everything that affects the display, compares it with `_last_render_key`,
  and only pushes to the hardware on change.
- `settings_area.py` - `CalendarSettingsGroup(Adw.PreferencesGroup)`: option rows plus a
  calendar list of `CalendarRow(Adw.ExpanderRow)`; must stay a single `PreferencesGroup`
  because the app adds it to an `Adw.PreferencesPage`.

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

### Lifecycle handling

`on_ready()` re-fires on every page (re)load on the *same* action instance, and the core
clears the input's image just before. So the mixin subscribes idempotently (`_store_token`
guard), resets the render cache, and repaints. `on_disconnect()` unsubscribes and calls
`super().on_disconnect()` (the base does the RPyC teardown).

### Settings scopes

- **Plugin-level** (`PluginBase.get_settings()/set_settings()`): `calendars` (list of
  `{id, name, source, enabled, color}`), `refresh_minutes`, `days_ahead`, `time_format`,
  `hide_all_day`. UI is hand-built in `settings_area.py`; any change goes through
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
- Don't add foreground imports beyond the app's `requirements.txt`; new third-party needs go
  in `backend_requirements.txt` and run in the backend.
- Framework source lives in the StreamController checkout, not here: chiefly
  `src/backend/PluginManager/` (`PluginBase.py`, `ActionCore.py`, `InputBases.py`,
  `EventAssigner.py`) and `GtkHelper/GenerativeUI/`.
