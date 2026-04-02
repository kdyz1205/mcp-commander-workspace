#!/usr/bin/env python3
"""Public Binance futures funding rates (no API key). For trading_funding_public skill."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    sym = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT").upper()
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    out = {
        "ok": True,
        "symbol": data.get("symbol"),
        "markPrice": data.get("markPrice"),
        "indexPrice": data.get("indexPrice"),
        "lastFundingRate": data.get("lastFundingRate"),
        "nextFundingTime": data.get("nextFundingTime"),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
