from __future__ import annotations

from dataclasses import dataclass

KIND_BEARER = "bearer"
KIND_BASIC = "basic"


@dataclass
class Credential:
    """What a source puts on the wire: a bearer token, or a username/password pair."""
    kind: str
    token: str = ""
    username: str = ""
    password: str = ""
    expires_at: float = 0.0


@dataclass
class AccountInfo:
    """An account a provider can offer to link. Never carries a secret.

    The provider fills in `kind` - what the account *is* ("google", "dav", ...) - and the
    backend resolves the rest against the registered sources, so an account type nothing can
    read yet is still offered and explained rather than silently dropped.
    """
    provider: str
    id: str
    label: str = ""
    email: str = ""
    kind: str = ""
    calendar_type: str = ""
    supported: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {"provider": self.provider, "id": self.id, "label": self.label, "email": self.email,
                "kind": self.kind, "calendar_type": self.calendar_type,
                "supported": self.supported, "detail": self.detail}


class AccountProvider:
    """Interface every provider implements; the backend keeps one instance per provider."""

    provider_id = ""
    # True when accounts come from somewhere outside this plugin (the desktop) and are found by
    # `list_accounts()`; False when linking *is* a flow of our own, as OAuth consent is.
    discoverable = False
    # Which calendar types a non-discoverable provider can authenticate, for the manual add
    # choices. Discoverable providers say nothing here - what they serve is per account.
    calendar_types: tuple[str, ...] = ()

    def configure(self, config: dict) -> None:
        """Called with the full backend configuration whenever it is (re)pushed."""

    def available(self) -> bool:
        """False when the provider can't work here (no desktop daemon, no library) - it is
        then simply absent from discovery, which is what most users should see."""
        return True

    def list_accounts(self) -> list[AccountInfo]:
        """Accounts this provider can offer to link. Desktop providers discover them; the
        OAuth provider has nothing to discover (linking is its consent flow)."""
        return []

    def get_credential(self, account_id: str, force_refresh: bool = False) -> Credential:
        raise NotImplementedError

    def forget(self, account_id: str) -> None:
        """Revoke/delete whatever this provider stored for the account. No-op if nothing."""

    def required_permissions(self) -> dict:
        """Flatpak sandbox permissions the provider needs beyond the app's own:
        {"dbus": [session bus names], "filesystem": [paths, with :ro where enough]}."""
        return {"dbus": [], "filesystem": []}

    def check_access(self) -> tuple[bool, str]:
        """Whether what this provider talks to can be reached *right now*, and why not.

        A granted `flatpak override` only takes effect when the sandbox is next set up, so
        checking the override alone would call a permission working while this process still
        cannot use it. Providers that talk to nothing are always reachable.
        """
        return True, ""
