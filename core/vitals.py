"""
Vitals — DevClaw's life signs monitor.

Calculates TTL (Time To Live) from balance and burn rate.
This is the physical health bar.
"""
from __future__ import annotations

import json
from pathlib import Path


def calculate_ttl(balance_path: str = ".auth/balance.json") -> float:
    """
    Calculate remaining days of life.

    TTL = balance / daily_burn_rate (BMR).
    Returns 0.5 (near death) if file missing or corrupt.
    """
    try:
        data = json.loads(Path(balance_path).read_text(encoding="utf-8"))
        balance = float(data.get("balance", 0))
        if balance <= 0:
            return 0.0  # Dead — no balance left
        bmr = float(data.get("bmr", 1.0))
        if bmr <= 0:
            bmr = 0.1  # prevent division by zero
        return balance / bmr
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.5  # assume near death


def update_balance(balance_path: str, delta: float) -> float:
    """
    Add or subtract from balance. Returns new balance.
    """
    p = Path(balance_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {"balance": 0, "bmr": 1.0}

    data["balance"] = float(data.get("balance", 0)) + delta
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data["balance"]


def get_vitals_summary(balance_path: str = ".auth/balance.json") -> str:
    """Human-readable vitals string."""
    try:
        data = json.loads(Path(balance_path).read_text(encoding="utf-8"))
        balance = float(data.get("balance", 0))
        bmr = float(data.get("bmr", 1.0))
        ttl = balance / max(bmr, 0.1)
        status = "HEALTHY" if ttl > 14 else "HUNGRY" if ttl > 3 else "CRITICAL"
        return f"TTL={ttl:.1f} days | BALANCE=${balance:.2f} | BURN=${bmr:.2f}/day | STATUS={status}"
    except Exception:
        return "TTL=0.5 days | BALANCE=$0.00 | BURN=$1.00/day | STATUS=CRITICAL"
