"""Isolated backend process: fetches and expands the configured calendars.

Runs in the plugin's own venv (see __install__.py) so icalendar / recurring-ical-events never
have to be added to the shared app requirements. Talks to the foreground PluginBase over
RPyC via streamcontroller_plugin_tools.BackendBase.

Two registries do the dispatching: sources.py (a calendar's `type`) and accounts/registry.py
(its `account_provider`). This file only runs the poll loop and the RPyC surface.

Everything crossing the RPyC boundary is JSON text: rpyc proxies plain dict/list arguments
*by reference*, so every field access on the other side would silently round-trip back here.
"""
from __future__ import annotations

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

from backend.accounts.base import AccountProvider  # noqa: E402
from backend.accounts.oauth_google import OAuthGoogleProvider  # noqa: E402
from backend.accounts.registry import DEFAULT_PROVIDER, make_providers  # noqa: E402
from backend.google_oauth import AuthFlowError, LoopbackFlow, PendingFlow  # noqa: E402
from backend.google_source import GoogleClient  # noqa: E402
from backend.source_errors import AuthError, SourceError  # noqa: E402
from backend.sources import SourceContext, classify_account_kind, describe_sources, source_for  # noqa: E402
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
        self._providers: dict[str, AccountProvider] = make_providers()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # In-progress Google consent flows, keyed by the id the settings UI polls with.
        self._flows: dict[str, tuple[PendingFlow, LoopbackFlow]] = {}
        super().__init__()

    # --- called by the foreground over RPyC ------------------------------------------

    def configure(self, config_json: str) -> None:
        """Replace the calendar configuration and refresh immediately.

        config_json: {"calendars": [{"id", "name", "type", "source", "account_provider",
                      "account_id", "google_calendar", "enabled"}], "refresh_seconds",
                      "days_back", "days_ahead", "cache_dir", "credentials_dir",
                      "google": {"client_id", "client_secret"}}
        """
        config = json.loads(config_json)
        with self._lock:
            self._config = config
        for provider in self._providers.values():
            provider.configure(config)
        log.info(f"Configured {len(config.get('calendars', []))} calendar(s), refresh every {self._refresh_seconds()}s")
        self._ensure_thread()
        self._wake.set()

    def refresh_now(self) -> None:
        self._ensure_thread()
        self._wake.set()

    def test_source(self, calendar_json: str) -> str:
        """Synchronously fetch one calendar entry (no caching). Returns JSON
        {"ok", "count", "error", "sample"}."""
        calendar = json.loads(calendar_json)
        calendar.setdefault("id", "test")
        start, end = self._window()
        try:
            events = source_for(calendar).fetch(calendar, start, end, SourceContext(providers=self._providers))
        except SourceError as e:
            return json.dumps({"ok": False, "count": 0, "error": str(e), "sample": []})
        except Exception as e:  # never let a surprise propagate through RPyC as a crash
            log.exception("test_source failed")
            return json.dumps({"ok": False, "count": 0, "error": f"{e.__class__.__name__}: {e}", "sample": []})
        sample = [e.title for e in events[:3]]
        return json.dumps({"ok": True, "count": len(events), "error": None, "sample": sample})

    def describe_sources(self) -> str:
        """What calendar types exist and how each one is added. JSON
        {"sources": [{"id", "label", "needs_account", "manual_fields", "account_kinds",
        "providers"}]}."""
        return json.dumps({"sources": describe_sources(self._providers)})

    def list_accounts(self, provider: str = "") -> str:
        """Accounts the providers can offer to link (all of them, or one), each classified
        against the registered sources. JSON {"ok", "accounts": [{"provider", "id", "label",
        "email", "kind", "calendar_type", "supported", "detail"}], "error"}."""
        names = [provider] if provider else list(self._providers)
        accounts: list[dict] = []
        errors: list[str] = []
        for name in names:
            candidate = self._providers.get(name)
            if candidate is None:
                errors.append(f"Unknown account provider {name!r}")
                continue
            try:
                if candidate.available():
                    for account in candidate.list_accounts():
                        account.calendar_type, account.supported, account.detail = \
                            classify_account_kind(account.kind)
                        accounts.append(account.to_dict())
            except SourceError as e:
                errors.append(f"{name}: {e}")
            except Exception as e:
                log.exception(f"Listing {name} accounts failed")
                errors.append(f"{name}: {e.__class__.__name__}: {e}")
        return json.dumps({"ok": not errors, "accounts": accounts, "error": "; ".join(errors) or None})

    def list_calendars(self, calendar_type: str, provider: str, account_id: str) -> str:
        """JSON {"ok", "calendars": [{"id", "name", "primary", "color", "access_role"}], "error"}."""
        try:
            calendars = source_for({"type": calendar_type}).list_calendars(self._provider(provider), account_id)
        except SourceError as e:
            return json.dumps({"ok": False, "calendars": [], "error": str(e)})
        except Exception as e:
            log.exception("Listing calendars failed")
            return json.dumps({"ok": False, "calendars": [], "error": f"{e.__class__.__name__}: {e}"})
        return json.dumps({"ok": True, "calendars": calendars, "error": None})

    def forget_account(self, provider: str, account_id: str) -> str:
        try:
            self._provider(provider).forget(account_id)
        except SourceError as e:
            return json.dumps({"ok": False, "error": str(e)})
        return json.dumps({"ok": True, "error": None})

    def provider_permissions(self, provider: str) -> str:
        """JSON {"dbus": [...], "filesystem": [...]} - what the Flatpak sandbox must be granted."""
        try:
            return json.dumps(self._provider(provider).required_permissions())
        except SourceError:
            return json.dumps({"dbus": [], "filesystem": []})

    def list_providers(self) -> str:
        """Every registered provider, so the foreground never has to name one itself. JSON
        {"providers": [{"id", "discoverable", "available", "permissions"}]}."""
        providers = []
        for provider_id, provider in self._providers.items():
            try:
                available = provider.available()
            except Exception:
                log.exception(f"Checking whether {provider_id} is available failed")
                available = False
            providers.append({"id": provider_id, "discoverable": provider.discoverable,
                              "available": available,
                              "permissions": provider.required_permissions()})
        return json.dumps({"providers": providers})

    def check_provider_access(self, provider: str) -> str:
        """Whether this process can reach what the provider talks to right now. JSON
        {"ok", "error"}."""
        try:
            ok, error = self._provider(provider).check_access()
        except SourceError as e:
            return json.dumps({"ok": False, "error": str(e)})
        except Exception as e:
            log.exception(f"Checking access for {provider} failed")
            return json.dumps({"ok": False, "error": f"{e.__class__.__name__}: {e}"})
        return json.dumps({"ok": ok, "error": error})

    def on_disconnect(self, conn):
        self._stop.set()
        self._wake.set()
        super().on_disconnect(conn)

    # --- polling -----------------------------------------------------------------------

    def _provider(self, name: str) -> AccountProvider:
        provider = self._providers.get(name or DEFAULT_PROVIDER)
        if provider is None:
            raise AuthError(f"Unknown account provider {name!r}")
        return provider

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
            ctx = SourceContext(providers=self._providers, cache_dir=self._config.get("cache_dir"))
        window_start, window_end = self._window()

        all_events: list[CalendarEvent] = []
        statuses: list[CalendarStatus] = []
        had_error = False
        for calendar in calendars:
            calendar_id = str(calendar.get("id") or "")
            if not calendar_id or not calendar.get("enabled", True):
                continue
            status = CalendarStatus(calendar_id=calendar_id, fetched_at=datetime.now(timezone.utc))
            source = None
            try:
                source = source_for(calendar)
                events = source.fetch(calendar, window_start, window_end, ctx)
            except SourceError as e:
                had_error = True
                status.ok = False
                status.error = str(e)
                status.needs_reauth = isinstance(e, AuthError)
                log.warning(f"Calendar {calendar.get('name') or calendar_id}: {e}")
                events = source.load_cached(calendar, window_start, window_end, ctx) if source else []
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

    # --- Google OAuth consent flow (the one provider that needs a browser round-trip) --------

    def _credentials_dir(self) -> str:
        with self._lock:
            return str(self._config.get("credentials_dir") or "")

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
            provider = OAuthGoogleProvider(client_id, client_secret, self._credentials_dir())
            store = provider.store
            account_id = uuid.uuid4().hex
            store.save(account_id, {
                "refresh_token": tokens["refresh_token"],
                "access_token": tokens.get("access_token", ""),
                "expires_at": time.time() + float(tokens.get("expires_in") or 3600),
            })
            # The primary calendar's id is the account address; naming the account this way
            # keeps the consent screen down to the single calendar.readonly scope.
            email = GoogleClient(provider, account_id).account_email()
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


if __name__ == "__main__":
    CalendarBackend()
