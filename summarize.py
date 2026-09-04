#!/usr/bin/env python3
"""Post a daily digest of the Grapple "Emails" project to Slack.

Pulls every row from the Grapple project, keeps the trailing 7 days (ending
yesterday in the configured timezone), compares them with the 7 days before,
and posts a short Block Kit message to a Slack channel.

Environment variables:
    GRAPPLE_API_KEY   Grapple workspace API key (required).
    SLACK_BOT_TOKEN   Slack bot token with chat:write (required unless --dry-run).
    SLACK_CHANNEL     Channel to post to (default: #gtm).
    GRAPPLE_PROJECT   Project name to summarise (default: Emails).
    REPORT_TIMEZONE   IANA timezone for day boundaries (default: America/Chicago).
    REPORT_WINDOW_DAYS  Length of the window in days (default: 7).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import requests

GRAPPLE_BASE_URL = "https://app.askgrapple.com/api/v1"
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
PER_PAGE = 5000
HTTP_TIMEOUT = 60

DEFAULT_CHANNEL = "#gtm"
DEFAULT_PROJECT = "Emails"
DEFAULT_TIMEZONE = "America/Chicago"

# UeType values in the Emails project (Instantly "unibox" email types).
UE_CAMPAIGN_SENT = 1
UE_RECEIVED = 2
UE_MANUAL_SENT = 3

# Subjects that mark a reply as an auto-responder rather than a human reply.
AUTO_REPLY_PATTERN = re.compile(
    r"automatic reply|auto-?reply|auto-?response|autoresponder"
    r"|out of (?:the )?office|out-of-office|\booo\b|\bpto\b"
    r"|on vacation|on leave|on holiday|slow to reply"
    r"|away from (?:my|the) (?:desk|office)|maternity|paternity",
    re.IGNORECASE,
)

MAX_LISTED_REPLIES = 20
MAX_LISTED_CAMPAIGNS = 15
UNKNOWN_CAMPAIGN = "(no campaign)"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Email:
    id: str
    lead: str
    subject: str
    timestamp: datetime  # timezone-aware
    ue_type: int | None
    ai_interest: int | None
    campaign: str

    @property
    def is_campaign_sent(self) -> bool:
        return self.ue_type == UE_CAMPAIGN_SENT

    @property
    def is_manual_sent(self) -> bool:
        return self.ue_type == UE_MANUAL_SENT

    @property
    def is_reply(self) -> bool:
        return self.ue_type == UE_RECEIVED

    @property
    def is_auto_reply(self) -> bool:
        return self.is_reply and bool(AUTO_REPLY_PATTERN.search(self.subject))

    @property
    def is_human_reply(self) -> bool:
        return self.is_reply and not self.is_auto_reply

    @property
    def is_interested(self) -> bool:
        return self.ai_interest is not None and self.ai_interest > 0


@dataclass(frozen=True)
class Window:
    label: str
    start: datetime  # inclusive, timezone-aware
    end: datetime  # exclusive, timezone-aware

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


@dataclass
class CampaignStats:
    sent: int = 0
    replies: int = 0
    human_replies: int = 0


@dataclass
class Stats:
    sent: int = 0
    manual_sent: int = 0
    replies: int = 0
    human_replies: int = 0
    auto_replies: int = 0
    interested: int = 0
    by_campaign: dict[str, CampaignStats] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Parsing Grapple rows
# --------------------------------------------------------------------------- #
def parse_timestamp(value) -> datetime | None:
    """Parse the timestamp shapes Grapple emits for date fields."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        inner = value.get("$date")
        if isinstance(inner, dict):
            inner = inner.get("$numberLong")
        if inner is None:
            return None
        return parse_timestamp(inner)
    if isinstance(value, (int, float)):
        millis = float(value)
        # Heuristic: anything larger than year 3000 in seconds is milliseconds.
        seconds = millis / 1000 if millis > 32_503_680_000 else millis
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", stripped):
            return parse_timestamp(float(stripped))
        iso = stripped[:-1] + "+00:00" if stripped.endswith("Z") else stripped
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_row(cells: Sequence[dict]) -> Email | None:
    """Turn one Grapple row (a list of {key,name,value} cells) into an Email."""
    values = {cell.get("name"): cell.get("value") for cell in cells if isinstance(cell, dict)}
    timestamp = parse_timestamp(values.get("TimestampEmail"))
    if timestamp is None:
        return None
    return Email(
        id=str(values.get("Id") or ""),
        lead=str(values.get("Lead") or "").strip(),
        subject=str(values.get("Subject") or "").strip(),
        timestamp=timestamp,
        ue_type=_as_int(values.get("UeType")),
        ai_interest=_as_int(values.get("AiInterestValue")),
        campaign=str(values.get("Name") or "").strip() or UNKNOWN_CAMPAIGN,
    )


def parse_rows(rows: Iterable[Sequence[dict]]) -> list[Email]:
    emails = [parse_row(row) for row in rows]
    return [email for email in emails if email is not None]


# --------------------------------------------------------------------------- #
# Grapple API client
# --------------------------------------------------------------------------- #
class GrappleError(RuntimeError):
    pass


class GrappleClient:
    def __init__(self, api_key: str, base_url: str = GRAPPLE_BASE_URL, session=None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=HTTP_TIMEOUT)
        if response.status_code != 200:
            detail = response.text[:300]
            raise GrappleError(f"GET {path} failed with HTTP {response.status_code}: {detail}")
        return response.json()

    def workspace(self) -> dict:
        return self._get("/me")["workspace"]

    def find_project(self, workspace_slug: str, name: str) -> dict:
        projects = self._get(f"/workspaces/{workspace_slug}/projects")["data"]
        matches = [p for p in projects if p.get("name", "").strip().lower() == name.strip().lower()]
        if not matches:
            available = ", ".join(sorted(p.get("name", "?") for p in projects)) or "none"
            raise GrappleError(f'Project "{name}" not found in workspace "{workspace_slug}". Available: {available}')
        return matches[0]

    def fetch_all_rows(self, workspace_slug: str, project_id: int) -> list[list[dict]]:
        rows: list[list[dict]] = []
        page = 1
        while True:
            payload = self._get(
                f"/workspaces/{workspace_slug}/projects/{project_id}/data",
                params={"page": page, "per_page": PER_PAGE},
            )
            rows.extend(payload.get("data", []))
            if not payload.get("meta", {}).get("has_more"):
                return rows
            page += 1


# --------------------------------------------------------------------------- #
# Reporting windows and statistics
# --------------------------------------------------------------------------- #
DEFAULT_WINDOW_DAYS = 7


def report_windows(report_date: date, tz: ZoneInfo, days: int = DEFAULT_WINDOW_DAYS) -> tuple[Window, Window]:
    """Return (current, previous) windows of `days` days each.

    The current window ends at the end of `report_date` (inclusive); the
    previous window is the `days` days immediately before it.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    end = datetime(report_date.year, report_date.month, report_date.day, tzinfo=tz) + timedelta(days=1)
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    return (
        Window(_range_label(start, end), start, end),
        Window(_range_label(prev_start, start), prev_start, start),
    )


def _range_label(start: datetime, end: datetime) -> str:
    """Label like 'Aug 29 – Sep 4' for [start, end)."""
    last_day = end - timedelta(days=1)
    if start.date() == last_day.date():
        return start.strftime("%b %-d")
    return f"{start.strftime('%b %-d')} – {last_day.strftime('%b %-d')}"


def default_report_date(now: datetime, tz: ZoneInfo) -> date:
    return (now.astimezone(tz) - timedelta(days=1)).date()


def emails_in(emails: Iterable[Email], window: Window) -> list[Email]:
    return [email for email in emails if window.contains(email.timestamp)]


def compute_stats(emails: Iterable[Email]) -> Stats:
    stats = Stats()
    by_campaign: dict[str, CampaignStats] = defaultdict(CampaignStats)
    for email in emails:
        campaign = by_campaign[email.campaign]
        if email.is_campaign_sent:
            stats.sent += 1
            campaign.sent += 1
        elif email.is_manual_sent:
            stats.manual_sent += 1
        elif email.is_reply:
            stats.replies += 1
            campaign.replies += 1
            if email.is_human_reply:
                stats.human_replies += 1
                campaign.human_replies += 1
            else:
                stats.auto_replies += 1
        if email.is_interested:
            stats.interested += 1
    stats.by_campaign = dict(
        sorted(by_campaign.items(), key=lambda item: (-item[1].sent, -item[1].replies, item[0]))
    )
    return stats


def rate_value(numerator: int, denominator: int) -> float | None:
    return None if denominator <= 0 else 100 * numerator / denominator


def rate(numerator: int, denominator: int) -> str:
    value = rate_value(numerator, denominator)
    return "n/a" if value is None else f"{value:.1f}%"


def delta(current: int, previous: int) -> str:
    """Signed change, e.g. '+12', '-3', '±0'."""
    change = current - previous
    if change == 0:
        return "±0"
    return f"{change:+d}"


def rate_delta(current: float | None, previous: float | None) -> str:
    if current is None or previous is None:
        return "n/a"
    change = current - previous
    if abs(change) < 0.05:
        return "±0.0 pts"
    return f"{change:+.1f} pts"


# --------------------------------------------------------------------------- #
# Slack message
# --------------------------------------------------------------------------- #
def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2990]}]}


def _campaign_table(current: Stats, previous: Stats) -> str:
    if not current.by_campaign:
        return "_No campaign activity._"
    items = list(current.by_campaign.items())[:MAX_LISTED_CAMPAIGNS]
    width = min(max(len("Campaign"), *(len(name) for name, _ in items)), 40)
    lines = [f"{'Campaign':<{width}}  {'Sent':>10}  {'Replies':>10}"]
    for name, stats in items:
        prev = previous.by_campaign.get(name, CampaignStats())
        label = name if len(name) <= width else name[: width - 1] + "…"
        sent = f"{stats.sent} ({delta(stats.sent, prev.sent)})"
        replies = f"{stats.replies} ({delta(stats.replies, prev.replies)})"
        lines.append(f"{label:<{width}}  {sent:>10}  {replies:>10}")
    hidden = len(current.by_campaign) - len(items)
    if hidden > 0:
        lines.append(f"… and {hidden} more campaign(s)")
    return "```\n" + "\n".join(lines) + "\n```"


def _reply_line(email: Email) -> str:
    marker = ":star: " if email.is_interested else ""
    subject = email.subject or "(no subject)"
    return f"• {marker}{email.lead} · {email.campaign} · _{subject}_"


def _headline(current: Stats, previous: Stats) -> str:
    cur_rate = rate_value(current.replies, current.sent)
    prev_rate = rate_value(previous.replies, previous.sent)
    lines = [
        f"*Sent:* {current.sent}  ({delta(current.sent, previous.sent)})",
        f"*Replies:* {current.replies}  ({delta(current.replies, previous.replies)})"
        f"  ·  {current.human_replies} from people, {current.auto_replies} auto-reply/OOO",
        f"*Reply rate:* {rate(current.replies, current.sent)}  ({rate_delta(cur_rate, prev_rate)})",
    ]
    if current.interested or previous.interested:
        lines.append(f"*Flagged interested:* {current.interested}  ({delta(current.interested, previous.interested)})")
    return "\n".join(lines)


def build_message(
    *,
    window: Window,
    previous_window: Window,
    emails: Sequence[Email],
    previous_emails: Sequence[Email],
    workspace_name: str,
    project_name: str,
    tz_name: str,
    generated_at: datetime,
) -> tuple[str, list[dict]]:
    current = compute_stats(emails)
    previous = compute_stats(previous_emails)
    days = (window.end - window.start).days
    title = f"GTM Email Digest — Last {days} days ({window.label})"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        _context(f"Change in parentheses is vs. the previous {days} days ({previous_window.label})."),
        _section(_headline(current, previous)),
        _section(f"*By campaign*\n{_campaign_table(current, previous)}"),
    ]

    human = sorted((e for e in emails if e.is_human_reply), key=lambda e: e.timestamp, reverse=True)
    if human:
        listed = human[:MAX_LISTED_REPLIES]
        text = f"*Replies from people ({len(human)})*\n" + "\n".join(_reply_line(e) for e in listed)
        if len(human) > len(listed):
            text += f"\n… and {len(human) - len(listed)} more"
        blocks.append(_section(text))
    else:
        blocks.append(_section("*Replies from people*\n_None in this period._"))

    blocks.append(
        _context(
            f"{workspace_name} · {project_name} · days in {tz_name} · "
            f"generated {generated_at.strftime('%Y-%m-%d %H:%M %Z')}"
        )
    )

    fallback = (
        f"{title}: {current.sent} sent ({delta(current.sent, previous.sent)}), "
        f"{current.replies} replies ({delta(current.replies, previous.replies)}), "
        f"{current.human_replies} from people."
    )
    return fallback, blocks


# --------------------------------------------------------------------------- #
# Slack API
# --------------------------------------------------------------------------- #
class SlackError(RuntimeError):
    pass


def post_to_slack(token: str, channel: str, text: str, blocks: list[dict], session=None) -> dict:
    session = session or requests.Session()
    response = session.post(
        SLACK_POST_MESSAGE_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False},
        timeout=HTTP_TIMEOUT,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SlackError(f"Slack returned non-JSON response (HTTP {response.status_code})") from exc
    if not payload.get("ok"):
        error = payload.get("error", "unknown_error")
        hint = ""
        if error == "not_in_channel":
            hint = f" — invite the bot to {channel} (/invite @YourApp) and retry."
        elif error == "channel_not_found":
            hint = " — check SLACK_CHANNEL; private channels need the bot invited and the channel ID."
        elif error in {"invalid_auth", "not_authed", "token_revoked"}:
            hint = " — check SLACK_BOT_TOKEN."
        elif error == "missing_scope":
            hint = " — the bot token needs the chat:write scope."
        raise SlackError(f"Slack chat.postMessage failed: {error}{hint}")
    return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Report date (YYYY-MM-DD, local). Defaults to yesterday.")
    parser.add_argument("--timezone", default=os.environ.get("REPORT_TIMEZONE", DEFAULT_TIMEZONE))
    parser.add_argument("--project", default=os.environ.get("GRAPPLE_PROJECT", DEFAULT_PROJECT))
    parser.add_argument("--channel", default=os.environ.get("SLACK_CHANNEL", DEFAULT_CHANNEL))
    parser.add_argument(
        "--days",
        type=int,
        default=int(os.environ.get("REPORT_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)),
        help="Length of the reporting window in days (default 7). The comparison period is the same length before it.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the message instead of posting to Slack.")
    parser.add_argument(
        "--require-local-hour",
        type=int,
        metavar="HOUR",
        help="Exit quietly unless this run corresponds to HOUR o'clock local time (used by the cron schedule).",
    )
    parser.add_argument(
        "--cron",
        help="The cron expression that triggered this run (github.event.schedule); "
        "used with --require-local-hour to pick the right one of the two UTC schedules.",
    )
    return parser.parse_args(argv)


def should_run_now(now_local: datetime, required_hour: int | None, scheduled_utc_hour: int | None = None) -> bool:
    """Decide whether a scheduled run should post.

    Two cron entries exist (one per UTC offset of the timezone). When the
    triggering cron's UTC hour is known, check whether that UTC hour lands on
    the required local hour today; this stays correct even if GitHub starts the
    job late. Otherwise fall back to the current local hour.
    """
    if required_hour is None:
        return True
    if scheduled_utc_hour is not None:
        scheduled = now_local.astimezone(timezone.utc).replace(
            hour=scheduled_utc_hour, minute=0, second=0, microsecond=0
        )
        return scheduled.astimezone(now_local.tzinfo).hour == required_hour
    return now_local.hour == required_hour


def cron_utc_hour(cron: str | None) -> int | None:
    """Extract the hour field from a 5-field cron expression, if it is a plain number."""
    if not cron:
        return None
    fields = cron.split()
    if len(fields) != 5 or not fields[1].isdigit():
        return None
    return int(fields[1])


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    tz = ZoneInfo(args.timezone)
    now_utc = datetime.now(tz=timezone.utc)
    now_local = now_utc.astimezone(tz)

    if not should_run_now(now_local, args.require_local_hour, cron_utc_hour(args.cron)):
        print(
            f"Local time is {now_local:%H:%M %Z} (cron: {args.cron or 'n/a'}); "
            f"not the {args.require_local_hour}:00 local run. Skipping."
        )
        return 0

    api_key = os.environ.get("GRAPPLE_API_KEY")
    if not api_key:
        print("GRAPPLE_API_KEY is not set.", file=sys.stderr)
        return 2
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    if not args.dry_run and not slack_token:
        print("SLACK_BOT_TOKEN is not set (use --dry-run to preview without posting).", file=sys.stderr)
        return 2

    report_date = date.fromisoformat(args.date) if args.date else default_report_date(now_utc, tz)
    window, previous_window = report_windows(report_date, tz, args.days)

    client = GrappleClient(api_key)
    workspace = client.workspace()
    project = client.find_project(workspace["slug"], args.project)
    rows = client.fetch_all_rows(workspace["slug"], project["id"])
    emails = parse_rows(rows)
    print(
        f"Fetched {len(rows)} rows ({len(emails)} with timestamps) from "
        f'"{project["name"]}" in workspace "{workspace["name"]}".'
    )

    text, blocks = build_message(
        window=window,
        previous_window=previous_window,
        emails=emails_in(emails, window),
        previous_emails=emails_in(emails, previous_window),
        workspace_name=workspace["name"],
        project_name=project["name"],
        tz_name=args.timezone,
        generated_at=now_local,
    )

    if args.dry_run:
        print(f"[dry-run] Would post to {args.channel}:\n{text}\n")
        print(json.dumps(blocks, indent=2, ensure_ascii=False))
        return 0

    result = post_to_slack(slack_token, args.channel, text, blocks)
    print(f"Posted to {result.get('channel')} at ts {result.get('ts')}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GrappleError, SlackError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
