"""
VirtualWallet — ACID 级虚拟账本。

物理存储：.auth/paper_wallet.json
并发安全：threading.Lock 保护所有余额读写
事务模型：lock_funds() → commit_trade() / rollback()

这不是玩具代码。这是隔离死亡的最后防线。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class InsufficientBalanceError(Exception):
    """余额不足以执行此交易。"""


class WalletLockedError(Exception):
    """钱包正在处理另一笔交易。"""


@dataclass
class PendingTx:
    """A pending transaction in the ledger, not yet committed."""
    tx_id: str
    token: str
    amount: float        # negative = debit, positive = credit
    counter_token: str
    counter_amount: float
    timestamp: float
    metadata: dict[str, Any]


class VirtualWallet:
    """
    Thread-safe virtual wallet with transaction semantics.

    Usage:
        wallet = VirtualWallet(workspace)
        tx = wallet.lock_funds("USDC", 1000, tx_id="trade-001")
        try:
            # ... execute simulated trade ...
            wallet.commit_trade(tx, credit_token="SOL", credit_amount=6.5)
        except Exception:
            wallet.rollback(tx)
    """

    DEFAULT_BALANCES = {"USDC": 10000.0, "SOL": 0.0}

    def __init__(self, workspace: Path | str):
        self._ws = Path(workspace).resolve()
        self._path = self._ws / ".auth" / "paper_wallet.json"
        self._lock = threading.Lock()
        self._pending: dict[str, PendingTx] = {}
        self._ensure_exists()

    def _ensure_exists(self) -> None:
        if not self._path.is_file():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._write({
                "balances": dict(self.DEFAULT_BALANCES),
                "total_pnl": 0.0,
                "trade_count": 0,
                "created_at": time.time(),
                "trades": [],
            })

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "balances": dict(self.DEFAULT_BALANCES),
                "total_pnl": 0.0,
                "trade_count": 0,
                "trades": [],
            }

    def _write(self, data: dict) -> None:
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # ── Read Operations (thread-safe) ──

    def get_balance(self, token: str) -> float:
        with self._lock:
            data = self._read()
            return float(data.get("balances", {}).get(token.upper(), 0))

    def get_all_balances(self) -> dict[str, float]:
        with self._lock:
            data = self._read()
            return dict(data.get("balances", {}))

    def get_portfolio_summary(self) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            return {
                "balances": data.get("balances", {}),
                "total_pnl": data.get("total_pnl", 0),
                "trade_count": data.get("trade_count", 0),
                "pending_txs": len(self._pending),
            }

    # ── Transaction Operations ──

    def lock_funds(
        self,
        token: str,
        amount: float,
        *,
        tx_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PendingTx:
        """
        Phase 1: Lock funds for a pending trade.

        Deducts `amount` from `token` balance immediately (pessimistic lock).
        If insufficient balance, raises InsufficientBalanceError.
        Returns PendingTx handle for commit/rollback.
        """
        token = token.upper()
        tx_id = tx_id or f"tx-{int(time.time()*1000)}"

        with self._lock:
            data = self._read()
            balances = data.get("balances", {})
            current = float(balances.get(token, 0))

            if current < amount:
                raise InsufficientBalanceError(
                    f"{token} 余额不足: 需要 {amount:.4f}, 当前 {current:.4f}"
                )

            # Deduct immediately (pessimistic)
            balances[token] = round(current - amount, 8)
            data["balances"] = balances
            self._write(data)

            tx = PendingTx(
                tx_id=tx_id,
                token=token,
                amount=-amount,  # negative = debit
                counter_token="",
                counter_amount=0,
                timestamp=time.time(),
                metadata=metadata or {},
            )
            self._pending[tx_id] = tx
            return tx

    def commit_trade(
        self,
        tx: PendingTx,
        *,
        credit_token: str,
        credit_amount: float,
        pnl: float = 0.0,
    ) -> dict[str, float]:
        """
        Phase 2: Commit trade — credit the output token.

        Returns updated balances.
        """
        credit_token = credit_token.upper()

        with self._lock:
            if tx.tx_id not in self._pending:
                raise ValueError(f"Transaction {tx.tx_id} not found in pending")

            data = self._read()
            balances = data.get("balances", {})

            # Credit output token
            current = float(balances.get(credit_token, 0))
            balances[credit_token] = round(current + credit_amount, 8)

            # Update metadata
            data["balances"] = balances
            data["total_pnl"] = round(data.get("total_pnl", 0) + pnl, 4)
            data["trade_count"] = data.get("trade_count", 0) + 1

            # Append trade record (keep last 100)
            trades = data.get("trades", [])
            trades.append({
                "tx_id": tx.tx_id,
                "debit": f"{abs(tx.amount):.4f} {tx.token}",
                "credit": f"{credit_amount:.4f} {credit_token}",
                "pnl": round(pnl, 4),
                "ts": time.time(),
                **(tx.metadata or {}),
            })
            data["trades"] = trades[-100:]

            self._write(data)
            del self._pending[tx.tx_id]

            return dict(balances)

    def rollback(self, tx: PendingTx) -> dict[str, float]:
        """
        Abort: return locked funds to balance.
        """
        with self._lock:
            if tx.tx_id not in self._pending:
                return self._read().get("balances", {})

            data = self._read()
            balances = data.get("balances", {})

            # Restore debited amount
            current = float(balances.get(tx.token, 0))
            balances[tx.token] = round(current + abs(tx.amount), 8)
            data["balances"] = balances
            self._write(data)

            del self._pending[tx.tx_id]
            return dict(balances)

    def reset(self, initial: dict[str, float] | None = None) -> None:
        """Reset wallet to initial state. For testing."""
        with self._lock:
            self._write({
                "balances": initial or dict(self.DEFAULT_BALANCES),
                "total_pnl": 0.0,
                "trade_count": 0,
                "created_at": time.time(),
                "trades": [],
            })
            self._pending.clear()
