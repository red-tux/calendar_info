import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.ics_source import SourceError, expand_events, fetch_ics, normalize_source

UTC = timezone.utc

SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:weekly@example.com
DTSTART;TZID=America/New_York:20260901T100000
DTEND;TZID=America/New_York:20260901T103000
RRULE:FREQ=WEEKLY;BYDAY=TU,TH
SUMMARY:Standup
LOCATION:https://meet.google.com/abc-defg-hij
END:VEVENT
BEGIN:VEVENT
UID:weekly@example.com
RECURRENCE-ID;TZID=America/New_York:20260903T100000
DTSTART;TZID=America/New_York:20260903T110000
DTEND;TZID=America/New_York:20260903T113000
SUMMARY:Standup (moved)
END:VEVENT
BEGIN:VEVENT
UID:allday@example.com
DTSTART;VALUE=DATE:20260904
SUMMARY:Company holiday
END:VEVENT
BEGIN:VEVENT
UID:conf@example.com
DTSTART:20260902T150000Z
DURATION:PT45M
SUMMARY:Design review
DESCRIPTION:Notes: https://example.com/doc\\nZoom: https://zoom.us/j/99887766?pwd=x
X-GOOGLE-CONFERENCE:https://meet.google.com/xyz-abcd-efg
STATUS:CONFIRMED
END:VEVENT
BEGIN:VEVENT
UID:cancelled@example.com
DTSTART:20260902T170000Z
DTEND:20260902T173000Z
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""



class EventZoneTest(unittest.TestCase):
    ZONED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//T//EN
BEGIN:VEVENT
UID:ny@example.com
DTSTART;TZID=America/New_York:20260903T110000
DTEND;TZID=America/New_York:20260903T113000
SUMMARY:Eastern
END:VEVENT
BEGIN:VEVENT
UID:utc@example.com
DTSTART:20260903T150000Z
DTEND:20260903T153000Z
SUMMARY:Zulu
END:VEVENT
BEGIN:VEVENT
UID:allday@example.com
DTSTART;VALUE=DATE:20260903
DTEND;VALUE=DATE:20260904
SUMMARY:Whole day
END:VEVENT
END:VCALENDAR"""

    def test_tzid_is_carried_per_event(self):
        window = (datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 5, tzinfo=UTC))
        by_title = {e.title: e for e in expand_events(self.ZONED, "c", *window)}
        self.assertEqual(by_title["Eastern"].tzid, "America/New_York")
        self.assertEqual(by_title["Zulu"].tzid, "UTC")
        # All-day events have no clock time, so no zone to display them in.
        self.assertEqual(by_title["Whole day"].tzid, "")

class ExpandTests(unittest.TestCase):
    def setUp(self):
        self.window_start = datetime(2026, 9, 1, tzinfo=UTC)
        self.window_end = datetime(2026, 9, 8, tzinfo=UTC)
        self.events = expand_events(SAMPLE, "cal1", self.window_start, self.window_end)
        self.by_title = {e.title: e for e in self.events}

    def test_recurrence_expanded_with_override(self):
        standups = [e for e in self.events if e.series_uid == "weekly@example.com"]
        # Tue Sep 1, Thu Sep 3 (moved), Tue Sep 8 is outside the window end (exclusive at 00:00Z)
        self.assertEqual([e.title for e in standups], ["Standup", "Standup (moved)"])
        self.assertEqual(standups[0].start, datetime(2026, 9, 1, 14, 0, tzinfo=UTC))  # 10:00 EDT
        self.assertEqual(standups[1].start, datetime(2026, 9, 3, 15, 0, tzinfo=UTC))  # 11:00 EDT
        self.assertNotEqual(standups[0].uid, standups[1].uid)
        self.assertEqual(standups[0].meeting_link, "https://meet.google.com/abc-defg-hij")

    def test_all_day_defaults_to_one_day(self):
        holiday = self.by_title["Company holiday"]
        self.assertTrue(holiday.all_day)
        data = holiday.to_dict()
        self.assertEqual((data["start"], data["end"]), ("2026-09-04", "2026-09-05"))

    def test_duration_and_conference_property_precedence(self):
        review = self.by_title["Design review"]
        self.assertEqual(review.duration, timedelta(minutes=45))
        self.assertEqual(review.meeting_link, "https://meet.google.com/xyz-abcd-efg")

    def test_cancelled_status_preserved(self):
        self.assertTrue(self.by_title["Cancelled thing"].is_cancelled)

    def test_sorted_by_start(self):
        starts = [e.start for e in self.events]
        self.assertEqual(starts, sorted(starts))

    def test_round_trip_through_wire_format(self):
        from internal.events import CalendarEvent
        for event in self.events:
            back = CalendarEvent.from_dict(event.to_dict(), tz=UTC)
            self.assertEqual(back.uid, event.uid)
            self.assertEqual(back.start, event.start)
            self.assertEqual(back.end, event.end)

    def test_invalid_text_raises_source_error(self):
        with self.assertRaises(SourceError):
            expand_events("this is not a calendar", "x", self.window_start, self.window_end)


class FetchTests(unittest.TestCase):
    def test_webcal_normalized(self):
        self.assertEqual(normalize_source("webcal://example.com/a.ics"), "https://example.com/a.ics")

    def test_local_file_and_file_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cal.ics")
            with open(path, "w") as f:
                f.write(SAMPLE)
            self.assertEqual(fetch_ics(path), SAMPLE)
            self.assertEqual(fetch_ics("file://" + path), SAMPLE)

    def test_missing_file_is_source_error(self):
        with self.assertRaises(SourceError):
            fetch_ics("/nonexistent/calendar.ics")

    def test_empty_source_is_source_error(self):
        with self.assertRaises(SourceError):
            fetch_ics("   ")


if __name__ == "__main__":
    unittest.main()
