import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from backend.google_oauth import AuthFlowError, LoopbackFlow, describe_token_error
from backend.google_source import TokenStore, map_event
from backend.source_errors import AuthError, SourceError

UTC = timezone.utc


def item(**overrides) -> dict:
    """A minimal events.list item, as returned with singleEvents=true."""
    base = {
        "id": "abc123",
        "status": "confirmed",
        "summary": "Standup",
        "htmlLink": "https://www.google.com/calendar/event?eid=abc123",
        "start": {"dateTime": "2026-09-02T10:00:00-04:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-09-02T10:30:00-04:00", "timeZone": "America/New_York"},
    }
    base.update(overrides)
    return base


class MapEventTest(unittest.TestCase):
    def test_timed_event_is_utc(self):
        event = map_event(item(), "cal-1")
        self.assertEqual(event.start, datetime(2026, 9, 2, 14, 0, tzinfo=UTC))
        self.assertEqual(event.end, datetime(2026, 9, 2, 14, 30, tzinfo=UTC))
        self.assertFalse(event.all_day)
        self.assertEqual(event.calendar_id, "cal-1")
        self.assertEqual(event.title, "Standup")
        self.assertEqual(event.status, "CONFIRMED")

    def test_timezone_comes_from_the_start_object(self):
        self.assertEqual(map_event(item(), "cal-1").tzid, "America/New_York")
        no_zone = item(start={"dateTime": "2026-09-02T10:00:00-04:00"})
        self.assertEqual(map_event(no_zone, "cal-1").tzid, "")
        all_day = item(start={"date": "2026-09-02"}, end={"date": "2026-09-03"})
        self.assertEqual(map_event(all_day, "cal-1").tzid, "")

    def test_all_day_event_keeps_dates(self):
        event = map_event(item(start={"date": "2026-09-02"}, end={"date": "2026-09-03"}), "cal-1")
        self.assertTrue(event.all_day)
        # Naive midnight on the backend; the foreground pins it to local midnight.
        self.assertIsNone(event.start.tzinfo)
        self.assertEqual(event.to_dict()["start"], "2026-09-02")
        self.assertEqual(event.to_dict()["end"], "2026-09-03")

    def test_recurring_instance_carries_series_uid(self):
        event = map_event(item(id="abc123_20260902T140000Z", recurringEventId="abc123"), "cal-1")
        self.assertEqual(event.uid, "abc123_20260902T140000Z")
        self.assertEqual(event.series_uid, "abc123")

    def test_one_off_event_is_its_own_series(self):
        self.assertEqual(map_event(item(), "cal-1").series_uid, "abc123")

    def test_hangout_link_wins_over_description(self):
        event = map_event(item(hangoutLink="https://meet.google.com/abc-defg-hij",
                               description="Old link https://zoom.us/j/999"), "cal-1")
        self.assertEqual(event.meeting_link, "https://meet.google.com/abc-defg-hij")

    def test_conference_data_video_entry_point(self):
        event = map_event(item(conferenceData={"entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
            {"entryPointType": "video", "uri": "https://meet.google.com/xyz-abcd-efg"},
        ]}), "cal-1")
        self.assertEqual(event.meeting_link, "https://meet.google.com/xyz-abcd-efg")

    def test_falls_back_to_scanning_location_and_description(self):
        event = map_event(item(location="https://zoom.us/j/123456"), "cal-1")
        self.assertEqual(event.meeting_link, "https://zoom.us/j/123456")

    def test_untitled_and_cancelled(self):
        event = map_event(item(summary=None, status="cancelled"), "cal-1")
        self.assertEqual(event.title, "(no title)")
        self.assertEqual(event.status, "CANCELLED")
        self.assertTrue(event.is_cancelled)

    def test_end_before_start_is_clamped(self):
        event = map_event(item(end={"dateTime": "2026-09-02T09:00:00-04:00"}), "cal-1")
        self.assertEqual(event.end, event.start)

    def test_missing_times_raise(self):
        with self.assertRaises(SourceError):
            map_event(item(start={}), "cal-1")
        with self.assertRaises(SourceError):
            map_event(item(end=None), "cal-1")

    def test_round_trips_through_the_wire_format(self):
        event = map_event(item(), "cal-1")
        self.assertEqual(json.loads(json.dumps(event.to_dict()))["uid"], "abc123")


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = TokenStore(os.path.join(self._dir.name, "credentials"))

    def tearDown(self):
        self._dir.cleanup()

    def test_saved_token_is_private_and_round_trips(self):
        self.store.save("acct", {"refresh_token": "r", "email": "a@b.c"})
        path = self.store.path_for("acct")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(self.store.load("acct")["email"], "a@b.c")

    def test_missing_and_incomplete_tokens_are_auth_errors(self):
        with self.assertRaises(AuthError):
            self.store.load("nobody")
        self.store.save("acct", {"email": "a@b.c"})
        with self.assertRaises(AuthError):
            self.store.load("acct")

    def test_delete_is_idempotent(self):
        self.store.save("acct", {"refresh_token": "r"})
        self.store.delete("acct")
        self.store.delete("acct")
        self.assertFalse(os.path.exists(self.store.path_for("acct")))


class LoopbackFlowTest(unittest.TestCase):
    def test_auth_url_is_pkce_on_a_loopback_redirect(self):
        flow = LoopbackFlow("client-id.apps.googleusercontent.com", "secret")
        try:
            url = flow.start()
            query = parse_qs(urlparse(url).query)
            self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertTrue(query["code_challenge"][0])
            # Never send the verifier itself to the authorization endpoint.
            self.assertNotIn("code_verifier", query)
            # offline + consent is what makes Google return a refresh token every time.
            self.assertEqual(query["access_type"], ["offline"])
            self.assertEqual(query["prompt"], ["consent"])
            self.assertEqual(query["scope"], ["https://www.googleapis.com/auth/calendar.readonly"])
            redirect = query["redirect_uri"][0]
            self.assertTrue(redirect.startswith("http://127.0.0.1:"))
            self.assertEqual(redirect, flow.redirect_uri)
        finally:
            flow.close()

    def test_missing_client_id_is_rejected_before_binding(self):
        with self.assertRaises(AuthFlowError):
            LoopbackFlow("", "secret")


class TokenErrorTest(unittest.TestCase):
    def test_invalid_grant_points_at_the_publishing_status(self):
        message = describe_token_error({"error": "invalid_grant"}, 400)
        self.assertIn("7 days", message)
        self.assertIn("publish", message.lower())

    def test_invalid_client_names_the_credentials(self):
        self.assertIn("client ID", describe_token_error({"error": "invalid_client"}, 401))

    def test_redirect_mismatch_names_the_client_type(self):
        self.assertIn("Desktop app", describe_token_error({"error": "redirect_uri_mismatch"}, 400))

    def test_unknown_error_is_still_reported(self):
        self.assertIn("boom", describe_token_error({"error": "boom", "error_description": "why"}, 400))
        self.assertIn("HTTP 500", describe_token_error({}, 500))


if __name__ == "__main__":
    unittest.main()
