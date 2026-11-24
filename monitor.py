"""Fortnite island monitor -> Discord webhooks.

Checks the island in the official Fortnite Data API. The API only returns
public, discoverable islands, so "not found" means the island was banned,
delisted or unlisted.

Modes (MODE env var):
  check   ping you once when the island disappears, once when it's back
  report  short status + last-hour stats to the report channel (no ping)
  test    test message to the alerts channel with the current status

Env vars:
  DISCORD_WEBHOOK_URL         alerts channel webhook (secret)
  DISCORD_REPORT_WEBHOOK_URL  report channel webhook (secret, for MODE=report)
  DISCORD_USER_ID             your Discord user ID (gets pinged on alerts)
  ISLAND_CODE                 e.g. 1234-5678-9012
  MODE                        check | report | test (default: check)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.fortnite.com/ecosystem/v1/islands/{}"
STATE_FILE = Path("state.json")
RETRIES = 2            # extra checks right after a "not found", before pinging
RETRY_DELAY_SEC = 20   # pause between those checks
USER_AGENT = "PitMonitor/1.1"


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing env var: {name}")
    return value


def api_get(path: str):
    req = urllib.request.Request(
        path, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def check_island(code: str) -> tuple[str, str]:
    """Returns ("up" | "down" | "error", details)."""
    try:
        data = api_get(API_BASE.format(code))
        return "up", data.get("title") or code
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500:
            return "error", f"HTTP {e.code} (API problem, not a ban)"
        body = e.read().decode(errors="replace")[:300]
        return "down", f"HTTP {e.code}: {body}"
    except (urllib.error.URLError, TimeoutError) as e:
        return "error", f"Network error: {e}"


def last_hour_metrics(code: str) -> dict:
    """Latest non-empty hourly value for each metric. Empty dict on failure."""
    try:
        data = api_get(API_BASE.format(code) + "/metrics/hour")
    except Exception as e:  # stats are nice-to-have; never break the report
        print(f"metrics error: {e}")
        return {}
    result = {}
    for key in ("uniquePlayers", "plays", "peakCCU", "minutesPlayed",
                "averageMinutesPerPlayer", "favorites", "recommendations"):
        points = data.get(key) or []
        points = [p for p in points if isinstance(p, dict) and p.get("value") is not None]
        if points:
            latest = max(points, key=lambda p: p.get("timestamp", ""))
            result[key] = latest["value"]
    return result


def send_discord(webhook: str, text: str, ping_user: str | None = None) -> None:
    payload = {"content": text, "allowed_mentions": {"parse": []}}
    if ping_user:
        payload["content"] = f"<@{ping_user}> {text}"
        payload["allowed_mentions"] = {"users": [ping_user]}
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


def fmt(n) -> str:
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.1f}"
    return f"{int(n):,}"


def run_report(code: str, result: str, details: str) -> None:
    webhook = env("DISCORD_REPORT_WEBHOOK_URL")
    icon = {"up": "🟢", "down": "🔴", "error": "🟡"}[result]
    lines = [f"📊 **Hourly report** — `{code}`", f"{icon} Status: **{result}** — {details}"]

    if result == "up":
        m = last_hour_metrics(code)
        labels = [
            ("uniquePlayers", "Unique players"),
            ("plays", "Plays"),
            ("peakCCU", "Peak CCU"),
            ("minutesPlayed", "Minutes played"),
            ("averageMinutesPerPlayer", "Avg min / player"),
            ("favorites", "Favorites"),
            ("recommendations", "Recommends"),
        ]
        stats = [f"{label}: **{fmt(m[k])}**" for k, label in labels if k in m]
        lines.append("Last hour: " + " · ".join(stats) if stats else "Last hour: no stats yet")

    send_discord(webhook, "\n".join(lines))


def main() -> None:
    code = env("ISLAND_CODE")
    mode = (os.environ.get("MODE") or "check").strip().lower()

    result, details = check_island(code)
    print(f"[{mode}] {code}: {result} ({details})")

    if mode == "report":
        run_report(code, result, details)
        return

    webhook = env("DISCORD_WEBHOOK_URL")
    user_id = env("DISCORD_USER_ID")

    if mode == "test":
        icon = {"up": "🟢", "down": "🔴", "error": "🟡"}[result]
        send_discord(
            webhook,
            f"🧪 Test message: monitor works.\n{icon} `{code}` is currently **{result}** — {details}",
            ping_user=user_id,
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
        send_discord(webhook, f"🟢 `{code}` is back in discovery ({details}).", ping_user=user_id)
    elif new_state["status"] == "down" and state["status"] != "down":
        send_discord(
            webhook,
            f"🔴 `{code}` is gone from discovery — probably banned or unlisted. "
            f"Check Creator Portal.\n`{details}`",
            ping_user=user_id,
        )

    if new_state != state or not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(new_state) + "\n")


if __name__ == "__main__":
    main()
