"""
Market Cap Monitor — recurring price/mcap watch on Solana tokens.

Checks a token's market cap via DexScreener every N seconds.
When target mcap is breached, sends TG alert to user.
Runs as a daemon until target is hit or stopped.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


def fetch_token_data(token_address: str) -> dict[str, Any] | None:
    """Fetch token data from DexScreener."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DevClaw-McapMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            pairs = data.get("pairs", [])
            if not pairs:
                return None
            p = pairs[0]
            return {
                "name": p.get("baseToken", {}).get("name", "?"),
                "symbol": p.get("baseToken", {}).get("symbol", "?"),
                "price_usd": float(p.get("priceUsd") or 0),
                "mcap": float(p.get("marketCap") or p.get("fdv") or 0),
                "volume_24h": float(p.get("volume", {}).get("h24") or 0),
                "pair": p.get("pairAddress", ""),
                "dex": p.get("dexId", ""),
                "chain": p.get("chainId", ""),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }
    except Exception as e:
        return {"error": str(e)}


def send_tg_alert(message: str) -> bool:
    """Send alert to TG admin."""
    try:
        import telebot
        token = os.environ.get("TG_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TG_ADMIN_CHAT_IDS", "").strip().split(",")[0]
        if token and chat_id:
            bot = telebot.TeleBot(token)
            bot.send_message(int(chat_id), message)
            return True
    except Exception:
        pass
    return False


def check_and_alert(
    token_address: str,
    target_mcap: float,
    state_path: str = ".auth/live_monitor.json",
) -> dict[str, Any]:
    """Check token mcap against target. Alert if breached."""
    data = fetch_token_data(token_address)
    if not data or "error" in data:
        return {"status": "error", "detail": str(data)}

    mcap = data["mcap"]
    breached = mcap >= target_mcap

    result = {
        "token": data["name"],
        "symbol": data["symbol"],
        "mcap": mcap,
        "target": target_mcap,
        "gap": target_mcap - mcap,
        "breached": breached,
        "price": data["price_usd"],
        "timestamp": data["timestamp"],
    }

    # Save to disk
    try:
        p = Path(state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    # Alert if breached
    if breached:
        msg = (
            f"🚨 MCAP ALERT!\n"
            f"Token: {data['name']} ({data['symbol']})\n"
            f"Market Cap: ${mcap:,.0f}\n"
            f"Target: ${target_mcap:,.0f}\n"
            f"Price: ${data['price_usd']}\n"
            f"Status: BREACHED! 🎯"
        )
        send_tg_alert(msg)

    return result


def run_monitor_daemon(
    token_address: str,
    target_mcap: float,
    interval_sec: int = 60,
    max_checks: int = 0,  # 0 = infinite
):
    """Run recurring monitor. Checks every interval_sec until target hit."""
    print(f"[MONITOR] Watching {token_address[:12]}... Target: ${target_mcap:,.0f}")
    checks = 0
    while True:
        checks += 1
        result = check_and_alert(token_address, target_mcap)
        mcap = result.get("mcap", 0)
        print(f"[{result.get('timestamp', '?')}] {result.get('symbol', '?')}: ${mcap:,.0f} / ${target_mcap:,.0f}", end="")

        if result.get("breached"):
            print(" 🎯 BREACHED!")
            return result
        else:
            print(f" (gap: ${result.get('gap', 0):,.0f})")

        if max_checks > 0 and checks >= max_checks:
            print(f"[MONITOR] Max checks ({max_checks}) reached. Stopping.")
            return result

        time.sleep(interval_sec)


def execute(**kwargs):
    """DevClaw skill interface."""
    token = kwargs.get("token_address", "")
    target = kwargs.get("target_mcap", 0)
    if not token or not target:
        return {"error": "Need token_address and target_mcap"}
    return check_and_alert(token, target)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

    token = sys.argv[1] if len(sys.argv) > 1 else "6iA73gWCKkLWKbVr8rgibV57MMRxzsaqS9cWpgKBpump"
    target = float(sys.argv[2]) if len(sys.argv) > 2 else 6200000

    run_monitor_daemon(token, target, interval_sec=60, max_checks=0)
