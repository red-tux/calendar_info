"""The KDE provider's stdlib fallback: accounts.db + .provider XML -> what signond needs,
and the reply -> Credential mapping. The GI path and the D-Bus call need a desktop."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from backend.accounts.base import KIND_BASIC, KIND_BEARER
from backend.accounts.kde import (
    SIGNOND_NAME,
    KdeAccountsProvider,
    _credential_from_reply,
    parse_variant_text,
)
from backend.accounts.registry import PROVIDER_CLASSES
from backend.source_errors import AuthError, SourceError

SCHEMA = [
    "CREATE TABLE Accounts (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,provider TEXT,enabled INTEGER)",
    "CREATE TABLE Services (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,display TEXT NOT NULL,provider TEXT,type TEXT)",
    "CREATE TABLE Settings (account INTEGER NOT NULL,service INTEGER,key TEXT NOT NULL,type TEXT NOT NULL,value BLOB)",
]

GOOGLE_PROVIDER_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<provider id="google">
  <name>Google</name>
  <template>
    <group name="auth">
      <setting name="method">oauth2</setting>
      <setting name="mechanism">web_server</setting>
      <group name="oauth2">
        <group name="web_server">
          <setting name="Host">accounts.google.com</setting>
          <setting name="Scope" type="as">['https://www.googleapis.com/auth/calendar']</setting>
          <setting name="ClientId">template-client</setting>
          <setting name="ForceClientAuthViaRequestBody" type="b">true</setting>
        </group>
      </group>
    </group>
  </template>
</provider>
"""


class ParseTests(unittest.TestCase):
    def test_variant_text(self):
        self.assertEqual(parse_variant_text("s", "'o/oauth2/auth?a=b&c=d'"), "o/oauth2/auth?a=b&c=d")
        self.assertEqual(parse_variant_text("s", "''"), "")
        self.assertEqual(parse_variant_text("s", "plain text"), "plain text")
        self.assertEqual(parse_variant_text("as", "['https', 'http']"), ["https", "http"])
        self.assertEqual(parse_variant_text("as", "garbage["), [])
        self.assertIs(parse_variant_text("b", "true"), True)
        self.assertIs(parse_variant_text("b", "false"), False)
        self.assertEqual(parse_variant_text("u", "7"), 7)
        self.assertIsNone(parse_variant_text("a{sv}", "{}"))

    def test_reply_mapping(self):
        bearer = _credential_from_reply({"AccessToken": "ya29.x", "ExpiresIn": 3599, "RefreshToken": ""})
        self.assertEqual((bearer.kind, bearer.token), (KIND_BEARER, "ya29.x"))
        self.assertGreater(bearer.expires_at, time.time() + 3000)
        basic = _credential_from_reply({"UserName": "u", "Secret": "p"})
        self.assertEqual((basic.kind, basic.username, basic.password), (KIND_BASIC, "u", "p"))
        with self.assertRaises(AuthError):
            _credential_from_reply({"Scope": ["x"]})


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._dir.name, "accounts.db")
        conn = sqlite3.connect(self.db_path)
        for statement in SCHEMA:
            conn.execute(statement)
        conn.executemany("INSERT INTO Accounts (id, name, provider, enabled) VALUES (?, ?, ?, ?)", [
            (1, "google1", "google", 1),
            (2, "cloud", "nextcloud", 1),
            (3, "old-google", "google", 0),
            (4, "template-only", "google", 1),
        ])
        conn.executemany("INSERT INTO Settings (account, service, key, type, value) VALUES (?, 0, ?, ?, ?)", [
            (1, "CredentialsId", "u", "1"),
            (1, "auth/method", "s", "'oauth2'"),
            (1, "auth/mechanism", "s", "'web_server'"),
            (1, "auth/oauth2/web_server/ClientId", "s", "'account-client'"),
            (1, "auth/oauth2/web_server/Scope", "as", "['https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/tasks']"),
            (1, "auth/oauth2/web_server/ForceClientAuthViaRequestBody", "b", "true"),
            (1, "name", "s", "'google1'"),
            (4, "CredentialsId", "u", "4"),
        ])
        conn.execute("INSERT INTO Settings (account, service, key, type, value) VALUES (1, 7, 'enabled', 'b', 'true')")
        conn.commit()
        conn.close()
        providers_dir = os.path.join(self._dir.name, "providers")
        os.makedirs(os.path.join(providers_dir, "kde"))
        with open(os.path.join(providers_dir, "kde", "google.provider"), "w", encoding="utf-8") as f:
            f.write(GOOGLE_PROVIDER_XML)
        self.provider = KdeAccountsProvider(db_path=self.db_path, providers_dir=providers_dir,
                                            bus_address="", use_gi=False)

    def tearDown(self):
        self._dir.cleanup()

    def test_registered_and_available(self):
        self.assertIs(PROVIDER_CLASSES["kde"], KdeAccountsProvider)
        self.assertTrue(self.provider.available())
        self.assertFalse(KdeAccountsProvider(db_path=os.path.join(self._dir.name, "nope.db"), use_gi=False).available())
        self.assertIn(SIGNOND_NAME, self.provider.required_permissions()["dbus"])

    def test_lists_every_enabled_account_classified(self):
        accounts = self.provider.list_accounts()
        # Account 3 is disabled, so it is absent; account 2 is Nextcloud, so it is present but
        # classified `dav` for the backend to mark as unsupported.
        self.assertEqual([(a.id, a.label, a.kind) for a in accounts],
                         [("1", "google1", "google"), ("2", "cloud", "dav"), ("4", "template-only", "google")])
        self.assertTrue(all(a.provider == "kde" for a in accounts))

    def test_an_unknown_provider_is_kept_with_no_kind(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO Accounts (id, name, provider, enabled) VALUES (5, 'odd', 'weirdcloud', 1)")
        conn.commit()
        conn.close()
        odd = next(a for a in self.provider.list_accounts() if a.id == "5")
        self.assertEqual(odd.kind, "")

    def test_discoverable(self):
        self.assertTrue(self.provider.discoverable)

    def test_auth_data_from_account_settings(self):
        credentials_id, method, mechanism, params = self.provider._auth_data("1")
        self.assertEqual((credentials_id, method, mechanism), (1, "oauth2", "web_server"))
        self.assertEqual(params["ClientId"], "account-client")          # account wins over template
        self.assertEqual(params["Host"], "accounts.google.com")         # template fills the gaps
        self.assertEqual(params["Scope"][1], "https://www.googleapis.com/auth/tasks")
        self.assertIs(params["ForceClientAuthViaRequestBody"], True)
        self.assertNotIn("auth/method", params)
        self.assertNotIn("CredentialsId", params)

    def test_auth_data_from_template_alone(self):
        credentials_id, method, mechanism, params = self.provider._auth_data("4")
        self.assertEqual((credentials_id, method, mechanism), (4, "oauth2", "web_server"))
        self.assertEqual(params["ClientId"], "template-client")

    def test_missing_account_or_login(self):
        with self.assertRaises(AuthError):
            self.provider._auth_data("99")
        with self.assertRaises(AuthError):
            self.provider.get_credential("")
        with self.assertRaises(SourceError):
            KdeAccountsProvider(db_path=os.path.join(self._dir.name, "nope.db"), use_gi=False).list_accounts()

    def test_get_credential_caches_until_expiry(self):
        reply = {"AccessToken": "tok1", "ExpiresIn": 3600}
        with mock.patch.object(self.provider, "_request_token", return_value=reply) as request:
            first = self.provider.get_credential("1")
            second = self.provider.get_credential("1")
        self.assertEqual((first.kind, first.token), (KIND_BEARER, "tok1"))
        self.assertIs(second, first)
        request.assert_called_once()
        credentials_id, method, mechanism, params = request.call_args.args
        self.assertEqual((credentials_id, method, mechanism), (1, "oauth2", "web_server"))
        self.assertEqual(params["ClientId"], "account-client")

        with mock.patch.object(self.provider, "_request_token", return_value={"AccessToken": "tok2", "ExpiresIn": 10}):
            self.assertEqual(self.provider.get_credential("1", force_refresh=True).token, "tok2")
        self.provider.forget("1")
        with mock.patch.object(self.provider, "_request_token", return_value={"AccessToken": "tok3", "ExpiresIn": 10}):
            self.assertEqual(self.provider.get_credential("1").token, "tok3")


if __name__ == "__main__":
    unittest.main()
