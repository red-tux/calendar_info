"""The credential-provider seam: OAuthGoogleProvider on top of TokenStore, and GoogleClient
asking a provider rather than refreshing tokens itself."""
import os
import tempfile
import time
import unittest
from unittest import mock

from backend.accounts import oauth_google
from backend.accounts.base import KIND_BEARER, AccountProvider, Credential
from backend.accounts.oauth_google import OAuthGoogleProvider
from backend.accounts.registry import DEFAULT_PROVIDER, PROVIDER_CLASSES, make_providers
from backend.google_source import GoogleClient
from backend.source_errors import AuthError


class OAuthGoogleProviderTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.provider = OAuthGoogleProvider("client", "secret", os.path.join(self._dir.name, "credentials"))
        self.provider.store.save("acc", {"refresh_token": "r", "access_token": "a",
                                         "expires_at": time.time() + 3600, "email": "me@example.com"})

    def tearDown(self):
        self._dir.cleanup()

    def test_valid_cached_token_is_reused(self):
        with mock.patch.object(oauth_google, "refresh_access_token") as refresh:
            credential = self.provider.get_credential("acc")
        refresh.assert_not_called()
        self.assertEqual(credential.kind, KIND_BEARER)
        self.assertEqual(credential.token, "a")

    def test_expired_token_is_refreshed_and_saved(self):
        data = self.provider.store.load("acc")
        data["expires_at"] = time.time() - 10
        self.provider.store.save("acc", data)
        with mock.patch.object(oauth_google, "refresh_access_token",
                               return_value={"access_token": "b", "expires_in": 100}) as refresh:
            credential = self.provider.get_credential("acc")
        refresh.assert_called_once_with("client", "secret", "r")
        self.assertEqual(credential.token, "b")
        self.assertEqual(self.provider.store.load("acc")["access_token"], "b")

    def test_force_refresh_ignores_cached_token(self):
        with mock.patch.object(oauth_google, "refresh_access_token",
                               return_value={"access_token": "c", "expires_in": 100}):
            self.assertEqual(self.provider.get_credential("acc", force_refresh=True).token, "c")

    def test_configure_reads_the_backend_config(self):
        provider = OAuthGoogleProvider()
        provider.configure({"google": {"client_id": "x", "client_secret": "y"}, "credentials_dir": "/tmp/c"})
        self.assertEqual((provider.client_id, provider.client_secret, provider.credentials_dir), ("x", "y", "/tmp/c"))

    def test_missing_client_or_account_is_an_auth_error(self):
        with self.assertRaises(AuthError):
            OAuthGoogleProvider("", "", self._dir.name).get_credential("acc")
        with self.assertRaises(AuthError):
            self.provider.get_credential("")
        with self.assertRaises(AuthError):
            self.provider.get_credential("never-linked")

    def test_forget_revokes_and_deletes(self):
        with mock.patch.object(oauth_google, "revoke") as revoke:
            self.provider.forget("acc")
        revoke.assert_called_once_with("r")
        with self.assertRaises(AuthError):
            self.provider.store.load("acc")

    def test_registry_defaults_to_oauth(self):
        self.assertIn(DEFAULT_PROVIDER, PROVIDER_CLASSES)
        providers = make_providers()
        self.assertIsInstance(providers[DEFAULT_PROVIDER], OAuthGoogleProvider)
        for provider_id, provider in providers.items():
            self.assertEqual(provider.provider_id, provider_id)


class _FakeProvider(AccountProvider):
    provider_id = "fake"

    def __init__(self, token="tok"):
        self.token = token
        self.calls = []

    def get_credential(self, account_id, force_refresh=False):
        self.calls.append((account_id, force_refresh))
        return Credential(KIND_BEARER, token=self.token)


def _response(status_code, payload):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class GoogleClientProviderTests(unittest.TestCase):
    def test_bearer_token_comes_from_the_provider(self):
        provider = _FakeProvider()
        with mock.patch("backend.google_source.requests.get", return_value=_response(200, {"id": "me@example.com"})) as get:
            self.assertEqual(GoogleClient(provider, "acc").account_email(), "me@example.com")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})
        self.assertEqual(provider.calls, [("acc", False)])

    def test_401_retries_once_with_a_forced_refresh(self):
        provider = _FakeProvider()
        with mock.patch("backend.google_source.requests.get",
                        side_effect=[_response(401, {}), _response(200, {"id": "me@example.com"})]):
            self.assertEqual(GoogleClient(provider, "acc").account_email(), "me@example.com")
        self.assertEqual(provider.calls, [("acc", False), ("acc", True)])

    def test_non_bearer_credential_is_rejected(self):
        class Basic(AccountProvider):
            def get_credential(self, account_id, force_refresh=False):
                return Credential("basic", username="u", password="p")

        with self.assertRaises(AuthError):
            GoogleClient(Basic(), "acc").account_email()


if __name__ == "__main__":
    unittest.main()
