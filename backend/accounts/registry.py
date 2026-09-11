"""Every provider the backend knows about, keyed by the `account_provider` a calendar names.

Adding one: a module implementing `AccountProvider`, one entry here. Nothing else in the
backend, the settings UI's list rendering, or the RPyC surface has to learn about it.
"""
from __future__ import annotations

from backend.accounts.base import AccountProvider
from backend.accounts.oauth_google import OAuthGoogleProvider

DEFAULT_PROVIDER = OAuthGoogleProvider.provider_id

PROVIDER_CLASSES: dict[str, type[AccountProvider]] = {
    OAuthGoogleProvider.provider_id: OAuthGoogleProvider,
}


def make_providers() -> dict[str, AccountProvider]:
    return {provider_id: cls() for provider_id, cls in PROVIDER_CLASSES.items()}
