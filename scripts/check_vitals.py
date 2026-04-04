#!/usr/bin/env python3
"""
DevClaw Vitals Check — calculates Time-To-Live (TTL) from fund balance and burn rate.

Reads:
  .claw/fund_estimate.json   — current balance
  .claw/survival_state.json  — last known state and API events
  .claw/sessions/*.jsonl     — session logs for burn rate estimation

Outputs:
  TTL=X.X days | BALANCE=$X.XX | BURN=$X.XX/day | STATUS=HEALTHY|HUNGRY|CRITICAL
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLAW_DIR = PROJECT_ROOT / ".claw"
FUND_FILE = CLAW_DIR / "fund_estimate.json"
STATE_FILE = CLAW_DIR / "survival_state.json"
SESSIONS_DIR = CLAW_DIR / "sessions"

DEFAULT_DAILY_BURN = 2.50  # fallback if no session data exists
MIN_BURN_RATE = 0.01       # floor to prevent division-by-zero TTL inflation


def load_json(path: Path) -> dict:
    """Load a JSON file, returning empty dict on any failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def get_balance() -> float:
    """Read current balance from fund_estimate.json."""
    data = load_json(FUND_FILE)
    # Support multiple key conventions
    for key in ("balance", "balance_usd", "remaining", "funds"):
        if key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return 0.0


def estimate_daily_burn() -> float:
    """
    Estimate daily burn rate by counting session activity lines across all
    .jsonl files in .claw/sessions/ and dividing by the number of active days.

    Each line in a .jsonl file represents one API event/tool call.
    """
    if not SESSIONS_DIR.is_dir():
        return DEFAULT_DAILY_BURN

    total_lines = 0
    earliest_ts = None
    latest_ts = None

    jsonl_files = sorted(SESSIONS_DIR.glob("*.jsonl"))
    if not jsonl_files:
        return DEFAULT_DAILY_BURN

    for fpath in jsonl_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    total_lines += 1

                    # Try to extract timestamp for date range calculation
                    try:
                        entry = json.loads(line)
                        ts_str = entry.get("timestamp") or entry.get("ts") or entry.get("time")
                        if ts_str:
                            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                            if earliest_ts is None or ts < earliest_ts:
                                earliest_ts = ts
                            if latest_ts is None or ts > latest_ts:
                                latest_ts = ts
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        except (PermissionError, OSError):
            continue

    if total_lines == 0:
        return DEFAULT_DAILY_BURN

    # Calculate active days from timestamp range
    if earliest_ts and latest_ts:
        delta = (latest_ts - earliest_ts).total_seconds() / 86400.0
        active_days = max(delta, 1.0)
    else:
        # Fallback: estimate from file modification times
        try:
            first_mtime = os.path.getmtime(jsonl_files[0])
            last_mtime = os.path.getmtime(jsonl_files[-1])
            delta_days = (last_mtime - first_mtime) / 86400.0
            active_days = max(delta_days, 1.0)
        except OSError:
            active_days = 1.0

    # Estimate cost per event (rough average across typical API usage)
    # This can be calibrated per-project in fund_estimate.json["cost_per_event"]
    fund_data = load_json(FUND_FILE)
    cost_per_event = float(fund_data.get("cost_per_event", 0.003))

    daily_events = total_lines / active_days
    burn_rate = daily_events * cost_per_event

    return max(burn_rate, MIN_BURN_RATE)


def get_api_event_burn() -> float | None:
    """
    Alternative burn calculation from survival_state.json API events.
    Returns None if insufficient data.
    """
    state = load_json(STATE_FILE)
    events = state.get("api_events", [])
    if not events or len(events) < 2:
        return None

    costs = []
    timestamps = []
    for ev in events:
        cost = ev.get("cost") or ev.get("cost_usd")
        ts = ev.get("timestamp") or ev.get("ts")
        if cost is not None and ts is not None:
            try:
                costs.append(float(cost))
                timestamps.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
            except (ValueError, TypeError):
                continue

    if len(costs) < 2 or len(timestamps) < 2:
        return None

    total_cost = sum(costs)
    time_span = (max(timestamps) - min(timestamps)).total_seconds() / 86400.0
    if time_span < 0.01:
        return None

    return max(total_cost / time_span, MIN_BURN_RATE)


def determine_status(ttl: float) -> str:
    """Map TTL to human-readable status."""
    if ttl > 14:
        return "HEALTHY"
    elif ttl >= 3:
        return "HUNGRY"
    else:
        return "CRITICAL"


def main() -> None:
    """Run vitals check and print formatted output."""
    balance = get_balance()

    # Try API-event-based burn first, fall back to session log estimation
    burn = get_api_event_burn()
    if burn is None:
        burn = estimate_daily_burn()

    # Calculate TTL
    if burn > 0:
        ttl = balance / burn
    else:
        ttl = 0.0

    status = determine_status(ttl)

    # Print the canonical output line
    print(f"TTL={ttl:.1f} days | BALANCE=${balance:.2f} | BURN=${burn:.2f}/day | STATUS={status}")

    # Exit code reflects severity for use in shell scripts
    if status == "CRITICAL":
        sys.exit(2)
    elif status == "HUNGRY":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
