"""Fortnite island monitor -> Discord webhook.

Checks the island in the official Fortnite Data API. The API only returns
public, discoverable islands, so "not found" means the island was banned,
delisted or unlisted. Pings you once when it goes down and once when it's back.

Env vars:
  DISCORD_WEBHOOK_URL  webhook URL (secret)
  DISCORD_USER_ID      your Discord user ID (gets pinged)
  ISLAND_CODE          e.g. 1234-5678-9012
  TEST_MODE            "true" -> just send a test message with the current status
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.fortnite.com/ecosystem/v1/islands/{}"
STATE_FILE = Path("state.json")
RETRIES = 2            # extra checks right after a "not found", before pinging
RETRY_DELAY_SEC = 20   # pause between those checks
USER_AGENT = "PitMonitor/1.0"


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing env var: {name}")
    return value


def check_island(code: str) -> tuple[str, str]:
    """Returns ("up" | "down" | "error", details)."""
    req = urllib.request.Request(
        API_URL.format(code),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
            return "up", data.get("title") or code
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500:
            return "error", f"HTTP {e.code} (API problem, not a ban)"
        body = e.read().decode(errors="replace")[:300]
        return "down", f"HTTP {e.code}: {body}"
    except (urllib.error.URLError, TimeoutError) as e:
        return "error", f"Network error: {e}"


def send_discord(webhook: str, user_id: str, text: str) -> None:
    payload = {
        "content": f"<@{user_id}> {text}",
        "allowed_mentions": {"users": [user_id]},
    }
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        sys.exit(f"Discord webhook failed: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "up"}


def main() -> None:
    webhook = env("DISCORD_WEBHOOK_URL")
    user_id = env("DISCORD_USER_ID")
    code = env("ISLAND_CODE")
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"

    result, details = check_island(code)
    print(f"{code}: {result} ({details})")

    if test_mode:
        icon = {"up": "🟢", "down": "🔴", "error": "🟡"}[result]
        send_discord(
            webhook,
            user_id,
            f"🧪 Test message: monitor works.\n{icon} `{code}` is currently **{result}** — {details}",
        )
        return

    if result == "error":
        return  # API hiccup: don't alert, don't change state

    state = load_state()

    # Island just disappeared: re-check right away to rule out a one-off glitch.
    if result == "down" and state["status"] != "down":
        for attempt in range(1, RETRIES + 1):
            time.sleep(RETRY_DELAY_SEC)
            retry_result, retry_details = check_island(code)
            print(f"retry {attempt}/{RETRIES}: {retry_result} ({retry_details})")
            if retry_result == "up":
                result, details = retry_result, retry_details
                break

    new_state = {"status": "up" if result == "up" else "down"}

    if new_state["status"] == "up" and state["status"] == "down":
        send_discord(webhook, user_id, f"🟢 `{code}` is back in discovery ({details}).")
    elif new_state["status"] == "down" and state["status"] != "down":
        send_discord(
            webhook,
            user_id,
            f"🔴 `{code}` is gone from discovery — probably banned or unlisted. "
            f"Check Creator Portal.\n`{details}`",
        )

    if new_state != state or not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(new_state) + "\n")


if __name__ == "__main__":
    main()
