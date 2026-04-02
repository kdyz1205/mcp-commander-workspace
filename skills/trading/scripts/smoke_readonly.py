"""
Read-only smoke test for vendored trading + trading_skills (no API keys, no network).
Run from workspace root:

  py skills/trading/scripts/smoke_readonly.py
"""

from __future__ import annotations

import os
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def main() -> int:
    from trading.indicators import sma

    s = sma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert len(s) == 5, s
    print("trading.indicators.sma:", s[-1])

    import numpy as np
    import polars as pl

    from trading_skills import MarketRegimeDetector

    n = 120
    t = np.arange(n, dtype=np.int64) * 60_000
    close = 100 + np.cumsum(np.random.default_rng(0).standard_normal(n) * 0.5)
    high = close + np.abs(np.random.default_rng(1).standard_normal(n) * 0.3)
    low = close - np.abs(np.random.default_rng(2).standard_normal(n) * 0.3)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = np.full(n, 1e6)

    df = pl.DataFrame(
        {
            "open_time": t,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        }
    )
    det = MarketRegimeDetector()
    out = det.detect(df)
    print("MarketRegimeDetector.detect keys:", sorted(out.keys())[:8], "...")
    print("regime:", out.get("regime"))
    print("smoke_readonly: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
