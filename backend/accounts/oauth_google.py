"""Google via the user's own OAuth Desktop-app client.

Modelled on Home Assistant's application_credentials: the plugin ships no client, each user
registers one in their own Google Cloud project. Refresh tokens live in `TokenStore`, one 0600
file per account; linking happens through the consent flow in google_oauth.py, so there is
nothing for `list_accounts()` to discover.
"""
from __future__ import annotations

import time

from backend.accounts.base import KIND_BEARER, AccountInfo, AccountProvider, Credential
from backend.google_oauth import AuthFlowError, refresh_access_token, revoke
from backend.google_source import TokenStore
from backend.source_errors import AuthError, SourceError

_EXPIRY_SKEW_SECONDS = 60


class OAuthGoogleProvider(AccountProvider):
    provider_id = "oauth"
    calendar_types = ("google",)

    def __init__(self, client_id: str = "", client_secret: str = "", credentials_dir: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials_dir = credentials_dir

    def configure(self, config: dict) -> None:
        google = dict(config.get("google") or {})
        self.client_id = str(google.get("client_id") or "")
        self.client_secret = str(google.get("client_secret") or "")
        self.credentials_dir = str(config.get("credentials_dir") or "")

    @property
    def store(self) -> TokenStore:
        if not self.credentials_dir:
            raise SourceError("No credentials directory configured")
        return TokenStore(self.credentials_dir)

    def list_accounts(self) -> list[AccountInfo]:
        return []

    def get_credential(self, account_id: str, force_refresh: bool = False) -> Credential:
        if not self.client_id:
            raise AuthError("No Google OAuth client is configured yet - add one in the plugin settings.")
        store = self.store
        if not account_id:
            raise AuthError("This calendar is not linked to a Google account.")
        data = store.load(account_id)
        token = data.get("access_token") or ""
        expires_at = float(data.get("expires_at") or 0)
        if token and not force_refresh and time.time() < expires_at - _EXPIRY_SKEW_SECONDS:
            return Credential(KIND_BEARER, token=token, expires_at=expires_at)
        try:
            fresh = refresh_access_token(self.client_id, self.client_secret, data["refresh_token"])
        except AuthFlowError as e:
            raise AuthError(str(e)) from e
        data["access_token"] = fresh.get("access_token", "")
        data["expires_at"] = time.time() + float(fresh.get("expires_in") or 3600)
        # A rotated refresh token is only sent sometimes; keep the old one otherwise.
        if fresh.get("refresh_token"):
            data["refresh_token"] = fresh["refresh_token"]
        store.save(account_id, data)
        return Credential(KIND_BEARER, token=data["access_token"], expires_at=data["expires_at"])

    def forget(self, account_id: str) -> None:
        """Revoke what we can, then drop the stored token either way."""
        if not self.credentials_dir:
            return
        store = self.store
        try:
            data = store.load(account_id)
        except AuthError:
            data = {}
        token = data.get("refresh_token") or data.get("access_token")
        if token:
            revoke(token)
        store.delete(account_id)
