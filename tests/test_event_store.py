import unittest
from datetime import datetime, timedelta, timezone

from internal.event_store import EventStore
from internal.events import STATUS_CANCELLED, CalendarEvent, CalendarStatus

UTC = timezone.utc
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def ev(uid, start_offset_min, duration_min=30, **kwargs):
    start = NOW + timedelta(minutes=start_offset_min)
    return CalendarEvent(uid=uid, calendar_id="c", title=uid, start=start, end=start + timedelta(minutes=duration_min), **kwargs)


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.store = EventStore(dispatch=lambda cb, *a: (self.calls.append(cb), cb(*a)))

    def test_upcoming_ordering_and_filters(self):
        events = [
            ev("later", 120),
            ev("running", -10, 60),
            ev("past", -120, 30),
            ev("soon", 15),
            ev("cancelled", 30, status=STATUS_CANCELLED),
            CalendarEvent(uid="allday", calendar_id="c", title="allday", all_day=True,
                          start=NOW.replace(hour=0), end=NOW.replace(hour=0) + timedelta(days=1)),
        ]
        self.store.update(events, now=NOW)
        self.assertEqual([e.uid for e in self.store.get_upcoming(NOW)], ["allday", "running", "soon", "later"])
        self.assertEqual([e.uid for e in self.store.get_upcoming(NOW, include_all_day=False, include_in_progress=False)], ["soon", "later"])
        self.assertEqual(self.store.get_next(NOW, include_all_day=False).uid, "running")
        self.assertEqual([e.uid for e in self.store.get_upcoming(NOW, include_all_day=False, horizon=timedelta(hours=1))], ["running", "soon"])
        self.assertIn("cancelled", [e.uid for e in self.store.get_upcoming(NOW, include_cancelled=True)])

    def test_skip_and_dismiss_are_pruned_when_event_ends(self):
        self.store.update([ev("a", 5), ev("b", 60)], now=NOW)
        self.store.skip("a")
        self.store.dismiss("b")
        self.assertEqual(self.store.get_next(NOW).uid, "b")
        self.assertTrue(self.store.is_dismissed("b"))
        # 'a' has ended by the next refresh -> its skip mark is dropped; 'b' still live.
        later = NOW + timedelta(minutes=40)
        self.store.update([ev("a", 5), ev("b", 60)], now=later)
        self.assertFalse(self.store.is_skipped("a"))
        self.assertTrue(self.store.is_dismissed("b"))

    def test_today(self):
        self.store.update([ev("today", 60), ev("tomorrow", 24 * 60), ev("earlier", -300)], now=NOW)
        self.assertEqual([e.uid for e in self.store.get_today(NOW)], ["earlier", "today"])
        self.assertEqual([e.uid for e in self.store.get_today(NOW, include_past=False)], ["today"])

    def test_subscribers_and_statuses(self):
        seen = []
        token = self.store.subscribe(lambda: seen.append(1))
        self.store.update([], [CalendarStatus(calendar_id="c", ok=False, error="boom")], now=NOW)
        self.assertEqual(len(seen), 1)
        self.assertTrue(self.store.has_errors())
        self.store.set_backend_connected(True)
        self.store.set_backend_connected(True)  # unchanged -> no extra notify
        self.assertEqual(len(seen), 2)
        self.store.unsubscribe(token)
        self.store.update([], now=NOW)
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
