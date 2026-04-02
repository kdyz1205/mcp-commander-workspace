"""
Hypothesis → Verify → Revise scaffolding: append structured episodes to task_plan.md.

Trading PnL is not wired here; use SurvivalState / consecutive_failures as coarse triggers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def append_reasoning_episode(
    workspace: Path,
    *,
    trigger: str,
    hypothesis: str,
    verify_plan: list[str],
    revise_hint: str = "",
) -> Path:
    """Append one markdown episode to task_plan.md (create minimal file if missing)."""
    workspace = Path(workspace).resolve()
    path = workspace / "task_plan.md"
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    checks = "\n".join(f"- {x}" for x in verify_plan) if verify_plan else "- （待补充可执行检查步骤）"
    block = f"""

### 推理片段 [{ts}] — {trigger[:120]}

**假设（Hypothesis）:**  
{hypothesis}

**待验证（Verify）:**  
{checks}

**若证伪则修正（Revise）:**  
{revise_hint or "（由下一轮 DevClaw 根据工具输出填写：改参数 / 换技能 / 加风控）"}

---
"""
    if path.is_file():
        existing = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(existing + block, encoding="utf-8")
    else:
        path.write_text(
            "# Task plan\n\n> 由 reasoning_episode 自动追加；请与仓库模板对齐后整理。\n" + block,
            encoding="utf-8",
        )
    return path


def maybe_auto_reasoning_stub(
    workspace: Path,
    *,
    state_value: str,
    reason: str,
    snapshot: dict[str, Any],
) -> Path | None:
    """
    If DEGRADED or consecutive_failures >= threshold, append a template episode (debounced).
    Returns path to task_plan.md if written, else None.
    """
    import os

    thr = int(os.environ.get("META_REASONING_FAIL_THRESHOLD", "3") or "3")
    cf = int(snapshot.get("consecutive_failures", 0) or 0)
    degraded = state_value.upper() == "DEGRADED"
    if not degraded and cf < max(1, thr):
        return None

    debounce = float(os.environ.get("META_REASONING_DEBOUNCE_SEC", "7200") or "7200")
    ws = Path(workspace).resolve()
    stamp_path = ws / ".claw" / "reasoning_debounce.json"
    now = time.time()
    try:
        if stamp_path.is_file():
            data = json.loads(stamp_path.read_text(encoding="utf-8"))
            last = float(data.get("auto_stub", 0) or 0)
            if now - last < debounce:
                return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass

    hypo = (
        f"系统信号：{reason}。连续失败计数={cf}。"
        "可能原因包括：策略阈值与当前波动不匹配、数据源延迟、或环境资源不足。"
        "下一步应先做**最小对照实验**再改生产参数。"
    )
    verify = [
        "用只读命令或 web_fetch 复核最近错误日志与生存快照",
        "若涉及交易逻辑：先在纸上或小样本回测验证新阈值（本仓库不自动实盘）",
        "将结论写入 append_typed_memory(category=lesson)",
    ]
    out = append_reasoning_episode(
        ws,
        trigger=f"auto:{state_value}" + (f"; fails>={thr}" if cf >= thr else ""),
        hypothesis=hypo,
        verify_plan=verify,
        revise_hint="若假设被否：load_skill skill-creator 起草新 SKILL，或 ultimate-synthesize 合并旧技能。",
    )
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if stamp_path.is_file():
            try:
                raw = json.loads(stamp_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except json.JSONDecodeError:
                data = {}
        data["auto_stub"] = now
        stamp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return out
