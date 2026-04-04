"""
MetabolicKernel — DevClaw 的生存压强 (Ψ) 引擎。

Ψ (Psi) 是一个 0.0~1.0 的标量，决定此刻的 "性格" 和 "行为边界"：
  Ψ > 0.8  → PREDATOR   (掠食者模式：停止科研，全力套利/省钱)
  Ψ 0.4~0.8 → BALANCED   (平衡模式：一边赚钱一边进化)
  Ψ < 0.2  → EXPLORER   (探索者模式：顶级算力，深度重构)

公式：
  Ψ = w1 * financial_pressure + w2 * resource_pressure + w3 * failure_pressure

其中：
  financial_pressure = clamp(1 - TTL / TTL_safe, 0, 1)
  resource_pressure  = max(cpu%, mem%) / 100
  failure_pressure   = consecutive_failures / failure_threshold
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# ── Thresholds ──
_TTL_SAFE_DAYS = float(os.environ.get("PSI_TTL_SAFE_DAYS", "30"))
_FAILURE_THRESHOLD = int(os.environ.get("PSI_FAILURE_THRESHOLD", "5"))

# ── Weights (must sum to 1.0) ──
_W_FINANCIAL = 0.6
_W_RESOURCE = 0.2
_W_FAILURE = 0.2


@dataclass(frozen=True)
class PsiSnapshot:
    """A point-in-time survival pressure reading."""
    psi: float                 # 0.0 (safe) → 1.0 (dying)
    mode: str                  # PREDATOR / BALANCED / EXPLORER
    financial_pressure: float
    resource_pressure: float
    failure_pressure: float
    ttl_days: float
    balance_usd: float
    bmr_usd: float
    consecutive_failures: int

    @property
    def allows_research(self) -> bool:
        return self.psi < 0.4

    @property
    def forces_profit(self) -> bool:
        return self.psi > 0.6

    @property
    def blocks_expensive_model(self) -> bool:
        """Block Claude 3.5+ when under heavy pressure."""
        return self.psi > 0.5


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def calculate_psi(
    workspace: Path | str,
    *,
    balance_path: str = ".auth/balance.json",
) -> PsiSnapshot:
    """Calculate current survival pressure Ψ."""
    ws = Path(workspace).resolve()

    # ── Financial pressure ──
    try:
        data = json.loads((ws / balance_path).read_text(encoding="utf-8"))
        balance = float(data.get("balance", 0))
        bmr = max(float(data.get("bmr", 1.0)), 0.1)
        ttl = balance / bmr if balance > 0 else 0.0
    except Exception:
        balance, bmr, ttl = 0.0, 1.0, 0.5

    financial = _clamp(1.0 - ttl / _TTL_SAFE_DAYS)

    # ── Resource pressure ──
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        resource = _clamp(max(cpu, mem) / 100.0)
    except ImportError:
        resource = 0.0

    # ── Failure pressure ──
    try:
        state_path = ws / ".claw" / "survival_state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            failures = int(state.get("consecutive_failures", 0))
        else:
            failures = 0
    except Exception:
        failures = 0

    failure = _clamp(failures / _FAILURE_THRESHOLD)

    # ── Composite Ψ ──
    psi = _clamp(
        _W_FINANCIAL * financial
        + _W_RESOURCE * resource
        + _W_FAILURE * failure
    )

    # ── Mode determination ──
    if psi > 0.8:
        mode = "PREDATOR"
    elif psi > 0.4:
        mode = "BALANCED"
    else:
        mode = "EXPLORER"

    return PsiSnapshot(
        psi=round(psi, 3),
        mode=mode,
        financial_pressure=round(financial, 3),
        resource_pressure=round(resource, 3),
        failure_pressure=round(failure, 3),
        ttl_days=round(ttl, 1),
        balance_usd=round(balance, 2),
        bmr_usd=round(bmr, 2),
        consecutive_failures=failures,
    )


def psi_gate(
    workspace: Path | str,
    task_type: str = "general",
) -> tuple[bool, str, PsiSnapshot]:
    """
    Gate: should this task be allowed to execute?

    Returns (allowed, reason, snapshot).
    """
    snap = calculate_psi(workspace)

    # PREDATOR mode: block non-profit tasks
    if snap.forces_profit and task_type not in ("profit", "survival", "critical"):
        return (
            False,
            f"Ψ={snap.psi:.2f} (PREDATOR mode): 非盈利任务被拦截。"
            f" TTL={snap.ttl_days}天, 余额=${snap.balance_usd}",
            snap,
        )

    return (True, f"Ψ={snap.psi:.2f} ({snap.mode}): 允许执行", snap)
