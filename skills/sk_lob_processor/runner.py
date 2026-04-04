"""
LOB Processor — Order book feature engineering for quant trading.

Converts raw L2 order book data into structured features:
- Mid-price, Weighted Average Price (WAP)
- Order Book Imbalance (OBI)
- 40-dimensional feature vector for ML input

Input: {"bids": [{"price": x, "size": y}, ...], "asks": [...]}
Output: Feature vector [p1,s1,p2,s2,...] for 10 levels, both sides
"""
from __future__ import annotations
from typing import Any


def compute_mid_price(lob: dict[str, list]) -> float:
    """Mid-price = (best_bid + best_ask) / 2"""
    bids = lob.get("bids", [])
    asks = lob.get("asks", [])
    if not bids or not asks:
        return 0.0
    return (bids[0]["price"] + asks[0]["price"]) / 2


def compute_wap(lob: dict[str, list]) -> float:
    """Weighted Average Price using top-of-book sizes as weights.
    WAP = (bid_price * ask_size + ask_price * bid_size) / (bid_size + ask_size)
    """
    bids = lob.get("bids", [])
    asks = lob.get("asks", [])
    if not bids or not asks:
        return 0.0
    bp, bs = bids[0]["price"], bids[0]["size"]
    ap, as_ = asks[0]["price"], asks[0]["size"]
    total = bs + as_
    if total == 0:
        return (bp + ap) / 2
    return (bp * as_ + ap * bs) / total


def compute_imbalance(lob: dict[str, list], levels: int = 1) -> float:
    """Order Book Imbalance at top N levels.
    OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    Range: [-1, 1]. Positive = buy pressure.
    """
    bids = lob.get("bids", [])[:levels]
    asks = lob.get("asks", [])[:levels]
    bid_vol = sum(b["size"] for b in bids)
    ask_vol = sum(a["size"] for a in asks)
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def lob_to_features(lob: dict[str, list], depth: int = 10) -> list[float]:
    """Convert LOB to flat feature vector.

    Output: [bid_p1, bid_s1, bid_p2, bid_s2, ..., ask_p1, ask_s1, ...]
    Total: depth * 2 (price+size) * 2 (sides) = 40 features for depth=10
    """
    mid = compute_mid_price(lob)
    if mid == 0:
        mid = 1.0  # avoid division by zero

    features = []

    # Bid side: normalized prices and sizes
    bids = lob.get("bids", [])
    for i in range(depth):
        if i < len(bids):
            features.append((bids[i]["price"] - mid) / mid)  # normalized price delta
            features.append(bids[i]["size"] / 1000.0)  # normalized size
        else:
            features.append(0.0)
            features.append(0.0)

    # Ask side
    asks = lob.get("asks", [])
    for i in range(depth):
        if i < len(asks):
            features.append((asks[i]["price"] - mid) / mid)
            features.append(asks[i]["size"] / 1000.0)
        else:
            features.append(0.0)
            features.append(0.0)

    return features


def execute(**kwargs):
    """DevClaw skill interface."""
    lob = kwargs.get("lob", {"bids": [], "asks": []})
    return {
        "mid_price": compute_mid_price(lob),
        "wap": compute_wap(lob),
        "imbalance": compute_imbalance(lob),
        "features": lob_to_features(lob),
        "feature_dim": len(lob_to_features(lob)),
    }
