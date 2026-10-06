# grapple-to-slack

Watches the **Emails** project in the Grapple workspace **Grapple Marketing**
and posts an alert to the **#gtm** Slack channel whenever a lead replies and
is labelled **interested**.

## Interested-reply alert (`alert.py`)

`.github/workflows/interested-alert.yml` runs every hour. Each run:

1. pulls every row from the Grapple REST API,
2. keeps received emails (`UeType` 2) whose `AiInterestValue` is at or above
   the threshold (default **1**) and that arrived within the last 7 days,
3. posts one Slack message per reply not alerted before, and
4. records the alerted email IDs in `state/alerted.json`, which the workflow
   commits back to the repo so re-runs never repeat an alert.

`AiInterestValue` follows Instantly's interest scale, so negative values are
lost or not interested, 0 is neutral or out-of-office, and 1 or more is a
positive signal. The alert labels them as Interested (1), Meeting booked (2),
Meeting completed (3) and Closed (4).

A delayed or skipped hourly run is harmless: the next run catches up on
anything since the last one.

Each alert shows the lead's email, campaign, subject, received time
(America/Chicago) and the raw interest value.

## About `summarize.py`

`summarize.py` holds the shared Grapple client, row parsing and Slack
posting code that `alert.py` imports. It can also still produce the earlier
7-day digest locally (`python summarize.py --dry-run`), but nothing runs it
automatically.

Data comes from the [Grapple REST API](https://docs.askgrapple.com/api)
(`GET /me`, `GET .../projects`, `GET .../projects/{id}/data`). The API has no
server-side filtering, so both scripts pull every row and filter locally.

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

**Actions → Interested reply alert → Run workflow.** Tick *dry run* to see
what would be posted in the job log without posting or updating the state
file. With no interested replies the log simply says so.

## Running locally

```bash
pip install -r requirements.txt
export GRAPPLE_API_KEY=...
python alert.py --dry-run                         # preview alerts, don't post or save state
python alert.py --dry-run --min-interest 0        # preview the format using neutral replies
export SLACK_BOT_TOKEN=xoxb-...
python alert.py                                   # post for real and update state/alerted.json
python summarize.py --dry-run                     # the old 7-day digest, local only
```

Run the tests with:

```bash
python -m unittest discover -s tests -v
```

## Changing things

- **Interest threshold:** set `ALERT_MIN_INTEREST` in `interested-alert.yml`
  (default 1). Use 2 to alert only on meetings booked or better.
- **How often it checks:** edit the cron line in `interested-alert.yml`.
- **Lookback:** `ALERT_LOOKBACK_DAYS` (default 7) bounds how old a reply can
  be and still trigger an alert, for example after a long outage.
- **Channel:** set the `SLACK_CHANNEL` repository variable.
- **Timezone shown in alerts:** `REPORT_TIMEZONE` in the workflow.
- **Project name:** set `GRAPPLE_PROJECT` if the Grapple project is renamed.
- **Reset alerts:** empty `state/alerted.json` to `{"alerted": {}}` and the
  next run will alert again on anything within the lookback.

## Notes

- `UeType` mapping used: 1 = sent from campaign, 2 = received, 3 = sent manually.
- The state file is committed by `github-actions[bot]` with `[skip ci]`, so
  those commits do not trigger the CI workflow.

## License

[MIT](LICENSE)
