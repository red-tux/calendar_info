"""Isolated backend process: fetches and expands the configured iCalendar feeds.

Runs in the plugin's own venv (see __install__.py) so icalendar / recurring-ical-events never
have to be added to the shared app requirements. Talks to the foreground PluginBase over
RPyC via streamcontroller_plugin_tools.BackendBase.

Everything crossing the RPyC boundary is JSON text: rpyc proxies plain dict/list arguments
*by reference*, so every field access on the other side would silently round-trip back here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

from loguru import logger as log
from streamcontroller_plugin_tools import BackendBase

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from backend.ics_source import SourceError, fetch_ics, expand_events, load_source  # noqa: E402
from internal.events import CalendarEvent, CalendarStatus  # noqa: E402

DEFAULT_REFRESH_SECONDS = 300
MIN_REFRESH_SECONDS = 60
DEFAULT_DAYS_BACK = 1
DEFAULT_DAYS_AHEAD = 7
RETRY_AFTER_ERROR_SECONDS = 60


class CalendarBackend(BackendBase):
    def __init__(self):
        self._lock = threading.Lock()
        self._config: dict = {"calendars": [], "refresh_seconds": DEFAULT_REFRESH_SECONDS}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        super().__init__()

    # --- called by the foreground over RPyC ------------------------------------------

    def configure(self, config_json: str) -> None:
        """Replace the calendar configuration and refresh immediately.

        config_json: {"calendars": [{"id", "name", "source", "enabled"}], "refresh_seconds",
                      "days_back", "days_ahead", "cache_dir"}
        """
        config = json.loads(config_json)
        with self._lock:
            self._config = config
        log.info(f"Configured {len(config.get('calendars', []))} calendar(s), refresh every {self._refresh_seconds()}s")
        self._ensure_thread()
        self._wake.set()

    def refresh_now(self) -> None:
        self._ensure_thread()
        self._wake.set()

    def test_source(self, source: str) -> str:
        """Synchronously fetch+parse one source. Returns JSON {"ok", "count", "error", "sample"}."""
        start, end = self._window()
        try:
            events = load_source(source, "test", start, end)
        except SourceError as e:
            return json.dumps({"ok": False, "count": 0, "error": str(e), "sample": []})
        except Exception as e:  # never let a surprise propagate through RPyC as a crash
            log.exception("test_source failed")
            return json.dumps({"ok": False, "count": 0, "error": f"{e.__class__.__name__}: {e}", "sample": []})
        sample = [e.title for e in events[:3]]
        return json.dumps({"ok": True, "count": len(events), "error": None, "sample": sample})

    def on_disconnect(self, conn):
        self._stop.set()
        self._wake.set()
        super().on_disconnect(conn)

    # --- polling -----------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll_loop, name="calendar_poll", daemon=True)
        self._thread.start()

    def _refresh_seconds(self) -> int:
        with self._lock:
            value = self._config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)
        try:
            return max(MIN_REFRESH_SECONDS, int(value))
        except (TypeError, ValueError):
            return DEFAULT_REFRESH_SECONDS

    def _window(self) -> tuple[datetime, datetime]:
        with self._lock:
            back = int(self._config.get("days_back", DEFAULT_DAYS_BACK) or DEFAULT_DAYS_BACK)
            ahead = int(self._config.get("days_ahead", DEFAULT_DAYS_AHEAD) or DEFAULT_DAYS_AHEAD)
        now = datetime.now(timezone.utc)
        # Start at the beginning of the earliest day so all-day events on it are included.
        start = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now + timedelta(days=ahead)
        return start, end

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            had_error = False
            try:
                had_error = self._poll_once()
            except Exception:
                log.exception("Calendar poll failed")
                had_error = True
            wait = min(self._refresh_seconds(), RETRY_AFTER_ERROR_SECONDS) if had_error else self._refresh_seconds()
            self._wake.wait(timeout=wait)

    def _poll_once(self) -> bool:
        """Fetch every enabled calendar and push the result. Returns True if any failed."""
        with self._lock:
            calendars = [dict(c) for c in self._config.get("calendars", [])]
            cache_dir = self._config.get("cache_dir")
        window_start, window_end = self._window()

        all_events: list[CalendarEvent] = []
        statuses: list[CalendarStatus] = []
        had_error = False
        for calendar in calendars:
            calendar_id = str(calendar.get("id") or "")
            if not calendar_id or not calendar.get("enabled", True):
                continue
            source = calendar.get("source") or ""
            status = CalendarStatus(calendar_id=calendar_id, fetched_at=datetime.now(timezone.utc))
            try:
                text = fetch_ics(source)
                events = expand_events(text, calendar_id, window_start, window_end)
                self._write_cache(cache_dir, calendar_id, text)
            except SourceError as e:
                had_error = True
                status.ok = False
                status.error = str(e)
                log.warning(f"Calendar {calendar.get('name') or calendar_id}: {e}")
                events = self._events_from_cache(cache_dir, calendar_id, window_start, window_end)
                status.from_cache = bool(events)
            status.event_count = len(events)
            all_events.extend(events)
            statuses.append(status)

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "events": [e.to_dict() for e in all_events],
            "statuses": [s.to_dict() for s in statuses],
        }
        try:
            self.frontend.on_events_update(json.dumps(payload))
        except Exception as e:
            log.error(f"Failed relaying events to frontend: {e}")
        log.info(f"Relayed {len(all_events)} event(s) from {len(statuses)} calendar(s)")
        return had_error

    # --- last-good-copy cache (so a network blip doesn't blank the deck) ---------------

    @staticmethod
    def _cache_path(cache_dir: str | None, calendar_id: str) -> str | None:
        if not cache_dir:
            return None
        digest = hashlib.sha1(calendar_id.encode("utf-8")).hexdigest()[:16]
        return os.path.join(cache_dir, f"{digest}.ics")

    def _write_cache(self, cache_dir: str | None, calendar_id: str, text: str) -> None:
        path = self._cache_path(cache_dir, calendar_id)
        if path is None:
            return
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as e:
            log.warning(f"Could not write calendar cache {path}: {e}")

    def _events_from_cache(self, cache_dir: str | None, calendar_id: str, window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        path = self._cache_path(cache_dir, calendar_id)
        if path is None or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return expand_events(f.read(), calendar_id, window_start, window_end)
        except (OSError, SourceError) as e:
            log.warning(f"Could not use cached calendar {path}: {e}")
            return []


if __name__ == "__main__":
    CalendarBackend()
