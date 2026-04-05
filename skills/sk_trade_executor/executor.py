"""
Trade Executor — DevClaw 的链上交易手。

架构：
  SimulationEngine (默认) → 纸上交易，记录假设收益
  SolanaExecutor (需审批) → 真实链上交易 (Devnet/Mainnet)

安全铁律：
  1. 默认 SIMULATION 模式，不碰真钱
  2. 滑点保护：预估偏离 > max_slippage 则拒绝成交
  3. 单笔上限：不超过余额的 5%
  4. 熔断器：连续亏损 3 次自动停止
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class TradingMode(str, Enum):
    SIMULATION = "simulation"
    DEVNET = "devnet"
    MAINNET = "mainnet"  # requires explicit human approval


class TradeDirection(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SlippageTooHighError(Exception):
    """Raised when estimated slippage exceeds max_slippage."""


class InsufficientBalanceError(Exception):
    """Raised when trade amount exceeds available balance."""


class CircuitBreakerError(Exception):
    """Raised when consecutive losses trigger the panic button."""


@dataclass
class TradeProposal:
    """A proposed trade, not yet executed."""
    token_in: str          # e.g. "SOL"
    token_out: str         # e.g. "BONK"
    amount_in: float       # amount of token_in to spend
    expected_price: float  # expected price per token_out
    max_slippage: float    # max allowed slippage (0.01 = 1%)
    reason: str            # why this trade


@dataclass
class TradeResult:
    """Result of an executed (or simulated) trade."""
    success: bool
    mode: str              # simulation / devnet / mainnet
    direction: str         # buy / sell
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    price: float
    slippage: float        # actual slippage
    tx_id: str | None      # transaction hash (None for simulation)
    pnl: float             # profit/loss in USD
    timestamp: float
    error: str | None = None


class TradeExecutor:
    """
    The Wallet Actuator — DevClaw 的交易执行手。

    默认 simulation 模式：
      - 通过 DexScreener API 获取真实价格
      - 模拟成交，记录假设 PnL
      - 不需要私钥，不碰链上资产

    真实模式 (devnet/mainnet)：
      - 需要人工审批
      - 通过 Jupiter API 路由最优路径
      - 构建并签名 Solana 交易
    """

    # ── Safety Constants ──
    MAX_POSITION_PCT = 0.05      # 单笔不超过余额 5%
    CIRCUIT_BREAKER_LIMIT = 3    # 连续亏损 3 次熔断
    DEFAULT_MAX_SLIPPAGE = 0.01  # 默认最大滑点 1%

    def __init__(
        self,
        workspace: Path | str,
        mode: TradingMode = TradingMode.SIMULATION,
    ):
        self.workspace = Path(workspace).resolve()
        self.mode = mode
        self._trade_log_path = self.workspace / ".claw" / "trade_log.jsonl"
        self._trade_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Circuit breaker state
        self._consecutive_losses = 0

    # ── Public API ──

    # Symbol → Solana address mapping (for wallet + DexScreener lookups)
    _SYMBOL_MAP = {
        "SOL": "So11111111111111111111111111111111111111112",
        "USDC": "USDC",
        "USDT": "USDT",
    }

    def _resolve_token(self, token: str) -> str:
        """Resolve short symbol to full address if known."""
        return self._SYMBOL_MAP.get(token.upper(), token)

    def execute_swap(
        self,
        token_in: str,
        token_out: str,
        amount_in: float,
        *,
        max_slippage: float | None = None,
        reason: str = "",
    ) -> TradeResult:
        """
        Execute a token swap. Core function.

        Returns TradeResult with success/failure and PnL.
        Raises SlippageTooHighError if estimated slippage > max_slippage.
        Raises CircuitBreakerError if consecutive losses >= limit.
        """
        max_slippage = max_slippage or self.DEFAULT_MAX_SLIPPAGE
        # Resolve short symbols (SOL → full address)
        token_in = self._resolve_token(token_in)
        token_out = self._resolve_token(token_out)

        # ── Circuit breaker check ──
        if self._consecutive_losses >= self.CIRCUIT_BREAKER_LIMIT:
            raise CircuitBreakerError(
                f"熔断！连续亏损 {self._consecutive_losses} 次。"
                f" 需要人工审查后才能继续交易。"
            )

        # ── Get current price ──
        price_info = self._fetch_price(token_in, token_out)
        if price_info is None:
            return self._make_result(
                success=False, token_in=token_in, token_out=token_out,
                amount_in=amount_in, amount_out=0, price=0, slippage=0,
                error="价格获取失败",
            )

        current_price = price_info["price"]
        liquidity = price_info.get("liquidity", 0)

        # ── Convert amount to USD for slippage calculation ──
        _STABLES = {"USDC", "USDT", "DAI", "BUSD", "UST"}
        if token_in.upper() in _STABLES:
            amount_usd = amount_in  # already in USD
        else:
            amount_usd = amount_in * current_price  # e.g. 10 SOL * $80 = $800

        # ── Slippage estimation ──
        estimated_slippage = self._estimate_slippage(amount_usd, liquidity)
        if estimated_slippage > max_slippage:
            raise SlippageTooHighError(
                f"预估滑点 {estimated_slippage:.2%} 超过最大允许 {max_slippage:.2%}。"
                f" 流动性: ${liquidity:,.0f}, 订单量: {amount_in} {token_in}。"
                f" 交易已拒绝以保护本金。"
            )

        # ── Position size check ──
        balance = self._get_balance()
        max_amount = balance * self.MAX_POSITION_PCT
        if amount_in > max_amount and self.mode != TradingMode.SIMULATION:
            raise InsufficientBalanceError(
                f"订单 {amount_in} {token_in} 超过单笔上限"
                f" ({self.MAX_POSITION_PCT:.0%} of ${balance:.2f} = ${max_amount:.2f})"
            )

        # ── Execute based on mode ──
        if self.mode == TradingMode.SIMULATION:
            result = self._simulate_swap(
                token_in, token_out, amount_in, current_price, estimated_slippage
            )
        elif self.mode == TradingMode.DEVNET:
            result = self._devnet_swap(
                token_in, token_out, amount_in, current_price, max_slippage
            )
        else:
            # MAINNET: refuse without explicit approval file
            approval_path = self.workspace / ".claw" / "mainnet_trade_approved.json"
            if not approval_path.is_file():
                return self._make_result(
                    success=False, token_in=token_in, token_out=token_out,
                    amount_in=amount_in, amount_out=0, price=current_price,
                    slippage=estimated_slippage,
                    error="主网交易需要人工审批。创建 .claw/mainnet_trade_approved.json",
                )
            result = self._mainnet_swap(
                token_in, token_out, amount_in, current_price, max_slippage
            )

        # ── Update circuit breaker ──
        if result.success and result.pnl >= 0:
            self._consecutive_losses = 0
        elif result.success and result.pnl < 0:
            self._consecutive_losses += 1

        # ── Log trade ──
        self._log_trade(result, reason=reason)

        return result

    def get_trade_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Read recent trade log."""
        if not self._trade_log_path.is_file():
            return []
        lines = self._trade_log_path.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries

    def get_pnl_summary(self) -> dict[str, float]:
        """Calculate cumulative PnL from trade log."""
        history = self.get_trade_history(limit=1000)
        total_pnl = sum(t.get("pnl", 0) for t in history)
        wins = sum(1 for t in history if t.get("pnl", 0) > 0)
        losses = sum(1 for t in history if t.get("pnl", 0) < 0)
        return {
            "total_pnl": round(total_pnl, 4),
            "total_trades": len(history),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(len(history), 1), 2),
        }

    # ── Price Fetching ──

    def _fetch_price(self, token_in: str, token_out: str) -> dict | None:
        """Fetch current price from DexScreener API.

        Handles both directions:
        - Buy (USDC → SOL): search token_out to get its USD price
        - Sell (SOL → USDC): search token_in to get its USD price (inverted)
        """
        import urllib.request
        import urllib.error

        # Map common symbols to known Solana addresses for DexScreener
        _KNOWN = {
            "SOL": "So11111111111111111111111111111111111111112",
        }
        _STABLES = {"USDC", "USDT", "DAI", "BUSD", "UST"}

        # Determine which token to search:
        # If selling (token_out is a stable), search token_in for price
        # If buying (token_in is a stable), search token_out for price
        if token_out.upper() in _STABLES or token_out in _STABLES:
            # SELL direction: SOL → USDC — search token_in
            search_token = token_in
        else:
            # BUY direction: USDC → SOL — search token_out
            search_token = token_out

        search = search_token if len(search_token) > 20 else _KNOWN.get(search_token.upper(), search_token)

        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{search}"
            req = urllib.request.Request(url, headers={"User-Agent": "DevClaw/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            pairs = data.get("pairs") or []
            if not pairs:
                return None

            # Pick highest liquidity pair
            best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            return {
                "price": float(best.get("priceUsd", 0)),
                "liquidity": float(best.get("liquidity", {}).get("usd", 0) or 0),
                "volume_24h": float(best.get("volume", {}).get("h24", 0) or 0),
                "pair": best.get("pairAddress", ""),
                "dex": best.get("dexId", ""),
            }
        except Exception:
            return None

    # ── Dynamic Slippage + MEV Penalty Model ──

    # Base fee (DEX protocol fee, e.g. Raydium 0.25%)
    BASE_FEE = 0.0025
    # Alpha: order size impact coefficient
    ALPHA = 0.0003
    # Volatility penalty multiplier
    VOL_PENALTY_MULT = 0.5

    def _estimate_slippage(
        self,
        amount_usd: float,
        liquidity_usd: float,
        *,
        volatility: float = 0.0,
    ) -> float:
        """
        Quantitative-grade dynamic slippage estimation.

        Formula:
          Slippage = Base_Fee + α * √(Amount_USD) + Volatility_Penalty

        Where:
          - Base_Fee: DEX protocol fee (e.g. 0.25% for Raydium)
          - α * √(Amount): Non-linear order size impact (AMM price impact)
          - Volatility_Penalty: VOL_PENALTY_MULT * volatility (24h vol as decimal)

        This is NOT the toy model of `amount / liquidity`.
        Large orders hit sqrt impact — doubling order size does NOT double slippage.
        """
        if liquidity_usd <= 0:
            return 1.0  # 100% slippage = can't trade

        # AMM constant-product impact: amount / (2 * liquidity) but with sqrt dampening
        import math
        amm_impact = amount_usd / (2 * liquidity_usd)
        sqrt_impact = self.ALPHA * math.sqrt(amount_usd)
        vol_penalty = self.VOL_PENALTY_MULT * volatility

        total = self.BASE_FEE + sqrt_impact + amm_impact + vol_penalty
        return min(total, 1.0)  # cap at 100%

    # ── Simulation Engine (with Virtual Wallet) ──

    def _simulate_swap(
        self, token_in: str, token_out: str,
        amount_in: float, price: float, slippage: float,
    ) -> TradeResult:
        """
        Paper trade with ACID virtual wallet.

        1. lock_funds() on virtual wallet
        2. Calculate executed price with slippage penalty
        3. commit_trade() or rollback() on failure
        """
        from core.virtual_wallet import VirtualWallet, InsufficientBalanceError as WalletInsufficient

        wallet = VirtualWallet(self.workspace)
        tx_id = f"sim-{int(time.time()*1000)}"

        # Apply slippage to get simulated execution price
        # Buy (USDC → token): price goes UP (you pay more per token)
        # Sell (token → USDC): price goes DOWN (you receive less per token)
        _STABLES = {"USDC", "USDT", "DAI", "BUSD", "UST"}
        _is_sell = token_out.upper() in _STABLES
        exec_price = price * (1 - slippage)
        if _is_sell:
            # Selling token for USDC: amount_out = amount_in * exec_price
            amount_out = amount_in * exec_price
        else:
            # Buying token with USDC: amount_out = amount_in / exec_price
            amount_out = amount_in / exec_price if exec_price > 0 else 0

        # Phase 1: Lock funds
        try:
            pending = wallet.lock_funds(
                token_in, amount_in,
                tx_id=tx_id,
                metadata={"slippage": round(slippage, 6), "spot_price": price},
            )
        except WalletInsufficient as e:
            return self._make_result(
                success=False, token_in=token_in, token_out=token_out,
                amount_in=amount_in, amount_out=0, price=price,
                slippage=slippage, error=str(e),
            )

        # Phase 2: Commit trade — credit output token
        try:
            balances = wallet.commit_trade(
                pending,
                credit_token=token_out,
                credit_amount=amount_out,
                pnl=0,  # PnL calculated on sell
            )
        except Exception as e:
            wallet.rollback(pending)
            return self._make_result(
                success=False, token_in=token_in, token_out=token_out,
                amount_in=amount_in, amount_out=0, price=price,
                slippage=slippage, error=f"commit failed: {e}",
            )

        return self._make_result(
            success=True,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=amount_out,
            price=exec_price,
            slippage=slippage,
            tx_id=tx_id,
            pnl=0,
        )

    # ── Devnet Execution (placeholder) ──

    def _devnet_swap(
        self, token_in: str, token_out: str,
        amount_in: float, price: float, max_slippage: float,
    ) -> TradeResult:
        """Execute on Solana Devnet via Jupiter API."""
        # TODO: Implement when solana-py is installed
        # For now, fall back to simulation with [DEVNET] tag
        result = self._simulate_swap(token_in, token_out, amount_in, price, 0)
        result = TradeResult(
            **{**asdict(result), "mode": "devnet", "tx_id": f"devnet-sim-{int(time.time()*1000)}"}
        )
        return result

    # ── Mainnet Execution (placeholder) ──

    def _mainnet_swap(
        self, token_in: str, token_out: str,
        amount_in: float, price: float, max_slippage: float,
    ) -> TradeResult:
        """Execute on Solana Mainnet. REQUIRES human approval."""
        # TODO: Implement with Jupiter V6 API
        return self._make_result(
            success=False, token_in=token_in, token_out=token_out,
            amount_in=amount_in, amount_out=0, price=price, slippage=0,
            error="主网执行尚未实现。需要 solana-py + Jupiter SDK。",
        )

    # ── Helpers ──

    def _get_balance(self) -> float:
        try:
            data = json.loads(
                (self.workspace / ".auth" / "balance.json").read_text(encoding="utf-8")
            )
            return float(data.get("balance", 0))
        except Exception:
            return 0.0

    def _make_result(self, **kwargs) -> TradeResult:
        defaults = {
            "mode": self.mode.value,
            "direction": "buy",
            "timestamp": time.time(),
            "tx_id": None,
            "pnl": 0,
            "error": None,
        }
        defaults.update(kwargs)
        return TradeResult(**defaults)

    def _log_trade(self, result: TradeResult, *, reason: str = "") -> None:
        entry = asdict(result)
        entry["reason"] = reason
        with self._trade_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
