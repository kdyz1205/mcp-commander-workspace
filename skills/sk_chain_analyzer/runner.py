"""
Chain Analyzer — analyze Solana wallet transaction patterns.

Determines if a wallet is a market maker, whale, smart money, or retail trader
by analyzing transaction frequency, volume, token diversity, and timing patterns.
Uses DexScreener + Helius/Solana RPC.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any


def _helius_get_transactions(wallet: str, limit: int = 50) -> list[dict] | None:
    """Fetch parsed transactions from Helius API."""
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        return None
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={key}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "DevClaw-ChainAnalyzer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _get_rpc_signatures(wallet: str, limit: int = 50) -> list[dict] | None:
    """Get transaction signatures via Solana RPC."""
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [wallet, {"limit": limit}],
    })
    req = urllib.request.Request(
        rpc_url, data=payload.encode(),
        headers={"Content-Type": "application/json", "User-Agent": "DevClaw-ChainAnalyzer/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("result", [])
    except Exception:
        return None


def _get_token_accounts(wallet: str) -> list[dict]:
    """Get all token accounts for a wallet."""
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ],
    })
    req = urllib.request.Request(
        rpc_url, data=payload.encode(),
        headers={"Content-Type": "application/json", "User-Agent": "DevClaw-ChainAnalyzer/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("result", {}).get("value", [])
    except Exception:
        return []


def _analyze_helius_transactions(txs: list[dict]) -> dict[str, Any]:
    """Analyze Helius-enhanced transactions for patterns."""
    stats = {
        "total_txs": len(txs),
        "swaps": 0,
        "transfers": 0,
        "other": 0,
        "tokens_traded": set(),
        "dexes_used": set(),
        "total_volume_sol": 0.0,
        "avg_interval_sec": 0,
        "timestamps": [],
    }

    for tx in txs:
        tx_type = tx.get("type", "UNKNOWN")
        if tx_type == "SWAP":
            stats["swaps"] += 1
        elif tx_type == "TRANSFER":
            stats["transfers"] += 1
        else:
            stats["other"] += 1

        # Track tokens
        for transfer in tx.get("tokenTransfers", []):
            symbol = transfer.get("tokenSymbol") or transfer.get("mint", "")[:8]
            if symbol:
                stats["tokens_traded"].add(symbol)

        # Track DEX
        source = tx.get("source", "")
        if source:
            stats["dexes_used"].add(source)

        # Track native SOL volume
        for nt in tx.get("nativeTransfers", []):
            stats["total_volume_sol"] += abs(float(nt.get("amount", 0))) / 1e9

        # Track timing
        ts = tx.get("timestamp", 0)
        if ts:
            stats["timestamps"].append(ts)

    # Calculate timing pattern
    if len(stats["timestamps"]) >= 2:
        sorted_ts = sorted(stats["timestamps"])
        intervals = [sorted_ts[i+1] - sorted_ts[i] for i in range(len(sorted_ts)-1)]
        stats["avg_interval_sec"] = sum(intervals) / len(intervals) if intervals else 0
        stats["min_interval_sec"] = min(intervals) if intervals else 0
        stats["max_interval_sec"] = max(intervals) if intervals else 0
        stats["time_span_hours"] = (sorted_ts[-1] - sorted_ts[0]) / 3600

    stats["tokens_traded"] = list(stats["tokens_traded"])
    stats["dexes_used"] = list(stats["dexes_used"])
    return stats


def _classify_wallet(stats: dict, token_accounts: list) -> dict[str, Any]:
    """Classify wallet type based on transaction patterns."""
    classification = {
        "type": "unknown",
        "confidence": 0.0,
        "reasons": [],
    }

    swap_ratio = stats["swaps"] / max(stats["total_txs"], 1)
    tokens_count = len(stats["tokens_traded"])
    avg_interval = stats.get("avg_interval_sec", 0)
    min_interval = stats.get("min_interval_sec", float("inf"))

    # Market Maker detection
    mm_score = 0
    if swap_ratio > 0.7:
        mm_score += 2
        classification["reasons"].append(f"高比例swap交易 ({swap_ratio:.0%})")
    if avg_interval > 0 and avg_interval < 300:  # < 5 min average
        mm_score += 2
        classification["reasons"].append(f"高频交易 (平均间隔 {avg_interval:.0f}秒)")
    if min_interval < 60:
        mm_score += 1
        classification["reasons"].append(f"最小间隔 {min_interval:.0f}秒 — 自动化特征")
    if tokens_count <= 5 and stats["swaps"] > 20:
        mm_score += 2
        classification["reasons"].append(f"集中交易少量代币 ({tokens_count}个代币, {stats['swaps']}笔swap)")
    if len(stats.get("dexes_used", [])) <= 2 and stats["swaps"] > 10:
        mm_score += 1
        classification["reasons"].append(f"固定DEX ({', '.join(stats.get('dexes_used', []))})")

    # Whale detection
    whale_score = 0
    if stats["total_volume_sol"] > 100:
        whale_score += 2
        classification["reasons"].append(f"大额交易 ({stats['total_volume_sol']:.1f} SOL)")
    if len(token_accounts) > 50:
        whale_score += 1
        classification["reasons"].append(f"持有大量代币 ({len(token_accounts)}种)")

    # Smart money detection
    smart_score = 0
    if tokens_count > 10 and swap_ratio > 0.5:
        smart_score += 1
        classification["reasons"].append(f"广泛扫描代币 ({tokens_count}种)")

    # Classify
    if mm_score >= 4:
        classification["type"] = "做市商 (Market Maker)"
        classification["confidence"] = min(mm_score / 6, 1.0)
    elif whale_score >= 2:
        classification["type"] = "巨鲸 (Whale)"
        classification["confidence"] = min(whale_score / 3, 1.0)
    elif smart_score >= 1 and whale_score >= 1:
        classification["type"] = "聪明钱 (Smart Money)"
        classification["confidence"] = 0.6
    elif stats["total_txs"] < 20 and swap_ratio < 0.3:
        classification["type"] = "散户 (Retail)"
        classification["confidence"] = 0.5
        classification["reasons"].append("交易频率低，以转账为主")
    else:
        classification["type"] = "活跃交易者 (Active Trader)"
        classification["confidence"] = 0.4
        classification["reasons"].append("交易模式不明显属于任何类别")

    return classification


def analyze_wallet(wallet_address: str, question: str = "") -> str:
    """
    Full wallet analysis — returns formatted analysis text.

    Args:
        wallet_address: Solana wallet address
        question: Optional user question for context
    """
    lines = [f"📊 钱包分析: {wallet_address[:8]}...{wallet_address[-6:]}"]
    lines.append("=" * 40)

    # Fetch transactions
    txs = _helius_get_transactions(wallet_address, limit=50)
    used_helius = txs is not None

    if not txs:
        # Fallback to basic RPC
        sigs = _get_rpc_signatures(wallet_address, limit=50)
        if sigs:
            lines.append(f"\n📈 最近交易: {len(sigs)} 笔")
            if sigs:
                # Show time range
                first_ts = sigs[-1].get("blockTime", 0)
                last_ts = sigs[0].get("blockTime", 0)
                if first_ts and last_ts:
                    span_hours = (last_ts - first_ts) / 3600
                    lines.append(f"时间跨度: {span_hours:.1f} 小时")
                    avg_interval = (last_ts - first_ts) / max(len(sigs) - 1, 1)
                    lines.append(f"平均间隔: {avg_interval:.0f} 秒")
        else:
            lines.append("\n⚠️ 无法获取交易数据。请检查地址是否正确。")

    # Get token accounts
    token_accounts = _get_token_accounts(wallet_address)
    lines.append(f"\n💰 持有代币: {len(token_accounts)} 种")

    # Show top holdings
    holdings = []
    for acct in token_accounts:
        info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        amount = float(info.get("tokenAmount", {}).get("uiAmount") or 0)
        mint = info.get("mint", "")
        if amount > 0:
            holdings.append((mint[:8], amount))
    holdings.sort(key=lambda x: -x[1])
    for mint, amt in holdings[:5]:
        lines.append(f"  • {mint}... : {amt:,.2f}")

    # Analyze with Helius data if available
    if txs:
        stats = _analyze_helius_transactions(txs)
        lines.append(f"\n📈 交易统计 (最近{stats['total_txs']}笔):")
        lines.append(f"  • Swap: {stats['swaps']} | Transfer: {stats['transfers']} | Other: {stats['other']}")
        lines.append(f"  • 涉及代币: {', '.join(stats['tokens_traded'][:10])}")
        lines.append(f"  • 使用DEX: {', '.join(stats['dexes_used'][:5])}")
        lines.append(f"  • SOL交易量: {stats['total_volume_sol']:.2f} SOL")
        if stats.get("avg_interval_sec"):
            lines.append(f"  • 平均交易间隔: {stats['avg_interval_sec']:.0f}秒")
        if stats.get("time_span_hours"):
            lines.append(f"  • 时间跨度: {stats['time_span_hours']:.1f}小时")

        # Classification
        classification = _classify_wallet(stats, token_accounts)
        lines.append(f"\n🏷 分类判断: {classification['type']}")
        lines.append(f"置信度: {classification['confidence']:.0%}")
        if classification["reasons"]:
            lines.append("依据:")
            for reason in classification["reasons"]:
                lines.append(f"  • {reason}")
    elif not used_helius:
        # Basic RPC analysis — limited
        if token_accounts:
            if len(token_accounts) > 30:
                lines.append(f"\n🏷 初步判断: 可能是活跃交易者/做市商 (持有{len(token_accounts)}种代币)")
            elif len(token_accounts) > 10:
                lines.append(f"\n🏷 初步判断: 活跃交易者 (持有{len(token_accounts)}种代币)")
            else:
                lines.append(f"\n🏷 初步判断: 普通用户 (持有{len(token_accounts)}种代币)")
        lines.append("\n💡 提示: 设置 HELIUS_API_KEY 可获得详细交易分析")

    return "\n".join(lines)


def execute(**kwargs):
    """DevClaw skill interface."""
    wallet = kwargs.get("wallet_address", "")
    question = kwargs.get("question", "")
    if not wallet:
        return {"error": "Need wallet_address"}
    return analyze_wallet(wallet, question)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

    wallet = sys.argv[1] if len(sys.argv) > 1 else ""
    if not wallet:
        print("Usage: python runner.py <wallet_address>")
        sys.exit(1)

    print(analyze_wallet(wallet))
