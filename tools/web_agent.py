"""
Intent-driven crypto market fetcher for Cursor agents.
Uses DexScreener public API (+ optional CoinGecko spot) — no API keys required for basic use.
Docs: https://docs.dexscreener.com/api/reference
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DEX_BASE = "https://api.dexscreener.com"
CG_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"

# CoinGecko id by common symbol (extend as needed)
COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "ZEN": "horizen",
    "WIF": "dogwifcoin",
    "PEPE": "pepe",
    "BNB": "binancecoin",
}

SYMBOL_ALIASES: dict[str, str] = {
    "bitcoin": "BTC",
    "以太坊": "ETH",
    "sol": "SOL",
    "solana": "SOL",
    "zen": "ZEN",
    "horizen": "ZEN",
}


def _utf8_stdio() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def http_get_json(url: str, timeout: float = 45.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "mcp-commander-web-agent/1.0 (+https://github.com)",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def extract_symbols(query: str) -> list[str]:
    """Pull likely tickers from mixed Chinese/English queries."""
    q = query.strip()
    found: list[str] = []

    for m in re.finditer(r"\b([A-Z0-9]{2,14})\b", q):
        sym = m.group(1)
        if sym in {"HTTP", "HTTPS", "API", "URL", "JSON", "USD", "USDT", "USDC"}:
            continue
        if sym not in found:
            found.append(sym)

    lower = q.lower()
    for alias, sym in SYMBOL_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lower) and sym not in found:
            found.append(sym)

    pair_match = re.search(
        r"\b([A-Za-z0-9]{2,10})\s*/\s*([A-Za-z0-9]{2,10})\b", q, re.I
    )
    if pair_match:
        a, b = pair_match.group(1).upper(), pair_match.group(2).upper()
        for sym in (a, b):
            if sym not in found:
                found.append(sym)

    return found[:8]


def dex_search(q: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"q": q})
    url = f"{DEX_BASE}/latest/dex/search?{params}"
    return http_get_json(url)


def coingecko_spot(symbols: list[str]) -> dict[str, Any] | None:
    ids: list[str] = []
    for s in symbols:
        cid = COINGECKO_IDS.get(s.upper())
        if cid and cid not in ids:
            ids.append(cid)
    if not ids:
        return None
    qs = urllib.parse.urlencode(
        {
            "ids": ",".join(ids),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        }
    )
    try:
        return http_get_json(f"{CG_SIMPLE}?{qs}", timeout=30.0)
    except urllib.error.HTTPError as e:
        return {"_error": f"CoinGecko HTTP {e.code}", "detail": e.reason}
    except Exception as e:  # noqa: BLE001
        return {"_error": "CoinGecko failed", "detail": str(e)}


def pick_search_terms(query: str, symbols: list[str]) -> str:
    if symbols:
        if len(symbols) >= 2:
            return f"{symbols[0]} {symbols[1]}"
        return symbols[0]
    compact = re.sub(r"\s+", " ", query).strip()
    return compact[:80] if compact else "solana"


def summarize_pairs(pairs: list[dict[str, Any]], limit: int = 8) -> list[str]:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Fetched at {now} (DexScreener pairs, showing up to {limit})")
    for i, p in enumerate(pairs[:limit], 1):
        base = (p.get("baseToken") or {}).get("symbol", "?")
        quote = (p.get("quoteToken") or {}).get("symbol", "?")
        chain = p.get("chainId", "?")
        dex = p.get("dexId", "?")
        price = p.get("priceUsd")
        vol = p.get("volume", {}).get("h24") if isinstance(p.get("volume"), dict) else None
        liq = p.get("liquidity", {}).get("usd") if isinstance(p.get("liquidity"), dict) else None
        chg = p.get("priceChange", {}).get("h24") if isinstance(p.get("priceChange"), dict) else None
        url = p.get("url", "")
        lines.append(
            f"{i}. {base}/{quote} | {chain} @ {dex} | priceUsd={price} | "
            f"h24_vol={vol} | liq_usd={liq} | h24_chg%={chg} | {url}"
        )
    return lines


@dataclass
class RunResult:
    query: str
    search_term: str
    symbols: list[str]
    dex_raw: dict[str, Any]
    coingecko_raw: dict[str, Any] | None
    summary_lines: list[str]


def run(query: str) -> RunResult:
    symbols = extract_symbols(query)
    term = pick_search_terms(query, symbols)
    dex_raw = dex_search(term)
    pairs = dex_raw.get("pairs") or []
    summary = summarize_pairs(pairs if isinstance(pairs, list) else [])
    cg = coingecko_spot(symbols) if symbols else None

    if cg and "_error" not in cg:
        summary.append("--- CoinGecko spot (usd, approx) ---")
        for sym in symbols[:6]:
            cid = COINGECKO_IDS.get(sym.upper())
            if not cid:
                continue
            block = cg.get(cid)
            if isinstance(block, dict):
                summary.append(
                    f"{sym}: usd={block.get('usd')} "
                    f"24h%={block.get('usd_24h_change')} "
                    f"updated={block.get('last_updated_at')}"
                )

    return RunResult(
        query=query,
        search_term=term,
        symbols=symbols,
        dex_raw=dex_raw if isinstance(dex_raw, dict) else {"pairs": []},
        coingecko_raw=cg,
        summary_lines=summary,
    )


def main() -> int:
    _utf8_stdio()
    argv = sys.argv[1:]
    if not argv:
        print(
            'Usage: py tools/web_agent.py "natural language query"\n'
            "Example: py tools/web_agent.py \"Dexscreener ZEN/SOL latest price\"",
            file=sys.stderr,
        )
        return 2

    query = " ".join(argv).strip()
    if not query:
        print("Empty query.", file=sys.stderr)
        return 2

    try:
        result = run(query)
    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} {e.reason}", file=sys.stderr)
        try:
            print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        except Exception:
            pass
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"web_agent failed: {e}", file=sys.stderr)
        return 1

    print("=== WEB_AGENT_SUMMARY ===")
    for line in result.summary_lines:
        print(line)
    print("=== WEB_AGENT_META ===")
    print(f"search_term={result.search_term!r} symbols={result.symbols}")
    print("=== WEB_AGENT_JSON ===")
    out = {
        "query": result.query,
        "search_term": result.search_term,
        "symbols": result.symbols,
        "dexscreener": result.dex_raw,
        "coingecko": result.coingecko_raw,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
