"""KDE Online Accounts (Accounts-SSO): accounts from libaccounts-glib, tokens from signond.

The refresh token never leaves the desktop: for every request signond is asked over D-Bus for
a short-lived access token, with UiPolicy set so a dead login fails instead of popping a
dialog. Nothing is stored here.

Two ways to read the account list, tried in that order:
  - the `Accounts-1.0` GI binding (libaccounts-glib itself), when its typelib is installed;
  - the on-disk database (`accounts.db`, SQLite) plus the `.provider` XML templates, with
    stdlib only. That's what runs inside the Flatpak, where the typelib isn't available.
Both yield the same (credentials id, method, mechanism, parameters) that signond's
`AuthSession.process` takes. The D-Bus side needs only Gio, which the backend venv gets from
the app (see __install__.py).
"""
from __future__ import annotations

import ast
import os
import sqlite3
import time
import xml.etree.ElementTree as ET

from backend.accounts.base import KIND_BASIC, KIND_BEARER, AccountInfo, AccountProvider, Credential
from backend.source_errors import AuthError, SourceError

SIGNOND_NAME = "com.google.code.AccountsSSO.SingleSignOn"
SIGNOND_PATH = "/com/google/code/AccountsSSO/SingleSignOn"
SIGNOND_AUTH_SERVICE = SIGNOND_NAME + ".AuthService"
SIGNOND_AUTH_SESSION = SIGNOND_NAME + ".AuthSession"
# SignOn::UiPolicy: 0 default, 1 request password, 2 no user interaction, 3 validation.
UI_POLICY_NO_INTERACTION = 2
# The host session bus (set by the KDE dev container); unset means this process's own bus,
# which in a Flatpak is the proxied host bus once the talk-name has been granted.
BUS_ADDRESS_ENV = "CALENDAR_INFO_ACCOUNTS_DBUS_ADDRESS"
DEFAULT_PROVIDERS_DIR = "/usr/share/accounts/providers"
_DBUS_TIMEOUT_MS = 60_000
_EXPIRY_SKEW_SECONDS = 60
# Accounts-SSO providers whose token a calendar source here can use. CalDAV would add
# nextcloud/owncloud (password method -> basic credential).
SUPPORTED_PROVIDERS = ("google",)


class _GiUnavailable(Exception):
    """The Accounts-1.0 typelib can't be loaded; use the on-disk fallback."""


def parse_variant_text(type_string: str, text: str):
    """A GVariant in g_variant_print() form, as libaccounts-glib stores settings and as the
    .provider XML spells them: 'str', ['a', 'b'], true, 42. Strings and string arrays are
    Python literal syntax; only booleans differ."""
    text = (text or "").strip()
    if type_string == "b":
        return text.lower() == "true"
    if type_string in ("s", "as"):
        try:
            value = ast.literal_eval(text) if text else ""
        except (ValueError, SyntaxError):
            return text if type_string == "s" else []
        if type_string == "s":
            return value if isinstance(value, str) else text
        return [str(v) for v in value] if isinstance(value, (list, tuple)) else []
    if type_string in ("u", "i", "x", "t", "n", "q", "y"):
        try:
            return int(text)
        except ValueError:
            return 0
    return None


def default_db_path() -> str:
    # libaccounts-glib: $ACCOUNTS names the *parent* of its libaccounts-glib directory,
    # otherwise the XDG config dir.
    base = os.environ.get("ACCOUNTS") or os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "libaccounts-glib", "accounts.db")


class KdeAccountsProvider(AccountProvider):
    provider_id = "kde"

    def __init__(self, db_path: str | None = None, providers_dir: str | None = None,
                 bus_address: str | None = None, use_gi: bool = True):
        self.db_path = db_path or default_db_path()
        self.providers_dir = providers_dir or os.environ.get("AG_PROVIDERS") or DEFAULT_PROVIDERS_DIR
        self.bus_address = bus_address if bus_address is not None else os.environ.get(BUS_ADDRESS_ENV, "")
        self.use_gi = use_gi
        self._tokens: dict[str, Credential] = {}
        self._connection = None

    def available(self) -> bool:
        return os.path.isfile(self.db_path)

    def required_permissions(self) -> dict:
        return {"dbus": [SIGNOND_NAME],
                "filesystem": ["xdg-config/libaccounts-glib:ro", "/usr/share/accounts:ro"]}

    # --- discovery ---------------------------------------------------------------------

    def list_accounts(self) -> list[AccountInfo]:
        try:
            rows = self._accounts_via_gi()
        except _GiUnavailable:
            rows = self._accounts_via_db()
        return [AccountInfo(self.provider_id, str(account_id), label=name)
                for account_id, name, ag_provider, enabled in rows
                if enabled and ag_provider in SUPPORTED_PROVIDERS]

    def _accounts_via_gi(self) -> list[tuple[int, str, str, bool]]:
        Accounts = _gi_accounts(self.use_gi)
        manager = Accounts.Manager.new()
        rows = []
        for account_id in manager.list():
            account = manager.get_account(account_id)
            if account is None:
                continue
            rows.append((int(account_id), account.get_display_name() or "",
                         account.get_provider_name() or "", bool(account.get_enabled())))
        return rows

    def _accounts_via_db(self) -> list[tuple[int, str, str, bool]]:
        with self._db() as conn:
            return [(int(i), str(n or ""), str(p or ""), bool(e))
                    for i, n, p, e in conn.execute("SELECT id, name, provider, enabled FROM Accounts")]

    # --- what signond needs for one account ------------------------------------------------

    def _auth_data(self, account_id: str) -> tuple[int, str, str, dict]:
        """(credentials id, method, mechanism, session parameters)."""
        try:
            return self._auth_data_via_gi(account_id)
        except _GiUnavailable:
            return self._auth_data_via_db(account_id)

    def _auth_data_via_gi(self, account_id: str) -> tuple[int, str, str, dict]:
        Accounts = _gi_accounts(self.use_gi)
        account = Accounts.Manager.new().get_account(int(account_id))
        if account is None:
            raise AuthError(f"KDE account {account_id} no longer exists.")
        auth = Accounts.AccountService.new(account, None).get_auth_data()
        params = auth.get_login_parameters(None)
        return (int(auth.get_credentials_id()), auth.get_method() or "", auth.get_mechanism() or "",
                dict(params.unpack()) if params is not None else {})

    def _auth_data_via_db(self, account_id: str) -> tuple[int, str, str, dict]:
        with self._db() as conn:
            row = conn.execute("SELECT provider FROM Accounts WHERE id = ?", (int(account_id),)).fetchone()
            if row is None:
                raise AuthError(f"KDE account {account_id} no longer exists.")
            settings = {key: parse_variant_text(type_string, value)
                        for key, type_string, value in conn.execute(
                            "SELECT key, type, value FROM Settings WHERE account = ? AND (service = 0 OR service IS NULL)",
                            (int(account_id),))}
        # The account's own values win over the provider template, as in libaccounts-glib.
        merged = self._template_settings(str(row[0]))
        merged.update(settings)
        method = str(merged.get("auth/method") or "")
        mechanism = str(merged.get("auth/mechanism") or "")
        if not method or not mechanism:
            raise AuthError(f"KDE account {account_id} has no login method configured.")
        prefix = f"auth/{method}/{mechanism}/"
        params = {key[len(prefix):]: value for key, value in merged.items()
                  if key.startswith(prefix) and value is not None}
        credentials_id = merged.get("CredentialsId")
        if not credentials_id:
            raise AuthError(f"KDE account {account_id} has no stored login (CredentialsId).")
        return int(credentials_id), method, mechanism, params

    def _template_settings(self, ag_provider: str) -> dict:
        """`auth/...` settings from the provider's .provider XML template. KDE installs its
        definitions in a subdirectory of the providers dir, so one level down is searched too."""
        candidates = [os.path.join(self.providers_dir, f"{ag_provider}.provider")]
        try:
            for entry in sorted(os.listdir(self.providers_dir)):
                candidates.append(os.path.join(self.providers_dir, entry, f"{ag_provider}.provider"))
        except OSError:
            pass
        for path in candidates:
            if os.path.isfile(path):
                return _parse_template(path)
        return {}

    def _db(self) -> sqlite3.Connection:
        if not os.path.isfile(self.db_path):
            raise SourceError(f"No KDE account database at {self.db_path}")
        last_error = None
        # The database is normally reachable read-only; on a read-only mount SQLite may
        # additionally need immutable=1 (it cannot create the WAL index file there).
        for query in ("mode=ro", "mode=ro&immutable=1"):
            try:
                conn = sqlite3.connect(f"file:{self.db_path}?{query}", uri=True)
                conn.execute("SELECT 1 FROM Accounts LIMIT 1")
                return conn
            except sqlite3.Error as e:
                last_error = e
        raise SourceError(f"Could not read the KDE account database: {last_error}")

    # --- tokens ----------------------------------------------------------------------------

    def get_credential(self, account_id: str, force_refresh: bool = False) -> Credential:
        if not account_id:
            raise AuthError("This calendar is not linked to a KDE account.")
        cached = self._tokens.get(account_id)
        if cached is not None and not force_refresh and time.time() < cached.expires_at - _EXPIRY_SKEW_SECONDS:
            return cached
        credentials_id, method, mechanism, params = self._auth_data(account_id)
        reply = self._request_token(credentials_id, method, mechanism, params)
        credential = _credential_from_reply(reply)
        if credential.kind == KIND_BEARER:
            self._tokens[account_id] = credential
        return credential

    def forget(self, account_id: str) -> None:
        self._tokens.pop(account_id, None)

    def _request_token(self, credentials_id: int, method: str, mechanism: str, params: dict) -> dict:
        """One AuthSession.process() round trip. Raises AuthError with signond's message."""
        from gi.repository import Gio, GLib  # the app's PyGObject, via the venv's .pth

        session = {key: variant for key, variant in ((k, _to_variant(GLib, v)) for k, v in params.items())
                   if variant is not None}
        session["UiPolicy"] = GLib.Variant("u", UI_POLICY_NO_INTERACTION)
        try:
            connection = self._bus(Gio)
            session_path = connection.call_sync(
                SIGNOND_NAME, SIGNOND_PATH, SIGNOND_AUTH_SERVICE, "getAuthSessionObjectPath",
                GLib.Variant("(uss)", (credentials_id, "", method)),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MS, None).unpack()[0]
            try:
                reply = connection.call_sync(
                    SIGNOND_NAME, session_path, SIGNOND_AUTH_SESSION, "process",
                    GLib.Variant("(a{sv}s)", (session, mechanism)),
                    GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MS, None).unpack()[0]
            finally:
                try:
                    connection.call_sync(SIGNOND_NAME, session_path, SIGNOND_AUTH_SESSION, "objectUnref",
                                         None, None, Gio.DBusCallFlags.NONE, 5000, None)
                except GLib.Error:
                    pass
        except GLib.Error as e:
            raise AuthError(
                f"KDE Online Accounts could not provide a login: {e.message}. If this keeps "
                "happening, re-authenticate the account in System Settings -> Online Accounts."
            ) from e
        return dict(reply)

    def _bus(self, Gio):
        if self._connection is None:
            if self.bus_address:
                self._connection = Gio.DBusConnection.new_for_address_sync(
                    self.bus_address,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None, None)
            else:
                self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._connection


def _gi_accounts(use_gi: bool):
    if not use_gi:
        raise _GiUnavailable()
    try:
        import gi
        gi.require_version("Accounts", "1.0")
        from gi.repository import Accounts
    except (ImportError, ValueError) as e:
        raise _GiUnavailable(str(e)) from e
    return Accounts


def _parse_template(path: str) -> dict:
    """Flatten <template><group name="auth">...</group></template> into
    {"auth/method": ..., "auth/oauth2/web_server/Scope": [...], ...}."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {}
    settings: dict = {}

    def walk(element, prefix):
        for child in element:
            name = child.get("name") or ""
            if child.tag == "group":
                walk(child, f"{prefix}{name}/")
            elif child.tag == "setting":
                settings[f"{prefix}{name}"] = parse_variant_text(child.get("type") or "s", child.text or "")

    template = root.find("template")
    if template is not None:
        walk(template, "")
    return settings


def _to_variant(GLib, value):
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("u", value)
    if isinstance(value, str):
        return GLib.Variant("s", value)
    if isinstance(value, (list, tuple)):
        return GLib.Variant("as", [str(v) for v in value])
    return None


def _credential_from_reply(reply: dict) -> Credential:
    """signond's oauth2 plugin answers {AccessToken, ExpiresIn, ...}; the password plugin
    {UserName, Secret}. Either becomes the matching Credential."""
    token = str(reply.get("AccessToken") or "")
    if token:
        try:
            expires_in = float(reply.get("ExpiresIn") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        return Credential(KIND_BEARER, token=token, expires_at=time.time() + expires_in)
    if reply.get("Secret") or reply.get("UserName"):
        return Credential(KIND_BASIC, username=str(reply.get("UserName") or ""),
                          password=str(reply.get("Secret") or ""))
    raise AuthError("KDE Online Accounts answered without a token. Re-authenticate the account in "
                    "System Settings -> Online Accounts.")
