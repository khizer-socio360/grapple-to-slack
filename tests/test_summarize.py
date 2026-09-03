import unittest
from datetime import date, datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import summarize as s

TZ = ZoneInfo("America/Chicago")


def cell(name, value):
    return {"key": name.lower(), "name": name, "value": value}


def row(*, lead="a@b.com", subject="Hello", ts_ms=1788448528000, ue=1, ai=None, name="Camp A", id_="x"):
    return [
        cell("_id", {"$oid": "abc"}),
        cell("Lead", lead),
        cell("Subject", subject),
        cell("TimestampEmail", {"$date": {"$numberLong": str(ts_ms)}}),
        cell("AiInterestValue", ai),
        cell("Id", id_),
        cell("UeType", ue),
        cell("Name", name),
    ]


def email(**kwargs):
    defaults = dict(id="x", lead="a@b.com", subject="Hello", timestamp=datetime(2026, 9, 2, 15, tzinfo=timezone.utc),
                    ue_type=1, ai_interest=None, campaign="Camp A")
    defaults.update(kwargs)
    return s.Email(**defaults)


class ParseTimestampTests(unittest.TestCase):
    def test_mongo_extended_json(self):
        parsed = s.parse_timestamp({"$date": {"$numberLong": "1788448528000"}})
        self.assertEqual(parsed, datetime(2026, 9, 3, 15, 15, 28, tzinfo=timezone.utc))

    def test_iso_string_with_z(self):
        self.assertEqual(s.parse_timestamp("2026-09-03T15:15:28Z"), datetime(2026, 9, 3, 15, 15, 28, tzinfo=timezone.utc))

    def test_epoch_seconds_and_millis(self):
        self.assertEqual(s.parse_timestamp(1788448528), datetime(2026, 9, 3, 15, 15, 28, tzinfo=timezone.utc))
        self.assertEqual(s.parse_timestamp(1788448528000), datetime(2026, 9, 3, 15, 15, 28, tzinfo=timezone.utc))

    def test_garbage_returns_none(self):
        self.assertIsNone(s.parse_timestamp(None))
        self.assertIsNone(s.parse_timestamp("not a date"))
        self.assertIsNone(s.parse_timestamp({"$date": None}))


class ParseRowTests(unittest.TestCase):
    def test_parses_fields(self):
        parsed = s.parse_row(row(lead=" lead@x.com ", subject="Subj", ue="2", ai=0, name="Camp"))
        self.assertEqual(parsed.lead, "lead@x.com")
        self.assertEqual(parsed.subject, "Subj")
        self.assertEqual(parsed.ue_type, 2)
        self.assertEqual(parsed.ai_interest, 0)
        self.assertEqual(parsed.campaign, "Camp")

    def test_missing_campaign_uses_placeholder(self):
        self.assertEqual(s.parse_row(row(name=None)).campaign, s.UNKNOWN_CAMPAIGN)

    def test_missing_timestamp_is_dropped(self):
        bad = [c for c in row() if c["name"] != "TimestampEmail"]
        self.assertIsNone(s.parse_row(bad))
        self.assertEqual(len(s.parse_rows([row(), bad])), 1)


class ClassificationTests(unittest.TestCase):
    def test_auto_reply_detection(self):
        for subject in [
            "Automatic reply: Hi",
            "Out of Office - PTO Re: Getting more from your WMS",
            "Traveling and slow to reply Re: What's changing",
            "OOO until Monday",
            "Auto-Reply: thanks",
        ]:
            self.assertTrue(email(ue_type=2, subject=subject).is_auto_reply, subject)

    def test_human_reply(self):
        e = email(ue_type=2, subject="Re: Getting more from your WMS")
        self.assertTrue(e.is_human_reply)
        self.assertFalse(e.is_auto_reply)

    def test_sent_emails_are_never_replies(self):
        e = email(ue_type=1, subject="Automatic reply: weird")
        self.assertFalse(e.is_reply)
        self.assertFalse(e.is_auto_reply)

    def test_interested_flag(self):
        self.assertTrue(email(ai_interest=1).is_interested)
        self.assertFalse(email(ai_interest=0).is_interested)
        self.assertFalse(email(ai_interest=None).is_interested)


class WindowTests(unittest.TestCase):
    def test_default_report_date_is_yesterday_local(self):
        # 03:30 UTC on Sep 3 is 22:30 CDT on Sep 2, so "yesterday" is Sep 1.
        now = datetime(2026, 9, 3, 3, 30, tzinfo=timezone.utc)
        self.assertEqual(s.default_report_date(now, TZ), date(2026, 9, 1))

    def test_day_window_uses_local_midnight(self):
        day, month = s.report_windows(date(2026, 9, 2), TZ)
        self.assertEqual(day.start, datetime(2026, 9, 2, 0, 0, tzinfo=TZ))
        self.assertEqual(day.end, datetime(2026, 9, 3, 0, 0, tzinfo=TZ))
        self.assertEqual(day.label, "Sep 2")
        self.assertEqual(month.start, datetime(2026, 9, 1, 0, 0, tzinfo=TZ))
        self.assertEqual(month.end, day.end)
        self.assertEqual(month.label, "Sep 1 – Sep 2")

    def test_sunday_report_covers_friday_to_sunday(self):
        # 2026-08-30 is a Sunday.
        day, month = s.report_windows(date(2026, 8, 30), TZ)
        self.assertEqual(day.start, datetime(2026, 8, 28, 0, 0, tzinfo=TZ))
        self.assertEqual(day.end, datetime(2026, 8, 31, 0, 0, tzinfo=TZ))
        self.assertEqual(day.label, "Aug 28 – Aug 30")
        self.assertEqual(s.period_name(day), "Friday – Sunday")
        self.assertEqual(s.digest_title(date(2026, 8, 30), day), "GTM Email Digest — Fri Aug 28 – Sun Aug 30, 2026")
        self.assertEqual(month.label, "Aug 1 – Aug 30")

    def test_weekend_spanning_month_boundary(self):
        # 2026-11-01 is a Sunday; the period reaches back into October.
        day, month = s.report_windows(date(2026, 11, 1), TZ)
        self.assertEqual(day.start.date(), date(2026, 10, 30))
        self.assertEqual(day.label, "Oct 30 – Nov 1")
        self.assertEqual(month.start.date(), date(2026, 11, 1))
        self.assertEqual(month.label, "Nov 1")

    def test_weekday_report_is_single_day(self):
        day, _ = s.report_windows(date(2026, 9, 2), TZ)
        self.assertEqual(s.period_name(day), "Yesterday")
        self.assertEqual(s.digest_title(date(2026, 9, 2), day), "GTM Email Digest — Wednesday, Sep 2, 2026")

    def test_first_of_month_labels(self):
        day, month = s.report_windows(date(2026, 9, 1), TZ)
        self.assertEqual(month.label, "Sep 1")
        self.assertEqual(month.start, day.start)

    def test_month_end_report_covers_whole_month(self):
        _, month = s.report_windows(date(2026, 9, 30), TZ)
        self.assertEqual(month.start.date(), date(2026, 9, 1))
        self.assertEqual(month.end.date(), date(2026, 10, 1))

    def test_contains_respects_boundaries(self):
        day, _ = s.report_windows(date(2026, 9, 2), TZ)
        self.assertTrue(day.contains(datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)))  # 00:00 CDT
        self.assertFalse(day.contains(datetime(2026, 9, 2, 4, 59, tzinfo=timezone.utc)))  # 23:59 CDT Sep 1
        self.assertFalse(day.contains(datetime(2026, 9, 3, 5, 0, tzinfo=timezone.utc)))  # 00:00 CDT Sep 3


class StatsTests(unittest.TestCase):
    def test_compute_stats(self):
        emails = [
            email(ue_type=1, campaign="A"),
            email(ue_type=1, campaign="A"),
            email(ue_type=1, campaign="B"),
            email(ue_type=3, campaign="A"),
            email(ue_type=2, campaign="A", subject="Re: hi"),
            email(ue_type=2, campaign="B", subject="Automatic reply: away", ai_interest=0),
            email(ue_type=2, campaign="B", subject="Re: interested", ai_interest=1),
        ]
        stats = s.compute_stats(emails)
        self.assertEqual((stats.sent, stats.manual_sent, stats.replies), (3, 1, 3))
        self.assertEqual((stats.human_replies, stats.auto_replies, stats.interested), (2, 1, 1))
        self.assertEqual(list(stats.by_campaign), ["A", "B"])
        self.assertEqual(stats.by_campaign["A"].sent, 2)
        self.assertEqual(stats.by_campaign["B"].replies, 2)
        self.assertEqual(stats.by_campaign["B"].human_replies, 1)

    def test_rate(self):
        self.assertEqual(s.rate(1, 0), "n/a")
        self.assertEqual(s.rate(3, 130), "2.3%")


class MessageTests(unittest.TestCase):
    def build(self, day_emails):
        day, month = s.report_windows(date(2026, 9, 2), TZ)
        return s.build_message(
            report_date=date(2026, 9, 2), day_window=day, month_window=month,
            day_emails=day_emails, month_emails=day_emails, workspace_name="WS", project_name="Emails",
            tz_name="America/Chicago", generated_at=datetime(2026, 9, 3, 9, 0, tzinfo=TZ),
        )

    def test_message_contains_replies_and_lead_email(self):
        text, blocks = self.build([
            email(ue_type=1), email(ue_type=2, lead="jane@acme.com", subject="Re: hi", ai_interest=2),
            email(ue_type=2, lead="ooo@acme.com", subject="Out of office"),
        ])
        flat = "\n".join(str(b) for b in blocks)
        self.assertIn("Wednesday, Sep 2, 2026", blocks[0]["text"]["text"])
        self.assertIn("jane@acme.com", flat)
        self.assertIn(":star:", flat)
        self.assertIn("Auto-replies / out of office (1)", flat)
        self.assertIn("ooo@acme.com", flat)
        self.assertIn("1 sent, 2 replies (1 human)", text)
        self.assertLessEqual(len(blocks), 50)
        for block in blocks:
            if block["type"] == "section":
                self.assertLessEqual(len(block["text"]["text"]), 3000)

    def test_message_with_no_replies(self):
        _, blocks = self.build([email(ue_type=1)])
        flat = "\n".join(str(b) for b in blocks)
        self.assertIn("No human replies yesterday", flat)
        self.assertNotIn("Auto-replies", flat)

    def test_weekend_message_wording(self):
        day, month = s.report_windows(date(2026, 8, 30), TZ)
        _, blocks = s.build_message(
            report_date=date(2026, 8, 30), day_window=day, month_window=month,
            day_emails=[], month_emails=[], workspace_name="WS", project_name="Emails",
            tz_name="America/Chicago", generated_at=datetime(2026, 8, 31, 9, 0, tzinfo=TZ),
        )
        flat = "\n".join(str(b) for b in blocks)
        self.assertIn("Fri Aug 28 – Sun Aug 30, 2026", blocks[0]["text"]["text"])
        self.assertIn("*Friday – Sunday (Aug 28 – Aug 30)*", flat)
        self.assertIn("No human replies over the weekend", flat)

    def test_long_reply_list_is_capped(self):
        many = [email(ue_type=2, lead=f"lead{i}@x.com", subject="Re: hi") for i in range(30)]
        _, blocks = self.build(many)
        flat = "\n".join(str(b) for b in blocks)
        self.assertIn("… and 10 more", flat)


class GateTests(unittest.TestCase):
    def test_should_run_now(self):
        nine = datetime(2026, 9, 3, 9, 4, tzinfo=TZ)
        ten = datetime(2026, 9, 3, 10, 0, tzinfo=TZ)
        self.assertTrue(s.should_run_now(nine, 9))
        self.assertFalse(s.should_run_now(ten, 9))
        self.assertTrue(s.should_run_now(ten, None))


class SlackTests(unittest.TestCase):
    def _session(self, payload):
        response = mock.Mock(status_code=200)
        response.json.return_value = payload
        session = mock.Mock()
        session.post.return_value = response
        return session

    def test_post_success(self):
        session = self._session({"ok": True, "channel": "C1", "ts": "1"})
        result = s.post_to_slack("tok", "#gtm", "text", [], session=session)
        self.assertEqual(result["ts"], "1")
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["channel"], "#gtm")
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    def test_post_error_has_hint(self):
        session = self._session({"ok": False, "error": "not_in_channel"})
        with self.assertRaises(s.SlackError) as ctx:
            s.post_to_slack("tok", "#gtm", "text", [], session=session)
        self.assertIn("invite the bot", str(ctx.exception))


class GrappleClientTests(unittest.TestCase):
    def test_pagination_and_project_lookup(self):
        pages = {
            "/me": {"workspace": {"id": 1, "name": "WS", "slug": "ws"}},
            "/workspaces/ws/projects": {"data": [{"id": 7, "name": "Emails"}, {"id": 8, "name": "Other"}]},
        }
        data_pages = [
            {"data": [row(), row()], "meta": {"has_more": True}},
            {"data": [row()], "meta": {"has_more": False}},
        ]

        def fake_get(url, params=None, timeout=None):
            path = url.replace(s.GRAPPLE_BASE_URL, "")
            response = mock.Mock(status_code=200)
            if path.endswith("/data"):
                response.json.return_value = data_pages[params["page"] - 1]
            else:
                response.json.return_value = pages[path]
            return response

        session = mock.Mock()
        session.headers = {}
        session.get.side_effect = fake_get
        client = s.GrappleClient("key", session=session)
        self.assertEqual(client.workspace()["slug"], "ws")
        self.assertEqual(client.find_project("ws", "emails")["id"], 7)
        self.assertEqual(len(client.fetch_all_rows("ws", 7)), 3)
        with self.assertRaises(s.GrappleError):
            client.find_project("ws", "Nope")

    def test_http_error_raises(self):
        response = mock.Mock(status_code=401, text='{"message":"Unauthenticated."}')
        session = mock.Mock()
        session.headers = {}
        session.get.return_value = response
        with self.assertRaises(s.GrappleError):
            s.GrappleClient("key", session=session).workspace()


if __name__ == "__main__":
    unittest.main()
