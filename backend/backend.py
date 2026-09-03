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
import time
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger as log
from streamcontroller_plugin_tools import BackendBase

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from backend.google_oauth import AuthFlowError, LoopbackFlow, PendingFlow  # noqa: E402
from backend.google_source import GoogleClient, TokenStore  # noqa: E402
from backend.google_source import disconnect as revoke_and_forget  # noqa: E402
from backend.ics_source import fetch_ics, expand_events, load_source  # noqa: E402
from backend.source_errors import AuthError, SourceError  # noqa: E402
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
        # In-progress Google consent flows, keyed by the id the settings UI polls with.
        self._flows: dict[str, tuple[PendingFlow, LoopbackFlow]] = {}
        super().__init__()

    # --- called by the foreground over RPyC ------------------------------------------

    def configure(self, config_json: str) -> None:
        """Replace the calendar configuration and refresh immediately.

        config_json: {"calendars": [{"id", "name", "type", "source", "account_id",
                      "google_calendar", "enabled"}], "refresh_seconds", "days_back",
                      "days_ahead", "cache_dir", "credentials_dir",
                      "google": {"client_id", "client_secret"}}
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
            is_google = calendar.get("type") == "google"
            status = CalendarStatus(calendar_id=calendar_id, fetched_at=datetime.now(timezone.utc))
            try:
                if is_google:
                    events = self._fetch_google(calendar, calendar_id, window_start, window_end)
                    self._write_json_cache(cache_dir, calendar_id, events)
                else:
                    text = fetch_ics(calendar.get("source") or "")
                    events = expand_events(text, calendar_id, window_start, window_end)
                    self._write_cache(cache_dir, calendar_id, text)
            except SourceError as e:
                had_error = True
                status.ok = False
                status.error = str(e)
                status.needs_reauth = isinstance(e, AuthError)
                log.warning(f"Calendar {calendar.get('name') or calendar_id}: {e}")
                events = self._cached_events(is_google, cache_dir, calendar_id, window_start, window_end)
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

    # --- Google Calendar --------------------------------------------------------------

    def _google_settings(self) -> tuple[str, str, str]:
        """(client_id, client_secret, credentials_dir) from the pushed configuration."""
        with self._lock:
            google = dict(self._config.get("google") or {})
            credentials_dir = str(self._config.get("credentials_dir") or "")
        return str(google.get("client_id") or ""), str(google.get("client_secret") or ""), credentials_dir

    def _google_client(self, account_id: str) -> GoogleClient:
        client_id, client_secret, credentials_dir = self._google_settings()
        if not client_id:
            raise AuthError("No Google OAuth client is configured yet - add one in the plugin settings.")
        if not credentials_dir:
            raise SourceError("No credentials directory configured")
        if not account_id:
            raise AuthError("This calendar is not linked to a Google account.")
        return GoogleClient(client_id=client_id, client_secret=client_secret,
                            account_id=account_id, store=TokenStore(credentials_dir))

    def _fetch_google(self, calendar: dict, calendar_id: str,
                      window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        client = self._google_client(str(calendar.get("account_id") or ""))
        return client.fetch_events(str(calendar.get("google_calendar") or ""), calendar_id,
                                   window_start, window_end)

    def google_start_auth(self, request_json: str) -> str:
        """Begin a consent flow. Returns JSON {"ok", "flow_id", "auth_url", "error"}.

        The credentials come in with the request rather than from the pushed config so the
        settings UI can verify a client the user has only just typed in.
        """
        request = json.loads(request_json)
        try:
            flow = LoopbackFlow(str(request.get("client_id") or "").strip(),
                                str(request.get("client_secret") or "").strip())
            auth_url = flow.start()
        except AuthFlowError as e:
            return json.dumps({"ok": False, "error": str(e)})
        except OSError as e:
            return json.dumps({"ok": False, "error": f"Could not open a local port for the reply: {e}"})

        pending = PendingFlow(flow_id=uuid.uuid4().hex, auth_url=auth_url)
        with self._lock:
            self._flows[pending.flow_id] = (pending, flow)
        threading.Thread(
            target=self._run_auth_flow, name="calendar_google_auth", daemon=True,
            args=(flow, pending, str(request.get("client_id") or "").strip(),
                  str(request.get("client_secret") or "").strip()),
        ).start()
        return json.dumps({"ok": True, "flow_id": pending.flow_id, "auth_url": auth_url})

    def _run_auth_flow(self, flow: LoopbackFlow, pending: PendingFlow,
                       client_id: str, client_secret: str) -> None:
        try:
            tokens = flow.run()
            _, _, credentials_dir = self._google_settings()
            if not credentials_dir:
                raise AuthFlowError("No credentials directory configured")
            store = TokenStore(credentials_dir)
            account_id = uuid.uuid4().hex
            store.save(account_id, {
                "refresh_token": tokens["refresh_token"],
                "access_token": tokens.get("access_token", ""),
                "expires_at": time.time() + float(tokens.get("expires_in") or 3600),
            })
            # The primary calendar's id is the account address; naming the account this way
            # keeps the consent screen down to the single calendar.readonly scope.
            email = GoogleClient(client_id, client_secret, account_id, store).account_email()
            data = store.load(account_id)
            data["email"] = email
            store.save(account_id, data)
            pending.account_id = account_id
            pending.email = email
            pending.state = "ok"
            log.info(f"Linked Google account {email}")
        except (AuthFlowError, SourceError) as e:
            pending.error = str(e)
            pending.state = "error"
            log.warning(f"Google authorization failed: {e}")
        except Exception as e:  # never let a surprise kill the thread silently
            log.exception("Google authorization crashed")
            pending.error = f"{e.__class__.__name__}: {e}"
            pending.state = "error"

    def google_poll_auth(self, flow_id: str) -> str:
        """JSON {"state": pending|ok|error|unknown, "email", "account_id", "error"}."""
        with self._lock:
            entry = self._flows.get(flow_id)
        if entry is None:
            return json.dumps({"state": "unknown", "error": "That authorization is no longer running"})
        pending, _flow = entry
        if pending.state in ("ok", "error", "cancelled"):
            with self._lock:
                self._flows.pop(flow_id, None)
        return json.dumps({"state": pending.state, "email": pending.email,
                           "account_id": pending.account_id, "error": pending.error})

    def google_cancel_auth(self, flow_id: str) -> str:
        with self._lock:
            entry = self._flows.pop(flow_id, None)
        if entry is not None:
            pending, flow = entry
            pending.state = "cancelled"
            flow.close()
        return json.dumps({"ok": True})

    def google_list_calendars(self, account_id: str) -> str:
        """JSON {"ok", "calendars": [{"id", "name", "primary", "color", "access_role"}], "error"}."""
        try:
            calendars = self._google_client(account_id).list_calendars()
        except SourceError as e:
            return json.dumps({"ok": False, "calendars": [], "error": str(e)})
        except Exception as e:
            log.exception("Listing Google calendars failed")
            return json.dumps({"ok": False, "calendars": [], "error": f"{e.__class__.__name__}: {e}"})
        return json.dumps({"ok": True, "calendars": calendars, "error": None})

    def google_disconnect(self, account_id: str) -> str:
        client_id, client_secret, credentials_dir = self._google_settings()
        if credentials_dir:
            revoke_and_forget(client_id, client_secret, account_id, TokenStore(credentials_dir))
        return json.dumps({"ok": True})

    # --- last-good-copy cache (so a network blip doesn't blank the deck) ---------------

    @staticmethod
    def _cache_path(cache_dir: str | None, calendar_id: str, suffix: str = ".ics") -> str | None:
        if not cache_dir:
            return None
        digest = hashlib.sha1(calendar_id.encode("utf-8")).hexdigest()[:16]
        return os.path.join(cache_dir, f"{digest}{suffix}")

    def _cached_events(self, is_google: bool, cache_dir: str | None, calendar_id: str,
                       window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        if is_google:
            return self._events_from_json_cache(cache_dir, calendar_id, window_start, window_end)
        return self._events_from_cache(cache_dir, calendar_id, window_start, window_end)

    def _write_json_cache(self, cache_dir: str | None, calendar_id: str,
                          events: list[CalendarEvent]) -> None:
        """The Google path has no raw document to keep, so the mapped events are the cache."""
        path = self._cache_path(cache_dir, calendar_id, ".json")
        if path is None:
            return
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in events], f)
            os.replace(tmp, path)
        except OSError as e:
            log.warning(f"Could not write calendar cache {path}: {e}")

    def _events_from_json_cache(self, cache_dir: str | None, calendar_id: str,
                                window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        path = self._cache_path(cache_dir, calendar_id, ".json")
        if path is None or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            log.warning(f"Could not use cached calendar {path}: {e}")
            return []
        events = []
        for item in raw:
            try:
                event = CalendarEvent.from_dict(item)
            except (KeyError, ValueError):
                continue
            # The cache outlives the window it was written for; re-filter to the current one.
            if event.end > window_start and event.start < window_end:
                events.append(event)
        return events

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
