# market-updates → Confluence Sync

Automatically syncs SP market commitment messages from `#market-updates` Slack channel to the [Expected SP Markets Priced](https://betprophet.atlassian.net/wiki/spaces/AP/pages/1427013765/Expected+SP+Markets+Priced) Confluence page.

Runs every hour via GitHub Actions.

## How it works

1. Reads all new messages from `#market-updates` since the last run
2. Pre-filters for messages that look like SP market commitments
3. Sends relevant messages + current Confluence page HTML to Claude
4. Claude surgically updates only the relevant SP section(s)
5. Writes the updated page back to Confluence
6. Saves the last-processed Slack timestamp to `last_processed_ts.txt`

## Setup

### 1. Slack Bot Token

Create a Slack App at https://api.slack.com/apps with these **Bot Token Scopes**:
- `channels:history` — read public channel messages
- `channels:read` — list channels to resolve `#market-updates` ID
- `groups:history` — read private channel messages (if needed)
- `groups:read` — list private channels (if needed)

Install the app to your workspace, invite it to `#market-updates`:
```
/invite @your-bot-name
```

Copy the **Bot User OAuth Token** (`xoxb-...`).

### 2. Confluence API Token

1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Create a token named `github-actions-confluence`
3. Note your Atlassian account email and the token

### 3. GitHub Secrets

In `DaraghProphet/marketsteam` → Settings → Secrets and variables → Actions, add:

| Secret name | Value |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` from step 1 |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `CONFLUENCE_EMAIL` | Your Atlassian account email |
| `CONFLUENCE_API_TOKEN` | Token from step 2 |

### 4. Add files to repo

Add to the root of `DaraghProphet/marketsteam`:
- `sync_market_updates.py`
- `.github/workflows/sync_market_updates.yml`

The `last_processed_ts.txt` file is created automatically on first run.

### 5. Test manually

Go to Actions → **Sync #market-updates → Confluence** → **Run workflow** to trigger it immediately and check the logs.

## Message format

The bot understands natural language — no special formatting required in Slack. Examples that will be picked up:

> WhiteSwan will price live NHL markets (moneyline, spread, total) starting June 1

> OddsReactor committed to pricing NBA moneyline and spread

> New SP joining: Apex Sports — will cover NFL moneyline, spread, total and NBA moneyline

Messages that are clearly not market commitments (emoji reactions, general chat, questions) are ignored.

## Troubleshooting

- **Nothing being updated:** Check the Actions log. If Claude's output is rejected (sanity check fails), it means the response was truncated — this shouldn't happen with normal page sizes but can be checked in logs.
- **Wrong SP being updated:** Claude uses fuzzy name matching. If an SP name in Slack differs from the page, you may need to use the canonical name.
- **Rate limits:** The script runs once per hour, well within Slack and Confluence API limits.
