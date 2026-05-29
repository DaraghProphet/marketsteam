"""
sync_market_updates.py

Polls #market-updates Slack channel, extracts SP market commitment messages,
and surgically updates the Confluence page "Expected SP Markets Priced".

Runs hourly via GitHub Actions. Uses a state file (last_processed_ts.txt)
committed back to the repo to track which Slack messages have been processed.
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN          = os.environ["SLACK_BOT_TOKEN"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
CONFLUENCE_EMAIL     = os.environ["CONFLUENCE_EMAIL"]
CONFLUENCE_API_TOKEN = os.environ["CONFLUENCE_API_TOKEN"]

CONFLUENCE_BASE      = "https://betprophet.atlassian.net/wiki"
PAGE_ID              = "1427013765"
SLACK_CHANNEL_NAME   = "market-updates"
STATE_FILE           = "last_processed_ts.txt"
CLAUDE_MODEL         = "claude-sonnet-4-20250514"

# ── Slack helpers ─────────────────────────────────────────────────────────────

def get_channel_id(channel_name: str) -> str:
    """Resolve channel name to ID."""
    cursor = None
    while True:
        params = {"limit": 200, "types": "public_channel,private_channel"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for ch in data.get("channels", []):
            if ch["name"] == channel_name:
                return ch["id"]
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise ValueError(f"Channel #{channel_name} not found")


def fetch_messages_since(channel_id: str, oldest_ts: str) -> list[dict]:
    """Return all messages posted after oldest_ts (exclusive)."""
    messages = []
    cursor = None
    while True:
        params = {"channel": channel_id, "oldest": oldest_ts, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(
            "https://slack.com/api/conversations.history",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack error: {data.get('error')}")
        batch = [m for m in data.get("messages", []) if m.get("type") == "message" and not m.get("bot_id")]
        messages.extend(batch)
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


# ── Confluence helpers ────────────────────────────────────────────────────────

def get_confluence_page() -> tuple[str, int]:
    """Return (html_body, version_number)."""
    r = requests.get(
        f"{CONFLUENCE_BASE}/api/v2/pages/{PAGE_ID}",
        params={"body-format": "storage"},
        auth=(CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    body = data["body"]["storage"]["value"]
    version = data["version"]["number"]
    return body, version


def update_confluence_page(new_body: str, version: int, message: str) -> None:
    """Write updated body back to Confluence."""
    payload = {
        "id": PAGE_ID,
        "status": "current",
        "title": "Expected SP Markets Priced",
        "body": {
            "representation": "storage",
            "value": new_body,
        },
        "version": {
            "number": version + 1,
            "message": message,
        },
    }
    r = requests.put(
        f"{CONFLUENCE_BASE}/api/v2/pages/{PAGE_ID}",
        auth=(CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN),
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    print(f"  ✓ Confluence updated to v{version + 1}")


# ── Claude helper ─────────────────────────────────────────────────────────────

def call_claude(prompt: str) -> str:
    """Call Claude and return the text response."""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()


# ── Core logic ────────────────────────────────────────────────────────────────

def build_update_prompt(slack_messages: list[dict], current_html: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg_block = "\n".join(
        f"[{datetime.fromtimestamp(float(m['ts']), tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] {m.get('text', '')}"
        for m in slack_messages
    )
    return f"""You are updating a Confluence page that tracks which markets each Service Provider (SP) has committed to pricing on a sports prediction exchange.

The page body uses Confluence storage format HTML. SP sections use legacy-content extension blocks where the inner XML is HTML-entity-encoded inside a data-parameters attribute.

CURRENT PAGE HTML (full):
{current_html}

NEW SLACK MESSAGES FROM #market-updates (newest last):
{msg_block}

INSTRUCTIONS:
1. Parse each Slack message for SP name(s) and market commitments (sport + market types like moneyline, spread, total, live moneyline, etc.).
2. Ignore messages with no SP market commitment info (chatter, questions, emoji-only).
3. For each SP mentioned:
   - Find their <details><summary>SP Name</summary>...</details> section.
   - Inside the data-parameters attribute, locate the HTML-entity-encoded Expected column (the first &lt;ac:layout-cell&gt;).
   - Under the correct sport &lt;h4&gt; heading, replace the placeholder &lt;em&gt;(add expected markets)&lt;/em&gt; list item with real bullet points, OR append new bullet points if some already exist.
   - If the sport heading doesn't exist yet in the Expected column, add it before &lt;/ac:layout-cell&gt;.
   - If the SP doesn't exist on the page at all, append a new <details> block at the end of the SP list following the same legacy-content format as existing SPs.
4. After each new bullet point you add, append this inline note (still inside the &lt;li&gt;&lt;p&gt;...): <span style="font-size:11px;color:#888"> — added {today}</span>
5. Return ONLY the complete updated page HTML — no markdown fences, no explanation, no preamble. Preserve ALL existing content exactly, only adding new content.
"""


def filter_relevant_messages(messages: list[dict]) -> list[dict]:
    """Quick pre-filter: drop messages that are almost certainly not SP updates."""
    # Keep messages that mention common SP commitment patterns
    patterns = [
        r"\bwill\b", r"\bprice\b", r"\bpricing\b", r"\bcommit\b", r"\bmarket",
        r"\bmoneyline\b", r"\bspread\b", r"\btotal\b", r"\bprops?\b",
        r"\bnhl\b", r"\bnba\b", r"\bnfl\b", r"\bmlb\b", r"\bice hockey\b",
        r"\bbaseball\b", r"\bfootball\b", r"\bbasketball\b",
    ]
    combined = re.compile("|".join(patterns), re.IGNORECASE)
    return [m for m in messages if combined.search(m.get("text", ""))]


def load_last_ts() -> str:
    """Load the last-processed Slack timestamp from state file."""
    if os.path.exists(STATE_FILE):
        ts = open(STATE_FILE).read().strip()
        if ts:
            return ts
    # Default: look back 25 hours on first run
    return str(time.time() - 90000)


def save_last_ts(ts: str) -> None:
    with open(STATE_FILE, "w") as f:
        f.write(ts)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting sync...")

    # 1. Load state
    last_ts = load_last_ts()
    print(f"  Fetching messages since ts={last_ts}")

    # 2. Fetch Slack messages
    channel_id = get_channel_id(SLACK_CHANNEL_NAME)
    messages = fetch_messages_since(channel_id, last_ts)
    print(f"  Found {len(messages)} new message(s)")

    if not messages:
        print("  Nothing to do.")
        return

    # 3. Pre-filter for relevance
    relevant = filter_relevant_messages(messages)
    print(f"  {len(relevant)} message(s) look like SP market updates")

    if not relevant:
        # Still advance the timestamp so we don't re-process
        newest_ts = max(m["ts"] for m in messages)
        save_last_ts(newest_ts)
        print("  No relevant messages — timestamp advanced, done.")
        return

    # 4. Fetch current Confluence page
    print("  Fetching Confluence page...")
    current_html, version = get_confluence_page()
    print(f"  Page is at v{version}, {len(current_html):,} chars")

    # 5. Ask Claude to merge updates
    print("  Calling Claude to merge updates...")
    prompt = build_update_prompt(relevant, current_html)
    updated_html = call_claude(prompt)

    # Sanity check: updated body should be at least as large as original
    if len(updated_html) < len(current_html) * 0.9:
        raise RuntimeError(
            f"Claude returned suspiciously short body ({len(updated_html):,} chars vs "
            f"{len(current_html):,} original). Aborting to avoid data loss."
        )

    # 6. Write back to Confluence
    sp_names = ", ".join(
        set(re.findall(r"\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)\b", " ".join(m.get("text","") for m in relevant)))[:5]
    )
    version_msg = f"Auto-update from #market-updates ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    print("  Writing updated page to Confluence...")
    update_confluence_page(updated_html, version, version_msg)

    # 7. Advance state
    newest_ts = max(m["ts"] for m in messages)
    save_last_ts(newest_ts)
    print(f"  State advanced to ts={newest_ts}")
    print("  Done ✓")


if __name__ == "__main__":
    main()
