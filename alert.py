#!/usr/bin/env python3
"""Alert Slack when a lead replies and is labelled interested in Grapple.

Polls the Grapple "Emails" project, finds received emails whose
AiInterestValue is at or above the interest threshold, and posts one Slack
message per reply that has not been alerted before. Alerted email IDs are
kept in a small JSON state file so re-runs never repeat an alert.

Environment variables:
    GRAPPLE_API_KEY     Grapple workspace API key (required).
    SLACK_BOT_TOKEN     Slack bot token with chat:write (required unless --dry-run).
    SLACK_CHANNEL       Channel to post to (default: #gtm).
    GRAPPLE_PROJECT     Project name (default: Emails).
    REPORT_TIMEZONE     IANA timezone for displayed times (default: America/Chicago).
    ALERT_STATE_FILE    Path of the state file (default: state/alerted.json).
    ALERT_MIN_INTEREST  Minimum AiInterestValue that counts as interested (default: 1).
    ALERT_LOOKBACK_DAYS How far back to consider replies (default: 7).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import requests

from summarize import (
    DEFAULT_CHANNEL,
    DEFAULT_PROJECT,
    DEFAULT_TIMEZONE,
    Email,
    GrappleClient,
    GrappleError,
    SlackError,
    parse_rows,
    post_to_slack,
)

DEFAULT_STATE_FILE = "state/alerted.json"
DEFAULT_MIN_INTEREST = 1
DEFAULT_LOOKBACK_DAYS = 7
STATE_RETENTION_DAYS = 90

# Instantly-style interest statuses: anything >= 1 is a positive signal.
INTEREST_LABELS = {
    1: "Interested",
    2: "Meeting booked",
    3: "Meeting completed",
    4: "Closed",
}


def interest_label(value: int | None) -> str:
    if value is None:
        return "Unknown"
    return INTEREST_LABELS.get(value, f"Interest level {value}")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class AlertState:
    alerted: dict[str, str] = field(default_factory=dict)  # email id -> ISO timestamp of the email

    @classmethod
    def load(cls, path: Path) -> "AlertState":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"State file {path} is not valid JSON: {exc}") from exc
        alerted = data.get("alerted", {})
        if not isinstance(alerted, dict):
            raise RuntimeError(f"State file {path} has an unexpected shape.")
        return cls(alerted={str(k): str(v) for k, v in alerted.items()})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"alerted": dict(sorted(self.alerted.items(), key=lambda kv: kv[1]))}
        path.write_text(json.dumps(payload, indent=2) + "\n")

    def prune(self, now: datetime, retention_days: int = STATE_RETENTION_DAYS) -> None:
        cutoff = now - timedelta(days=retention_days)
        self.alerted = {
            k: v for k, v in self.alerted.items() if datetime.fromisoformat(v) >= cutoff
        }


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def find_new_interested(
    emails: Iterable[Email],
    state: AlertState,
    *,
    now: datetime,
    min_interest: int = DEFAULT_MIN_INTEREST,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[Email]:
    """Replies at or above the interest threshold, within the lookback, not yet alerted."""
    since = now - timedelta(days=lookback_days)
    picked = [
        e
        for e in emails
        if e.is_reply
        and e.ai_interest is not None
        and e.ai_interest >= min_interest
        and e.timestamp >= since
        and e.id
        and e.id not in state.alerted
    ]
    return sorted(picked, key=lambda e: e.timestamp)


# --------------------------------------------------------------------------- #
# Slack message
# --------------------------------------------------------------------------- #
def build_alert(email: Email, *, tz: ZoneInfo, workspace_name: str, project_name: str) -> tuple[str, list[dict]]:
    label = interest_label(email.ai_interest)
    when = email.timestamp.astimezone(tz).strftime("%a %b %-d, %-I:%M %p %Z")
    subject = email.subject or "(no subject)"
    title = f":star: {label}: {email.lead}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Lead*\n{email.lead}"},
                {"type": "mrkdwn", "text": f"*Campaign*\n{email.campaign}"},
                {"type": "mrkdwn", "text": f"*Subject*\n{subject}"},
                {"type": "mrkdwn", "text": f"*Received*\n{when}"},
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"AiInterestValue {email.ai_interest} · {workspace_name} · {project_name}",
                }
            ],
        },
    ]
    fallback = f"{label}: {email.lead} replied to \"{subject}\" ({email.campaign})"
    return fallback, blocks


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timezone", default=os.environ.get("REPORT_TIMEZONE", DEFAULT_TIMEZONE))
    parser.add_argument("--project", default=os.environ.get("GRAPPLE_PROJECT", DEFAULT_PROJECT))
    parser.add_argument("--channel", default=os.environ.get("SLACK_CHANNEL", DEFAULT_CHANNEL))
    parser.add_argument("--state-file", default=os.environ.get("ALERT_STATE_FILE", DEFAULT_STATE_FILE))
    parser.add_argument(
        "--min-interest",
        type=int,
        default=int(os.environ.get("ALERT_MIN_INTEREST", DEFAULT_MIN_INTEREST)),
        help="Minimum AiInterestValue that counts as interested (default 1).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("ALERT_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)),
        help="Only consider replies received within this many days (default 7).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the alerts instead of posting, and do not update the state file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    tz = ZoneInfo(args.timezone)
    now = datetime.now(tz=timezone.utc)

    api_key = os.environ.get("GRAPPLE_API_KEY")
    if not api_key:
        print("GRAPPLE_API_KEY is not set.", file=sys.stderr)
        return 2
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    if not args.dry_run and not slack_token:
        print("SLACK_BOT_TOKEN is not set (use --dry-run to preview without posting).", file=sys.stderr)
        return 2

    state_path = Path(args.state_file)
    state = AlertState.load(state_path)

    client = GrappleClient(api_key)
    workspace = client.workspace()
    project = client.find_project(workspace["slug"], args.project)
    emails = parse_rows(client.fetch_all_rows(workspace["slug"], project["id"]))
    print(f'Fetched {len(emails)} emails from "{project["name"]}" in workspace "{workspace["name"]}".')

    new = find_new_interested(
        emails, state, now=now, min_interest=args.min_interest, lookback_days=args.lookback_days
    )
    if not new:
        print(f"No new interested replies (threshold {args.min_interest}, last {args.lookback_days} days).")
        return 0

    print(f"{len(new)} new interested repl{'y' if len(new) == 1 else 'ies'}.")
    for email in new:
        text, blocks = build_alert(
            email, tz=tz, workspace_name=workspace["name"], project_name=project["name"]
        )
        if args.dry_run:
            print(f"[dry-run] Would post to {args.channel}: {text}")
            print(json.dumps(blocks, indent=2, ensure_ascii=False))
            continue
        post_to_slack(slack_token, args.channel, text, blocks)
        print(f"Posted: {text}")
        # Record immediately so a later failure in this run cannot cause a repeat.
        state.alerted[email.id] = email.timestamp.isoformat()
        state.prune(now)
        state.save(state_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GrappleError, SlackError, RuntimeError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
