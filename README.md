# grapple-to-slack

Posts a daily digest of the **Emails** project in the Grapple workspace
**Grapple Marketing** to the **#gtm** Slack channel, every weekday at
9:00 AM Central (America/Chicago).

The digest covers **yesterday** (Friday through Sunday on Mondays) and
**month to date**:

- campaign emails sent (plus manual sends)
- replies received, split into replies from people and auto-replies / out-of-office
- reply rate (overall and human-only)
- a per-campaign table of sent / replies / human replies
- the list of leads who replied yesterday (email, campaign, subject), with
  a :star: on any lead Grapple's `AiInterestValue` flags as interested

Data comes from the [Grapple REST API](https://docs.askgrapple.com/api)
(`GET /me`, `GET .../projects`, `GET .../projects/{id}/data`). The API has no
server-side filtering, so the job pulls every row and filters locally. Day
boundaries are computed in America/Chicago.

## How it runs

`.github/workflows/daily-digest.yml` runs on a GitHub Actions cron. GitHub cron
is UTC-only, so two schedules are registered (14:03 and 15:03 UTC) and the
script's `--require-local-hour 9` flag makes only the one that lands at
9 AM local actually post. The workflow passes the triggering cron expression
so the check uses the scheduled hour, not the actual start time, and still
posts if GitHub starts the job late. That keeps the send time stable across
daylight saving changes.

GitHub only registers a workflow's cron from the default branch, and it does
so when a commit is pushed to that branch. If the schedule ever stops firing
(for example after changing the default branch), push any commit touching the
workflow file to the default branch, or run it manually from the Actions tab.

## One-time setup

### 1. Slack app

1. Create a Slack app in the Grapple Slack workspace (https://api.slack.com/apps,
   "From scratch").
2. Under **OAuth & Permissions → Bot Token Scopes** add `chat:write`.
3. **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-...`).
4. In Slack, open #gtm and run `/invite @<your app name>` so the bot can post there.

### 2. Repository secrets and variables

In the GitHub repo go to **Settings → Secrets and variables → Actions**.

| Name | Type | Value |
| --- | --- | --- |
| `GRAPPLE_API_KEY` | Secret | Workspace API key from Grapple (Workspaces → settings → API Keys) |
| `SLACK_BOT_TOKEN` | Secret | The `xoxb-...` token from step 1 |
| `SLACK_CHANNEL` | Variable (optional) | Defaults to `#gtm`. Use a channel ID for private channels. |

### 3. Test it

**Actions → Daily GTM email digest → Run workflow.** Tick *dry run* to see the
message in the job log without posting, or leave it unticked to post to Slack
right away. The *report date* input lets you re-run a specific day.

## Running locally

```bash
pip install -r requirements.txt
export GRAPPLE_API_KEY=...
python summarize.py --dry-run                 # print the message, don't post
python summarize.py --dry-run --date 2026-09-02
export SLACK_BOT_TOKEN=xoxb-...
python summarize.py --channel '#gtm'          # post for real
```

Run the tests with:

```bash
python -m unittest discover -s tests -v
```

## Changing things

- **Send time:** edit the two cron lines in `daily-digest.yml` (keep them one
  hour apart, matching the timezone's two UTC offsets) and the
  `--require-local-hour` value in the same file.
- **Timezone:** set `REPORT_TIMEZONE` in the workflow (any IANA name).
- **Channel:** set the `SLACK_CHANNEL` repository variable.
- **What counts as an auto-reply:** `AUTO_REPLY_PATTERN` in `summarize.py`.
- **Project name:** set `GRAPPLE_PROJECT` if the Grapple project is renamed.

## Notes

- Monday's digest covers Friday through Sunday so weekend replies are not
  lost. Passing `--date` with a Sunday does the same.
- `UeType` mapping used: 1 = sent from campaign, 2 = received, 3 = sent manually.

## License

[MIT](LICENSE)
