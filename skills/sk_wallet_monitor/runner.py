"""
Wallet Transaction Monitor — watch a Solana wallet for specific token purchases.

Polls the wallet's recent transactions via Helius/Solana RPC and DexScreener.
When a buy of the target token exceeding the USD threshold is detected,
fires a TG alert.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Solana RPC helpers
# ---------------------------------------------------------------------------

def _get_rpc_url() -> str:
    """Return Solana RPC URL (prefer Helius if key available)."""
    helius_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if helius_key:
        return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
    return os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def _rpc_call(method: str, params: list, timeout: int = 15) -> dict | None:
    """Make a JSON-RPC call to Solana."""
    url = _get_rpc_url()
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        url,
        data=payload.encode(),
        headers={"Content-Type": "application/json", "User-Agent": "DevClaw-WalletMonitor/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[WALLET-MON] RPC error: {e}")
        return None


def _helius_parse_transactions(wallet: str, limit: int = 20) -> list[dict] | None:
    """Use Helius enhanced transaction API if available."""
    helius_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not helius_key:
        return None
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={helius_key}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "DevClaw-WalletMonitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[WALLET-MON] Helius error: {e}")
        return None


# ---------------------------------------------------------------------------
# DexScreener helpers
# ---------------------------------------------------------------------------

def _fetch_token_info(token_address: str) -> dict[str, Any] | None:
    """Get token name/symbol/price from DexScreener."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    req = urllib.request.Request(url, headers={"User-Agent": "DevClaw-WalletMonitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            pairs = data.get("pairs", [])
            if not pairs:
                return None
            p = pairs[0]
            return {
                "name": p.get("baseToken", {}).get("name", "?"),
                "symbol": p.get("baseToken", {}).get("symbol", "?"),
                "address": p.get("baseToken", {}).get("address", token_address),
                "price_usd": float(p.get("priceUsd") or 0),
            }
    except Exception:
        return None


def _search_token_by_name(name: str) -> dict[str, Any] | None:
    """Search DexScreener for a token by name/symbol."""
    url = f"https://api.dexscreener.com/latest/dex/search?q={urllib.request.quote(name)}"
    req = urllib.request.Request(url, headers={"User-Agent": "DevClaw-WalletMonitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            pairs = data.get("pairs", [])
            # Prefer Solana pairs
            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            chosen = sol_pairs[0] if sol_pairs else (pairs[0] if pairs else None)
            if not chosen:
                return None
            return {
                "name": chosen.get("baseToken", {}).get("name", "?"),
                "symbol": chosen.get("baseToken", {}).get("symbol", "?"),
                "address": chosen.get("baseToken", {}).get("address", ""),
                "price_usd": float(chosen.get("priceUsd") or 0),
            }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Transaction analysis
# ---------------------------------------------------------------------------

def _get_recent_signatures(wallet: str, limit: int = 20) -> list[str]:
    """Get recent transaction signatures for a wallet."""
    result = _rpc_call("getSignaturesForAddress", [wallet, {"limit": limit}])
    if not result or "result" not in result:
        return []
    return [tx["signature"] for tx in result["result"]]


def _parse_swap_from_helius(tx: dict, target_token_symbol: str, target_token_addr: str | None) -> dict | None:
    """Parse a Helius-enhanced transaction for swap info."""
    tx_type = tx.get("type", "")
    if tx_type not in ("SWAP", "TRANSFER"):
        return None

    # Check token transfers
    token_transfers = tx.get("tokenTransfers", [])
    native_transfers = tx.get("nativeTransfers", [])
    description = tx.get("description", "").lower()

    # Look for the target token in transfers
    target_sym_lower = target_token_symbol.lower()
    for transfer in token_transfers:
        mint = transfer.get("mint", "")
        token_name = (transfer.get("tokenName") or "").lower()
        token_symbol = (transfer.get("tokenSymbol") or "").lower()

        # Match by address or name/symbol
        is_target = False
        if target_token_addr and mint == target_token_addr:
            is_target = True
        elif target_sym_lower in token_symbol or target_sym_lower in token_name:
            is_target = True

        if is_target:
            amount = float(transfer.get("tokenAmount") or 0)
            if amount <= 0:
                continue
            return {
                "signature": tx.get("signature", ""),
                "type": tx_type,
                "token_symbol": transfer.get("tokenSymbol", target_token_symbol),
                "token_mint": mint,
                "amount": amount,
                "timestamp": tx.get("timestamp", 0),
            }
    return None


def analyze_wallet_transactions(
    wallet: str,
    target_token_symbol: str,
    target_token_addr: str | None = None,
    min_usd: float = 0,
    since_timestamp: int = 0,
) -> list[dict]:
    """Analyze recent wallet transactions for target token buys.

    Returns list of matching swaps with estimated USD value.
    """
    results = []

    # Try Helius first (enhanced parsed data)
    helius_txs = _helius_parse_transactions(wallet, limit=30)
    if helius_txs:
        for tx in helius_txs:
            ts = tx.get("timestamp", 0)
            if since_timestamp and ts <= since_timestamp:
                continue
            swap = _parse_swap_from_helius(tx, target_token_symbol, target_token_addr)
            if swap:
                results.append(swap)
        # Estimate USD values
        token_info = _fetch_token_info(target_token_addr) if target_token_addr else _search_token_by_name(target_token_symbol)
        price = token_info["price_usd"] if token_info else 0
        for r in results:
            r["estimated_usd"] = r["amount"] * price if price else 0
        # Filter by min USD
        if min_usd > 0:
            results = [r for r in results if r["estimated_usd"] >= min_usd]
        return results

    # Fallback: basic RPC (less detailed, but works without Helius)
    # With basic RPC we can only detect token balance changes, not parse swaps
    # This is a simplified version
    sigs = _get_recent_signatures(wallet, limit=20)
    if not sigs:
        return []

    # For basic RPC, we just check if there's activity and report it
    # Full swap parsing requires getParsedTransaction which is expensive
    # For now, return empty and let the daemon poll more frequently
    return results


# ---------------------------------------------------------------------------
# Monitor daemon
# ---------------------------------------------------------------------------

def send_tg_alert(message: str) -> bool:
    """Send alert to TG admin."""
    try:
        import telebot
        token = os.environ.get("TG_BOT_TOKEN", "").strip()
        raw_ids = os.environ.get("TG_ADMIN_CHAT_IDS", "").strip()
        chat_id = raw_ids.split(",")[0].strip() if raw_ids else ""
        if token and chat_id:
            bot = telebot.TeleBot(token)
            bot.send_message(int(chat_id), message)
            return True
    except Exception:
        pass
    return False


def run_wallet_monitor(
    wallet_address: str,
    target_token_symbol: str,
    target_token_addr: str | None = None,
    min_usd: float = 10000,
    interval_sec: int = 30,
    on_alert: Callable[[str], None] | None = None,
    stop_event: Any = None,
) -> None:
    """Run recurring wallet monitor. Checks every interval_sec.

    Args:
        wallet_address: Solana wallet to watch.
        target_token_symbol: Token name/symbol to filter (e.g. "PIXEL").
        target_token_addr: Optional token mint address for exact matching.
        min_usd: Minimum USD value to trigger alert.
        interval_sec: Polling interval.
        on_alert: Callback for sending alerts. Falls back to send_tg_alert.
        stop_event: threading.Event to signal shutdown.
    """
    print(f"[WALLET-MON] Watching {wallet_address[:12]}... "
          f"Token: {target_token_symbol} | Min: ${min_usd:,.0f} | Interval: {interval_sec}s")

    # Resolve token address if not provided
    if not target_token_addr:
        info = _search_token_by_name(target_token_symbol)
        if info and info.get("address"):
            target_token_addr = info["address"]
            print(f"[WALLET-MON] Resolved {target_token_symbol} → {target_token_addr[:12]}...")

    last_seen_timestamp = int(time.time())
    alert_fn = on_alert or send_tg_alert

    # Notify that monitoring started
    token_info = _fetch_token_info(target_token_addr) if target_token_addr else _search_token_by_name(target_token_symbol)
    token_display = f"{token_info['name']} ({token_info['symbol']})" if token_info else target_token_symbol

    while True:
        if stop_event and stop_event.is_set():
            print(f"[WALLET-MON] Shutdown signal. Stopping.")
            return

        try:
            swaps = analyze_wallet_transactions(
                wallet_address,
                target_token_symbol,
                target_token_addr,
                min_usd=min_usd,
                since_timestamp=last_seen_timestamp,
            )

            for swap in swaps:
                ts = swap.get("timestamp", 0)
                if ts > last_seen_timestamp:
                    last_seen_timestamp = ts

                msg = (
                    f"🚨 钱包买入提醒!\n"
                    f"钱包: {wallet_address[:8]}...{wallet_address[-6:]}\n"
                    f"代币: {swap.get('token_symbol', target_token_symbol)}\n"
                    f"数量: {swap.get('amount', 0):,.2f}\n"
                    f"估值: ${swap.get('estimated_usd', 0):,.0f}\n"
                    f"Tx: {swap.get('signature', '?')[:16]}...\n"
                    f"时间: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts))}"
                )
                try:
                    alert_fn(msg)
                except Exception:
                    send_tg_alert(msg)
                print(f"[WALLET-MON] Alert sent: {swap.get('token_symbol')} ${swap.get('estimated_usd', 0):,.0f}")

        except Exception as e:
            print(f"[WALLET-MON] Error: {e}")

        # Interruptible sleep
        if stop_event:
            stop_event.wait(timeout=interval_sec)
            if stop_event.is_set():
                return
        else:
            time.sleep(interval_sec)


def check_wallet_once(
    wallet_address: str,
    target_token_symbol: str,
    target_token_addr: str | None = None,
) -> dict[str, Any]:
    """One-shot check of wallet for target token activity."""
    if not target_token_addr:
        info = _search_token_by_name(target_token_symbol)
        if info:
            target_token_addr = info.get("address")

    token_info = _fetch_token_info(target_token_addr) if target_token_addr else _search_token_by_name(target_token_symbol)

    swaps = analyze_wallet_transactions(
        wallet_address,
        target_token_symbol,
        target_token_addr,
        min_usd=0,
        since_timestamp=0,
    )

    return {
        "wallet": wallet_address,
        "target_token": token_info or {"symbol": target_token_symbol},
        "recent_swaps": swaps[:10],
        "total_found": len(swaps),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


def execute(**kwargs):
    """DevClaw skill interface."""
    wallet = kwargs.get("wallet_address", "")
    token_symbol = kwargs.get("target_token_symbol", "")
    token_addr = kwargs.get("target_token_address")
    if not wallet or not token_symbol:
        return {"error": "Need wallet_address and target_token_symbol"}
    return check_wallet_once(wallet, token_symbol, token_addr)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

    wallet = sys.argv[1] if len(sys.argv) > 1 else ""
    token = sys.argv[2] if len(sys.argv) > 2 else "PIXEL"
    min_usd = float(sys.argv[3]) if len(sys.argv) > 3 else 10000

    if not wallet:
        print("Usage: python runner.py <wallet_address> [token_symbol] [min_usd]")
        sys.exit(1)

    run_wallet_monitor(wallet, token, min_usd=min_usd, interval_sec=30)
