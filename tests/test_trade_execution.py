"""
Pain-Driven TDD for Trade Execution Engine.

These tests simulate hostile market conditions:
- Insufficient balance → must reject
- Slippage too high → must reject (MEV protection)
- Circuit breaker → must halt after consecutive losses
- Virtual wallet ACID → lock/commit/rollback integrity
- Dynamic slippage → non-linear with order size
"""
import json
import math
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.virtual_wallet import VirtualWallet, InsufficientBalanceError
from skills.sk_trade_executor.executor import (
    TradeExecutor,
    TradingMode,
    SlippageTooHighError,
    CircuitBreakerError,
)
from core.metabolic_kernel import calculate_psi


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace with initial balance."""
    (tmp_path / ".auth").mkdir()
    (tmp_path / ".auth" / "balance.json").write_text(
        '{"balance": 50, "bmr": 1.0}', encoding="utf-8"
    )
    (tmp_path / ".auth" / "paper_wallet.json").write_text(
        json.dumps({"balances": {"USDC": 10000.0, "SOL": 0.0}, "total_pnl": 0, "trade_count": 0, "trades": []}),
        encoding="utf-8",
    )
    (tmp_path / ".claw").mkdir()
    return tmp_path


# ══════════════════════════════════════════════════
# TEST GROUP A: Virtual Wallet ACID
# ══════════════════════════════════════════════════

class TestVirtualWallet:
    def test_initial_balances(self, workspace):
        wallet = VirtualWallet(workspace)
        assert wallet.get_balance("USDC") == 10000.0
        assert wallet.get_balance("SOL") == 0.0

    def test_lock_and_commit(self, workspace):
        wallet = VirtualWallet(workspace)
        tx = wallet.lock_funds("USDC", 1000, tx_id="test-001")

        # After lock: USDC should be deducted
        assert wallet.get_balance("USDC") == 9000.0

        # Commit: credit SOL
        balances = wallet.commit_trade(tx, credit_token="SOL", credit_amount=6.5)
        assert balances["USDC"] == 9000.0
        assert balances["SOL"] == 6.5

    def test_lock_and_rollback(self, workspace):
        wallet = VirtualWallet(workspace)
        tx = wallet.lock_funds("USDC", 5000, tx_id="test-002")

        # After lock: deducted
        assert wallet.get_balance("USDC") == 5000.0

        # Rollback: funds restored
        balances = wallet.rollback(tx)
        assert balances["USDC"] == 10000.0

    def test_insufficient_balance_raises(self, workspace):
        """Case A: $15,000 buy with $10,000 balance → MUST reject."""
        wallet = VirtualWallet(workspace)
        with pytest.raises(InsufficientBalanceError):
            wallet.lock_funds("USDC", 15000)

    def test_concurrent_access_no_overspend(self, workspace):
        """Concurrent threads cannot overspend."""
        wallet = VirtualWallet(workspace)
        results = []
        errors = []

        def try_lock():
            try:
                tx = wallet.lock_funds("USDC", 6000)
                results.append(tx)
            except InsufficientBalanceError:
                errors.append("insufficient")

        t1 = threading.Thread(target=try_lock)
        t2 = threading.Thread(target=try_lock)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Only one should succeed (10000 < 6000 * 2)
        assert len(results) == 1
        assert len(errors) == 1


# ══════════════════════════════════════════════════
# TEST GROUP B: Slippage & MEV Protection
# ══════════════════════════════════════════════════

class TestSlippageModel:
    def test_slippage_increases_with_order_size(self, workspace):
        """Larger orders must have higher slippage (non-linear)."""
        executor = TradeExecutor(workspace, mode=TradingMode.SIMULATION)
        liquidity = 500_000  # $500K pool

        slip_100 = executor._estimate_slippage(100, liquidity)
        slip_1000 = executor._estimate_slippage(1000, liquidity)
        slip_10000 = executor._estimate_slippage(10000, liquidity)

        assert slip_100 < slip_1000 < slip_10000
        # Non-linear: 10x order should NOT cause 10x slippage
        ratio = slip_10000 / slip_1000
        assert ratio < 10, f"Slippage should be sub-linear, got {ratio:.1f}x"

    def test_slippage_too_high_raises(self, workspace):
        """Case B: Low liquidity pool → slippage exceeds limit → MUST reject."""
        executor = TradeExecutor(workspace, mode=TradingMode.SIMULATION)

        # Mock price with tiny liquidity pool
        executor._fetch_price = lambda *a, **kw: {
            "price": 0.001, "liquidity": 1000, "volume_24h": 500,
            "pair": "test", "dex": "test",
        }

        # $5000 into $1000 liquidity → massive slippage → MUST reject
        with pytest.raises(SlippageTooHighError):
            executor.execute_swap(
                "USDC", "BONK", 5000,
                max_slippage=0.01,  # 1% max
            )

    def test_base_fee_always_present(self, workspace):
        """Even tiny orders have base fee."""
        executor = TradeExecutor(workspace, mode=TradingMode.SIMULATION)
        slip = executor._estimate_slippage(1, 1_000_000)
        assert slip >= executor.BASE_FEE

    def test_volatility_penalty(self, workspace):
        """High volatility increases slippage."""
        executor = TradeExecutor(workspace, mode=TradingMode.SIMULATION)
        liquidity = 500_000

        slip_calm = executor._estimate_slippage(1000, liquidity, volatility=0.0)
        slip_volatile = executor._estimate_slippage(1000, liquidity, volatility=0.5)

        assert slip_volatile > slip_calm


# ══════════════════════════════════════════════════
# TEST GROUP C: Circuit Breaker (Panic Button)
# ══════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_circuit_breaker_triggers_after_consecutive_losses(self, workspace):
        """3 consecutive losses → MUST halt trading."""
        executor = TradeExecutor(workspace, mode=TradingMode.SIMULATION)
        executor._consecutive_losses = 3

        with pytest.raises(CircuitBreakerError):
            executor.execute_swap("USDC", "SOL", 100)


# ══════════════════════════════════════════════════
# TEST GROUP D: Metabolic Kernel (Ψ)
# ══════════════════════════════════════════════════

class TestMetabolicKernel:
    def test_psi_healthy_system(self, workspace):
        """TTL=50 days, no failures → low Ψ (EXPLORER mode)."""
        (workspace / ".auth" / "balance.json").write_text(
            '{"balance": 50, "bmr": 1.0}', encoding="utf-8"
        )
        snap = calculate_psi(workspace)
        assert snap.psi < 0.5
        assert snap.mode in ("EXPLORER", "BALANCED")
        assert snap.allows_research is True

    def test_psi_dying_system(self, workspace):
        """TTL=1 day → high Ψ (PREDATOR mode)."""
        (workspace / ".auth" / "balance.json").write_text(
            '{"balance": 1, "bmr": 1.0}', encoding="utf-8"
        )
        snap = calculate_psi(workspace)
        assert snap.psi > 0.5
        assert snap.forces_profit is True

    def test_psi_blocks_expensive_model(self, workspace):
        """High pressure → must block expensive models."""
        (workspace / ".auth" / "balance.json").write_text(
            '{"balance": 2, "bmr": 1.0}', encoding="utf-8"
        )
        snap = calculate_psi(workspace)
        assert snap.blocks_expensive_model is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
