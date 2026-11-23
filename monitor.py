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
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.fortnite.com/ecosystem/v1/islands/{}"
STATE_FILE = Path("state.json")
FAILS_BEFORE_ALERT = 2  # consecutive "not found" results before pinging
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
        return {"status": "up", "fails": 0}


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
    new_state = dict(state)

    if result == "up":
        if state["status"] == "down":
            send_discord(webhook, user_id, f"🟢 `{code}` is back in discovery ({details}).")
        new_state = {"status": "up", "fails": 0}
    elif state["status"] != "down":  # already alerted -> nothing to do
        new_state["fails"] = state.get("fails", 0) + 1
        if new_state["fails"] >= FAILS_BEFORE_ALERT and state["status"] != "down":
            send_discord(
                webhook,
                user_id,
                f"🔴 `{code}` is gone from discovery — probably banned or unlisted. "
                f"Check Creator Portal.\n`{details}`",
            )
            new_state["status"] = "down"

    if new_state != state or not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(new_state) + "\n")


if __name__ == "__main__":
    main()
