"""
TDD Test: LOB Processor — order book feature engineering for quant trading.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Sample 10-level L2 order book
SAMPLE_LOB = {
    "bids": [
        {"price": 80.00, "size": 100}, {"price": 79.99, "size": 200},
        {"price": 79.98, "size": 150}, {"price": 79.97, "size": 300},
        {"price": 79.96, "size": 250}, {"price": 79.95, "size": 180},
        {"price": 79.94, "size": 120}, {"price": 79.93, "size": 90},
        {"price": 79.92, "size": 60}, {"price": 79.91, "size": 40},
    ],
    "asks": [
        {"price": 80.01, "size": 80}, {"price": 80.02, "size": 150},
        {"price": 80.03, "size": 200}, {"price": 80.04, "size": 350},
        {"price": 80.05, "size": 100}, {"price": 80.06, "size": 220},
        {"price": 80.07, "size": 170}, {"price": 80.08, "size": 130},
        {"price": 80.09, "size": 95}, {"price": 80.10, "size": 55},
    ],
}


def test_mid_price():
    from skills.sk_lob_processor.runner import compute_mid_price
    mid = compute_mid_price(SAMPLE_LOB)
    assert abs(mid - 80.005) < 0.0001


def test_weighted_mid_price():
    from skills.sk_lob_processor.runner import compute_wap
    wap = compute_wap(SAMPLE_LOB)
    # WAP = (best_bid*ask_size + best_ask*bid_size) / (bid_size+ask_size)
    # = (80.00*80 + 80.01*100) / (100+80) = (6400+6400.8) / 180 = 71.115...
    assert 79.9 < wap < 80.1  # reasonable range


def test_order_book_imbalance():
    from skills.sk_lob_processor.runner import compute_imbalance
    imb = compute_imbalance(SAMPLE_LOB)
    # bid_size(100) > ask_size(80) at level 1, so imbalance > 0 (buy pressure)
    assert -1 <= imb <= 1
    assert imb > 0  # more bid volume at top level


def test_imbalance_extreme_buy():
    """When bids massively outweigh asks, imbalance → 1."""
    from skills.sk_lob_processor.runner import compute_imbalance
    lob = {
        "bids": [{"price": 80, "size": 10000}],
        "asks": [{"price": 81, "size": 1}],
    }
    imb = compute_imbalance(lob)
    assert imb > 0.9


def test_feature_tensor_shape():
    """Feature tensor must be correct shape."""
    from skills.sk_lob_processor.runner import lob_to_features
    features = lob_to_features(SAMPLE_LOB)
    assert len(features) == 40  # 10 levels * 2 sides * 2 (price+size)


def test_feature_tensor_values():
    """Feature values must be real numbers, not NaN."""
    from skills.sk_lob_processor.runner import lob_to_features
    import math
    features = lob_to_features(SAMPLE_LOB)
    assert all(not math.isnan(f) for f in features)
    assert all(not math.isinf(f) for f in features)
