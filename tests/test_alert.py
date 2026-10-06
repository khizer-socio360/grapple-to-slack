import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import alert as a
import summarize as s

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


def email(**kwargs):
    defaults = dict(id="e1", lead="jane@acme.com", subject="Re: hi", timestamp=NOW - timedelta(hours=2),
                    ue_type=2, ai_interest=1, campaign="Camp A")
    defaults.update(kwargs)
    return s.Email(**defaults)


class SelectionTests(unittest.TestCase):
    def test_picks_interested_replies_only(self):
        emails = [
            email(id="a", ai_interest=1),
            email(id="b", ai_interest=2),
            email(id="c", ai_interest=0),
            email(id="d", ai_interest=-2),
            email(id="e", ai_interest=None),
            email(id="f", ai_interest=1, ue_type=1),  # sent, not a reply
            email(id="", ai_interest=1),  # no id: cannot be deduped, skipped
        ]
        picked = a.find_new_interested(emails, a.AlertState(), now=NOW)
        self.assertEqual([e.id for e in picked], ["a", "b"])

    def test_respects_threshold_lookback_and_state(self):
        old = email(id="old", timestamp=NOW - timedelta(days=8))
        seen = email(id="seen")
        fresh = email(id="fresh", ai_interest=3)
        low = email(id="low", ai_interest=1)
        state = a.AlertState(alerted={"seen": seen.timestamp.isoformat()})
        picked = a.find_new_interested([old, seen, fresh, low], state, now=NOW, min_interest=2)
        self.assertEqual([e.id for e in picked], ["fresh"])
        picked = a.find_new_interested([old, seen, fresh, low], state, now=NOW, lookback_days=30)
        self.assertEqual([e.id for e in picked], ["old", "fresh", "low"])

    def test_sorted_oldest_first(self):
        later = email(id="later", timestamp=NOW - timedelta(hours=1))
        earlier = email(id="earlier", timestamp=NOW - timedelta(hours=5))
        picked = a.find_new_interested([later, earlier], a.AlertState(), now=NOW)
        self.assertEqual([e.id for e in picked], ["earlier", "later"])


class StateTests(unittest.TestCase):
    def test_round_trip_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "alerted.json"
            self.assertEqual(a.AlertState.load(path).alerted, {})
            state = a.AlertState(alerted={
                "new": (NOW - timedelta(days=1)).isoformat(),
                "ancient": (NOW - timedelta(days=200)).isoformat(),
            })
            state.prune(NOW)
            state.save(path)
            loaded = a.AlertState.load(path)
            self.assertEqual(list(loaded.alerted), ["new"])
            self.assertEqual(json.loads(path.read_text())["alerted"]["new"], state.alerted["new"])

    def test_corrupt_state_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerted.json"
            path.write_text("{not json")
            with self.assertRaises(RuntimeError):
                a.AlertState.load(path)


class MessageTests(unittest.TestCase):
    def test_alert_message(self):
        text, blocks = a.build_alert(
            email(ai_interest=2, subject="Re: Getting more from your WMS"),
            tz=TZ, workspace_name="WS", project_name="Emails",
        )
        self.assertEqual(text, 'Meeting booked: jane@acme.com replied to "Re: Getting more from your WMS" (Camp A)')
        self.assertEqual(blocks[0]["text"]["text"], ":star: Meeting booked: jane@acme.com")
        fields = "\n".join(f["text"] for f in blocks[1]["fields"])
        self.assertIn("jane@acme.com", fields)
        self.assertIn("Camp A", fields)
        self.assertIn("Tue Oct 6, 8:00 AM CDT", fields)
        self.assertIn("AiInterestValue 2", blocks[2]["elements"][0]["text"])

    def test_interest_labels(self):
        self.assertEqual(a.interest_label(1), "Interested")
        self.assertEqual(a.interest_label(4), "Closed")
        self.assertEqual(a.interest_label(7), "Interest level 7")
        self.assertEqual(a.interest_label(None), "Unknown")


if __name__ == "__main__":
    unittest.main()
