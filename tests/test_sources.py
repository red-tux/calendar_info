"""The calendar-source seam: type -> source lookup, provider lookup, and the last-good cache."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from backend import sources
from backend.accounts.base import AccountProvider
from backend.accounts.registry import make_providers
from backend.source_errors import AuthError, SourceError
from backend.sources import (
    SOURCES,
    GoogleSource,
    IcsSource,
    SourceContext,
    classify_account_kind,
    describe_sources,
    source_for,
)


def _ics_with_event(start: datetime) -> str:
    end = start + timedelta(hours=1)
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:test",
        "BEGIN:VEVENT", "UID:one", "SUMMARY:Standup",
        f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}",
        "END:VEVENT", "END:VCALENDAR", "",
    ])


class RegistryTests(unittest.TestCase):
    def test_known_types(self):
        self.assertIsInstance(SOURCES["ics"], IcsSource)
        self.assertIsInstance(SOURCES["google"], GoogleSource)
        for type_id, source in SOURCES.items():
            self.assertEqual(source.type_id, type_id)

    def test_lookup_defaults_to_ics_and_rejects_unknown(self):
        self.assertIs(source_for({}), SOURCES["ics"])
        self.assertIs(source_for({"type": "google"}), SOURCES["google"])
        with self.assertRaises(SourceError):
            source_for({"type": "carrier-pigeon"})

    def test_provider_lookup(self):
        provider = AccountProvider()
        ctx = SourceContext(providers={"oauth": provider})
        self.assertIs(ctx.provider_for({}), provider)
        self.assertIs(ctx.provider_for({"account_provider": "oauth"}), provider)
        with self.assertRaises(AuthError):
            ctx.provider_for({"account_provider": "nope"})

    def test_ics_has_nothing_to_list(self):
        with self.assertRaises(SourceError):
            IcsSource().list_calendars(AccountProvider(), "acc")

    def test_only_account_backed_sources_need_an_account(self):
        self.assertTrue(GoogleSource().needs_account)
        self.assertFalse(IcsSource().needs_account)
        self.assertEqual([f["key"] for f in IcsSource().manual_fields], ["source"])
        self.assertEqual(GoogleSource().manual_fields, ())


class ClassificationTests(unittest.TestCase):
    def test_a_kind_a_source_declares_is_supported(self):
        self.assertEqual(classify_account_kind("google"), ("google", True, ""))

    def test_a_kind_no_source_declares_is_named_but_unsupported(self):
        calendar_type, supported, detail = classify_account_kind("dav")
        self.assertEqual((calendar_type, supported), ("", False))
        self.assertIn("CalDAV", detail)
        self.assertIn("not built yet", detail)

    def test_an_unknown_kind_says_so(self):
        calendar_type, supported, detail = classify_account_kind("")
        self.assertEqual((calendar_type, supported), ("", False))
        self.assertIn("does not recognise", detail)

    def test_describe_sources_names_manual_providers_only(self):
        entries = {entry["id"]: entry for entry in describe_sources(make_providers())}
        self.assertEqual(entries["google"]["providers"], ["oauth"])   # kde is discoverable
        self.assertTrue(entries["google"]["needs_account"])
        self.assertEqual(entries["google"]["account_kinds"], ["google"])
        self.assertEqual(entries["ics"]["providers"], [])
        self.assertFalse(entries["ics"]["needs_account"])
        self.assertEqual(entries["ics"]["manual_fields"][0]["key"], "source")
        for entry in entries.values():
            self.assertTrue(entry["label"])


class IcsCacheTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.ctx = SourceContext(cache_dir=self._dir.name)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.window = (now - timedelta(days=1), now + timedelta(days=7))
        self.text = _ics_with_event(now + timedelta(days=1))
        self.calendar = {"id": "cal", "type": "ics", "source": "https://example.com/a.ics"}

    def tearDown(self):
        self._dir.cleanup()

    def test_fetch_writes_the_cache_and_load_cached_reads_it_back(self):
        with mock.patch.object(sources, "fetch_ics", return_value=self.text):
            events = IcsSource().fetch(self.calendar, *self.window, self.ctx)
        self.assertEqual([e.title for e in events], ["Standup"])
        cached = IcsSource().load_cached(self.calendar, *self.window, self.ctx)
        self.assertEqual([e.uid for e in cached], [e.uid for e in events])

    def test_no_cache_dir_means_no_cache(self):
        ctx = SourceContext()
        with mock.patch.object(sources, "fetch_ics", return_value=self.text):
            IcsSource().fetch(self.calendar, *self.window, ctx)
        self.assertEqual(IcsSource().load_cached(self.calendar, *self.window, ctx), [])

    def test_json_cache_refilters_to_the_current_window(self):
        with mock.patch.object(sources, "fetch_ics", return_value=self.text):
            events = IcsSource().fetch(self.calendar, *self.window, self.ctx)
        sources._write_json_cache(self._dir.name, "g", events)
        self.assertEqual(len(sources._read_json_cache(self._dir.name, "g", *self.window)), 1)
        later = (self.window[1] + timedelta(days=1), self.window[1] + timedelta(days=2))
        self.assertEqual(sources._read_json_cache(self._dir.name, "g", *later), [])


if __name__ == "__main__":
    unittest.main()
