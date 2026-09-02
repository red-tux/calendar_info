import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from internal.events import (
    CalendarEvent,
    extract_meeting_link,
    format_clock,
    format_countdown,
    format_day,
    format_remaining,
    format_start,
    truncate,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


class MeetingLinkTests(unittest.TestCase):
    def test_google_meet_in_location(self):
        self.assertEqual(
            extract_meeting_link("https://meet.google.com/abc-defg-hij"),
            "https://meet.google.com/abc-defg-hij",
        )

    def test_provider_link_beats_generic_url_in_earlier_field(self):
        link = extract_meeting_link(
            None,
            "Room 4 - agenda at https://example.com/agenda",
            "Join: https://us02web.zoom.us/j/1234567890?pwd=abc",
        )
        self.assertEqual(link, "https://us02web.zoom.us/j/1234567890?pwd=abc")

    def test_teams_link_with_trailing_punctuation(self):
        text = "Join here: https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d."
        self.assertEqual(
            extract_meeting_link(text),
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d",
        )

    def test_generic_url_fallback(self):
        self.assertEqual(extract_meeting_link("see https://example.org/x)"), "https://example.org/x")

    def test_no_link(self):
        self.assertIsNone(extract_meeting_link("Conference room B", None, ""))


class SerializationTests(unittest.TestCase):
    def test_timed_round_trip_is_utc(self):
        start = datetime(2026, 9, 2, 14, 30, tzinfo=NY)
        event = CalendarEvent(uid="u", calendar_id="c", title="t", start=start, end=start + timedelta(hours=1))
        data = event.to_dict()
        self.assertEqual(data["start"], "2026-09-02T18:30:00+00:00")
        back = CalendarEvent.from_dict(data, tz=NY)
        self.assertEqual(back.start, start)
        self.assertEqual(back.duration, timedelta(hours=1))
        self.assertFalse(back.all_day)

    def test_all_day_round_trip_pins_local_midnight(self):
        data = {"uid": "u", "calendar_id": "c", "title": "Holiday", "all_day": True,
                "start": "2026-09-07", "end": "2026-09-08"}
        event = CalendarEvent.from_dict(data, tz=NY)
        self.assertEqual(event.start, datetime(2026, 9, 7, 0, 0, tzinfo=NY))
        self.assertEqual(event.end, datetime(2026, 9, 8, 0, 0, tzinfo=NY))
        self.assertEqual(event.to_dict()["start"], "2026-09-07")

    def test_missing_title_is_placeholder(self):
        event = CalendarEvent.from_dict({"uid": "u", "start": "2026-09-02T10:00:00+00:00", "end": "2026-09-02T11:00:00+00:00"})
        self.assertEqual(event.title, "(no title)")


class TimeHelperTests(unittest.TestCase):
    def test_progress_and_state(self):
        start = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
        event = CalendarEvent(uid="u", calendar_id="c", title="t", start=start, end=start + timedelta(hours=2))
        self.assertTrue(event.is_upcoming(start - timedelta(minutes=1)))
        self.assertTrue(event.is_in_progress(start + timedelta(hours=1)))
        self.assertAlmostEqual(event.progress_fraction(start + timedelta(hours=1)), 0.5)
        self.assertTrue(event.is_over(start + timedelta(hours=2)))

    def test_format_countdown(self):
        self.assertEqual(format_countdown(-5), "now")
        self.assertEqual(format_countdown(30), "<1m")
        self.assertEqual(format_countdown(12 * 60 + 59), "12m")
        self.assertEqual(format_countdown(65 * 60), "1h05")
        self.assertEqual(format_countdown(3 * 86400), "3d")
        self.assertEqual(format_countdown(65 * 60, compact=False), "in 1h 05m")

    def test_format_remaining(self):
        self.assertEqual(format_remaining(30), "ending")
        self.assertEqual(format_remaining(25 * 60), "25m left")
        self.assertEqual(format_remaining(65 * 60), "1h05 left")

    def test_format_clock(self):
        value = datetime(2026, 9, 2, 18, 5, tzinfo=UTC)
        self.assertEqual(format_clock(value, "24", NY), "14:05")
        self.assertEqual(format_clock(value, "12", NY), "2:05pm")
        self.assertEqual(format_clock(datetime(2026, 9, 2, 4, 0, tzinfo=UTC), "12", NY), "12:00am")

    def test_format_day_and_start(self):
        now = datetime(2026, 9, 2, 12, 0, tzinfo=NY)   # a Wednesday
        today = CalendarEvent(uid="a", calendar_id="c", title="t", start=now + timedelta(hours=2), end=now + timedelta(hours=3))
        tomorrow = CalendarEvent(uid="b", calendar_id="c", title="t", start=now + timedelta(days=1), end=now + timedelta(days=1, hours=1))
        friday = now + timedelta(days=2)
        self.assertEqual(format_day(today.start, now, NY), "Today")
        self.assertEqual(format_day(tomorrow.start, now, NY), "Tomorrow")
        self.assertEqual(format_day(friday, now, NY), "Fri")
        self.assertEqual(format_day(now + timedelta(days=12), now, NY), "Sep 14")
        self.assertEqual(format_start(today, now, "24", NY), "14:00")
        self.assertEqual(format_start(tomorrow, now, "24", NY), "Tomorrow 12:00")
        all_day = CalendarEvent(uid="d", calendar_id="c", title="t", all_day=True,
                                start=datetime(2026, 9, 3, tzinfo=NY), end=datetime(2026, 9, 4, tzinfo=NY))
        self.assertEqual(format_start(all_day, now, "24", NY), "Tomorrow")

    def test_truncate(self):
        self.assertEqual(truncate("Weekly   sync", 20), "Weekly sync")
        self.assertEqual(truncate("A very long meeting title", 10), "A very lo…")


if __name__ == "__main__":
    unittest.main()
