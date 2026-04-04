"""Market Scout — fetch SOL/USDC real-time price via Jupiter & DexScreener."""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

MARKET_VITALS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", ".auth", "market_vitals.json",
)

# Public API endpoints (no auth required)
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/solana/8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"

HTTP_TIMEOUT = 15


def _http_get_json(url, *, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "market-scout/1.0 (public-scan)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def fetch_jupiter_price():
    """Fetch SOL/USDC price from Jupiter Price API v2."""
    body = _http_get_json(JUPITER_PRICE_URL)
    if not isinstance(body, dict):
        return None
    data = body.get("data", {})
    sol_data = data.get("So11111111111111111111111111111111111111112", {})
    price = sol_data.get("price")
    if price is None:
        return None
    return float(price)


def fetch_dexscreener_price():
    """Fetch SOL/USDC price from DexScreener (Raydium SOL-USDC pair)."""
    body = _http_get_json(DEXSCREENER_PAIR_URL)
    if not isinstance(body, dict):
        return None
    pair = body.get("pair") or (body.get("pairs") or [None])[0]
    if not isinstance(pair, dict):
        return None
    price = pair.get("priceUsd")
    if price is None:
        return None
    return float(price)


def run():
    vitals_path = os.path.normpath(MARKET_VITALS_PATH)
    os.makedirs(os.path.dirname(vitals_path), exist_ok=True)

    jupiter_price = fetch_jupiter_price()
    dexscreener_price = fetch_dexscreener_price()

    # Use Jupiter as primary, DexScreener as fallback
    price = jupiter_price or dexscreener_price

    result = {
        "pair": "SOL/USDC",
        "price_usd": price,
        "sources": {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if jupiter_price is not None:
        result["sources"]["jupiter"] = jupiter_price
    if dexscreener_price is not None:
        result["sources"]["dexscreener"] = dexscreener_price

    # Merge into existing market_vitals.json if present
    data = {}
    if os.path.exists(vitals_path):
        with open(vitals_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}

    data["sol_usdc"] = result

    with open(vitals_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    status = f"${price:.2f}" if price else "FAILED"
    sources = ", ".join(result["sources"].keys()) or "none"
    print(f"[market_scout] SOL/USDC={status}  sources=[{sources}]  -> {vitals_path}")
    return data


if __name__ == "__main__":
    run()
