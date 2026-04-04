"""
TDD Test: Supervisor — the metabolic heart that balances survival vs research.

Tests:
1. TTL calculation from balance/BMR
2. Strategy selection based on TTL
3. Supervisor cycle logic (without real browser)
4. Balance file read/write
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_calculate_ttl_from_balance():
    """TTL = balance / daily burn rate."""
    from core.vitals import calculate_ttl

    with tempfile.TemporaryDirectory() as td:
        bf = os.path.join(td, "balance.json")
        with open(bf, "w") as f:
            json.dump({"balance": 50, "bmr": 2.0}, f)

        ttl = calculate_ttl(balance_path=bf)
        assert ttl == 25.0


def test_calculate_ttl_missing_file():
    """Missing balance file → TTL = 0.5 (near death)."""
    from core.vitals import calculate_ttl

    ttl = calculate_ttl(balance_path="/nonexistent/balance.json")
    assert ttl <= 1.0


def test_calculate_ttl_zero_bmr():
    """Zero BMR must not crash (division by zero)."""
    from core.vitals import calculate_ttl

    with tempfile.TemporaryDirectory() as td:
        bf = os.path.join(td, "balance.json")
        with open(bf, "w") as f:
            json.dump({"balance": 100, "bmr": 0}, f)

        ttl = calculate_ttl(balance_path=bf)
        assert ttl > 0  # should handle gracefully


def test_strategy_research_mode():
    """TTL > 30 → RESEARCH mode."""
    from core.supervisor import DevClawSupervisor

    sup = DevClawSupervisor.__new__(DevClawSupervisor)
    mode, _ = sup.decide_strategy(45.0)
    assert mode == "RESEARCH"


def test_strategy_balance_mode():
    """7 < TTL <= 30 → BALANCE mode."""
    from core.supervisor import DevClawSupervisor

    sup = DevClawSupervisor.__new__(DevClawSupervisor)
    mode, _ = sup.decide_strategy(15.0)
    assert mode == "BALANCE"


def test_strategy_survival_mode():
    """TTL <= 7 → SURVIVAL mode."""
    from core.supervisor import DevClawSupervisor

    sup = DevClawSupervisor.__new__(DevClawSupervisor)
    mode, _ = sup.decide_strategy(3.0)
    assert mode == "SURVIVAL"


def test_strategy_critical():
    """TTL < 1 → SURVIVAL + critical flag."""
    from core.supervisor import DevClawSupervisor

    sup = DevClawSupervisor.__new__(DevClawSupervisor)
    mode, directive = sup.decide_strategy(0.5)
    assert mode == "SURVIVAL"
    assert "危险" in directive or "续命" in directive or "survival" in directive.lower()


def test_update_balance():
    """Balance must be updatable."""
    from core.vitals import update_balance, calculate_ttl

    with tempfile.TemporaryDirectory() as td:
        bf = os.path.join(td, "balance.json")
        with open(bf, "w") as f:
            json.dump({"balance": 10, "bmr": 1.0}, f)

        update_balance(bf, delta=5.0)
        ttl = calculate_ttl(balance_path=bf)
        assert ttl == 15.0
