"""Fortnite island monitor -> Discord webhooks.

Checks the island in the official Fortnite Data API. The API only returns
public, discoverable islands, so "not found" means the island was banned,
delisted or unlisted.

Modes (MODE env var):
  check   ping you once when the island disappears, once when it's back
  report  short status + last-hour stats (no ping)
  test    test message with the current status

Env vars:
  DISCORD_WEBHOOK_URL         channel webhook (secret)
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


METRIC_KEYS = ("uniquePlayers", "plays", "peakCCU", "minutesPlayed",
               "averageMinutesPerPlayer", "favorites", "recommendations")
LOCAL_TZ = "Europe/Kyiv"


def parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def last_hour_metrics(code: str) -> tuple[datetime | None, dict]:
    """All metrics from the latest *complete* hour that has data.
    Returns (hour_start_utc, {metric: value}); (None, {}) on failure."""
    try:
        data = api_get(API_BASE.format(code) + "/metrics/hour")
    except Exception as e:  # stats are nice-to-have; never break the report
        print(f"metrics error: {e}")
        return None, {}

    by_hour: dict[datetime, dict] = {}
    for key in METRIC_KEYS:
        for p in data.get(key) or []:
            if not isinstance(p, dict) or p.get("value") is None:
                continue
            ts = parse_ts(p.get("timestamp", ""))
            if ts:
                by_hour.setdefault(ts, {})[key] = p["value"]

    now = datetime.now(timezone.utc)
    complete = [h for h, m in by_hour.items()
                if h + timedelta(hours=1) <= now and "plays" in m]
    if not complete:
        return None, {}
    hour = max(complete)
    return hour, by_hour[hour]


def hour_label(hour_utc: datetime) -> str:
    try:
        tz = ZoneInfo(LOCAL_TZ)
    except Exception:
        tz = ZoneInfo("Europe/Kiev")
    start = hour_utc.astimezone(tz)
    end = start + timedelta(hours=1)
    return f"{start:%H:%M}–{end:%H:%M} Kyiv"


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


def run_report(webhook: str, code: str, result: str, details: str) -> None:
    icon = {"up": "🟢", "down": "🔴", "error": "🟡"}[result]
    lines = [f"📊 **Hourly report** — `{code}`", f"{icon} Status: **{result}** — {details}"]

    if result == "up":
        hour, m = last_hour_metrics(code)
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
        if stats:
            lines.append(f"Last hour ({hour_label(hour)}): " + " · ".join(stats))
        else:
            lines.append("Last hour: no stats yet")

    send_discord(webhook, "\n".join(lines))


def main() -> None:
    code = env("ISLAND_CODE")
    mode = (os.environ.get("MODE") or "check").strip().lower()

    webhook = env("DISCORD_WEBHOOK_URL")
    user_id = env("DISCORD_USER_ID")

    result, details = check_island(code)
    print(f"[{mode}] {code}: {result} ({details})")

    if mode == "report":
        run_report(webhook, code, result, details)
        return

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
