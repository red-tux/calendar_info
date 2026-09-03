"""Google OAuth 2.0 for installed apps: PKCE with a loopback redirect on 127.0.0.1.

Backend-only (needs `requests`). The foreground never handles a token: it asks the backend to
start a flow, opens the returned URL in the user's browser, and polls until the backend reports
which account got linked.

Home Assistant has to bounce its redirect through https://my.home-assistant.io/redirect/oauth
because the browser is usually on a different machine than the instance. Here the browser is on
the same machine as this process, so Google's installed-app loopback flow applies: no hosted
redirect, no TLS, and nothing to register in the Cloud Console beyond the client itself.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Read-only for now; the write scope (and an RSVP action) can come later without touching
# the stored refresh token beyond a re-consent.
SCOPE_READONLY = "https://www.googleapis.com/auth/calendar.readonly"

FLOW_TIMEOUT_SECONDS = 300
_EXCHANGE_TIMEOUT = 30

_DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Calendar Info</title><style>
body{{font-family:system-ui,sans-serif;background:#1c1c1c;color:#eee;display:flex;
height:100vh;margin:0;align-items:center;justify-content:center;text-align:center}}
h1{{font-size:1.4rem;margin:0 0 .4rem}}p{{color:#aaa;margin:0}}
.bad h1{{color:#e06c6c}}</style></head>
<body class="{cls}"><div><h1>{heading}</h1><p>{detail}</p></div></body></html>"""


class AuthFlowError(Exception):
    """The consent flow failed. The message is shown to the user verbatim."""


@dataclass
class PendingFlow:
    """One in-progress consent flow. `state` is what the settings UI polls."""
    flow_id: str
    auth_url: str
    state: str = "pending"          # pending | ok | error | cancelled
    email: str = ""
    account_id: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.received = {k: v[0] for k, v in query.items()}  # type: ignore[attr-defined]
        error = self.server.received.get("error")  # type: ignore[attr-defined]
        if error == "access_denied":
            body = _DONE_PAGE.format(cls="bad", heading="Access denied",
                                     detail="You can close this tab and try again.")
        elif error:
            body = _DONE_PAGE.format(cls="bad", heading="Authorization failed", detail=error)
        else:
            body = _DONE_PAGE.format(cls="ok", heading="Calendar Info is connected",
                                     detail="You can close this tab and return to StreamController.")
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        self.server.done.set()  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:
        """Silence BaseHTTPRequestHandler's stderr logging (it would land in the app console)."""


class LoopbackFlow:
    """Authorization-code + PKCE flow served from an ephemeral port on 127.0.0.1."""

    def __init__(self, client_id: str, client_secret: str, scope: str = SCOPE_READONLY):
        if not client_id:
            raise AuthFlowError("No OAuth client ID configured")
        self.client_id = client_id
        self.client_secret = client_secret or ""
        self.scope = scope
        self._verifier = _b64url(os.urandom(64))
        self._state = secrets.token_urlsafe(24)
        self._server: HTTPServer | None = None

    def start(self) -> str:
        """Bind the loopback listener and return the URL to open in the browser."""
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        server.timeout = 1
        server.received = {}
        server.done = threading.Event()
        self._server = server
        self.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        challenge = _b64url(hashlib.sha256(self._verifier.encode("ascii")).digest())
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": self._state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # offline + consent is what guarantees a refresh token, including on a re-link.
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    def run(self, timeout: float = FLOW_TIMEOUT_SECONDS) -> dict:
        """Block until the browser comes back, then exchange the code. Returns the token dict.

        Raises AuthFlowError with a user-facing message on every failure path.
        """
        server = self._server
        if server is None:
            raise AuthFlowError("Flow was not started")
        try:
            deadline = time.monotonic() + timeout
            while not server.done.is_set():
                if time.monotonic() > deadline:
                    raise AuthFlowError("Timed out waiting for Google - the browser tab was never completed")
                server.handle_request()
            received = server.received
        finally:
            self.close()

        if received.get("error") == "access_denied":
            raise AuthFlowError("You declined access in the browser")
        if received.get("error"):
            raise AuthFlowError(f"Google returned '{received['error']}'")
        if received.get("state") != self._state:
            raise AuthFlowError("Mismatched state - the response did not come from the request we started")
        code = received.get("code")
        if not code:
            raise AuthFlowError("Google did not return an authorization code")
        return self._exchange(code)

    def _exchange(self, code: str) -> dict:
        data = {
            "code": code,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": self._verifier,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            response = requests.post(TOKEN_URL, data=data, timeout=_EXCHANGE_TIMEOUT)
        except requests.RequestException as e:
            raise AuthFlowError(f"Could not reach Google to exchange the code: {e}") from e
        payload = _json_or_empty(response)
        if response.status_code != 200:
            raise AuthFlowError(describe_token_error(payload, response.status_code))
        if not payload.get("refresh_token"):
            raise AuthFlowError(
                "Google did not return a refresh token. Remove Calendar Info at "
                "myaccount.google.com/permissions and connect again."
            )
        return payload

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.server_close()
            finally:
                self._server = None


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Trade a refresh token for a new access token. Raises AuthFlowError if it is dead."""
    data = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        response = requests.post(TOKEN_URL, data=data, timeout=_EXCHANGE_TIMEOUT)
    except requests.RequestException as e:
        raise AuthFlowError(f"Could not reach Google to refresh the token: {e}") from e
    payload = _json_or_empty(response)
    if response.status_code != 200:
        raise AuthFlowError(describe_token_error(payload, response.status_code))
    return payload


def revoke(token: str) -> None:
    """Best-effort revoke on disconnect; a dead token is already what we want."""
    try:
        requests.post(REVOKE_URL, data={"token": token}, timeout=_EXCHANGE_TIMEOUT)
    except requests.RequestException:
        pass


def describe_token_error(payload: dict, status_code: int) -> str:
    """Google's token endpoint errors, in words that say what to change.

    These are the ones users actually hit while following the Cloud Console setup, so each
    maps to the step that was missed rather than to the raw OAuth code.
    """
    error = str(payload.get("error") or "")
    description = str(payload.get("error_description") or "")
    if error == "invalid_client":
        return "Google rejected the client ID/secret (invalid_client). Check both values were copied whole."
    if error == "invalid_grant":
        return ("Google rejected the stored authorization (invalid_grant). This is usually a refresh "
                "token expired after 7 days because the OAuth app is still in Testing - publish the "
                "app in the Cloud Console, then connect again.")
    if error == "redirect_uri_mismatch":
        return ("Google rejected the loopback address (redirect_uri_mismatch). The OAuth client must "
                "be of type 'Desktop app'.")
    if error == "unauthorized_client":
        return "This OAuth client is not allowed to use the authorization-code flow. Create a 'Desktop app' client."
    if error:
        return f"Google returned {error}{': ' + description if description else ''}"
    return f"Google returned HTTP {status_code}"


def _json_or_empty(response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
