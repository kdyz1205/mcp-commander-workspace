"""Runtime control panel and natural-language operator intents for DevClaw."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path


_CONTROL_VERSION = 1
_RUNTIME_DIR = ".claw"
_CONTROL_FILE = "runtime_control.json"
_PANEL_FILE = "runtime_panel.md"

_START_PHRASES = (
    "开始干活",
    "开始工作",
    "开始处理",
    "开始接任务",
    "开始接活",
    "开工",
    "启动",
    "恢复工作",
    "恢复处理",
    "继续干活",
    "继续工作",
    "继续处理",
    "resume",
    "start working",
    "start",
    "wake up",
)
_PAUSE_PHRASES = (
    "暂停",
    "停下",
    "停止工作",
    "停止接任务",
    "停止接活",
    "休眠",
    "待机",
    "安静",
    "静默",
    "别再接任务",
    "别再干活",
    "pause",
    "hold",
    "stand by",
    "stop working",
)
_STATUS_PHRASES = (
    "控制面板",
    "面板",
    "现在状态",
    "状态如何",
    "状态怎么样",
    "汇报状态",
    "报告状态",
    "你还活着吗",
    "还活着吗",
    "现在在干嘛",
    "status",
    "panel",
    "control panel",
    "vitals",
)
_AUTONOMY_ON_PHRASES = (
    "恢复自主",
    "开启自主",
    "打开自主",
    "恢复心跳",
    "开启心跳",
    "恢复自动",
    "开启自动",
    "resume autonomy",
    "start autonomy",
    "enable autonomy",
)
_AUTONOMY_OFF_PHRASES = (
    "关闭自主",
    "停掉心跳",
    "不要自主",
    "保持静默",
    "只在我叫你时",
    "手动模式",
    "manual only",
    "quiet mode",
    "disable autonomy",
    "pause autonomy",
)
_SIMULATION_PHRASES = (
    "不要用真钱",
    "别用真钱",
    "只模拟",
    "模拟交易",
    "纸上交易",
    "禁止实盘",
    "关闭实盘",
    "不要下单",
    "不要碰okx",
    "不要动okx",
    "不要动我的okx",
    "dry run",
    "paper trade",
    "paper trading",
    "simulation only",
    "sim only",
)
_TRADING_DISABLE_PHRASES = (
    "禁止交易",
    "关闭交易",
    "别碰交易",
    "不要交易",
    "disable trading",
    "stop trading",
)
_LIVE_TRADING_PHRASES = (
    "真钱交易",
    "实盘交易",
    "开启实盘",
    "打开实盘",
    "启用真钱",
    "enable live trading",
    "live trading",
)
_CONTROL_FILLERS = (
    "请",
    "麻烦",
    "帮我",
    "帮忙",
    "一下",
    "先",
    "然后",
    "并且",
    "再",
    "好吗",
    "吧",
)
_LOW_SIGNAL_RESIDUALS = {
    "",
    "好",
    "好的",
    "收到",
    "继续",
    "开始",
    "恢复",
    "暂停",
    "停止",
    "状态",
    "面板",
}


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _runtime_dir(workspace: Path) -> Path:
    return workspace / _RUNTIME_DIR


def runtime_control_path(workspace: Path) -> Path:
    return _runtime_dir(workspace) / _CONTROL_FILE


def runtime_panel_path(workspace: Path) -> Path:
    return _runtime_dir(workspace) / _PANEL_FILE


@dataclass(frozen=True)
class RuntimeControlState:
    version: int = _CONTROL_VERSION
    accepting_tasks: bool = True
    background_master_enabled: bool = False
    idle_autotick_enabled: bool = False
    logic_chain_enabled: bool = False
    autonomous_life_enabled: bool = False
    trading_mode: str = "simulation"
    live_trading_enabled: bool = False
    manual_trading_approval_required: bool = True
    updated_at: str = ""
    updated_by: str = "bootstrap"
    last_operator_text: str = ""
    note: str = ""


@dataclass(frozen=True)
class ControlMessageOutcome:
    handled: bool
    reply: str
    state: RuntimeControlState
    queue_instruction: str | None = None


def _default_state() -> RuntimeControlState:
    background_flags = {
        "idle": _truthy("TG_IDLE_AUTOTICK"),
        "logic": _truthy("TG_AUTONOMOUS_LOGIC_CHAIN"),
        "life": _truthy("TG_AUTONOMOUS_LIFE"),
    }
    return RuntimeControlState(
        accepting_tasks=True,
        background_master_enabled=any(background_flags.values()),
        idle_autotick_enabled=background_flags["idle"],
        logic_chain_enabled=background_flags["logic"],
        autonomous_life_enabled=background_flags["life"],
        trading_mode="simulation",
        live_trading_enabled=False,
        manual_trading_approval_required=True,
        updated_at=_utc_now(),
        updated_by="bootstrap",
        note="Live trading is hard-disabled by default; simulation mode only.",
    )


def _coerce_state(payload: dict[str, object]) -> RuntimeControlState:
    base = _default_state()
    values = asdict(base)
    for key in values:
        if key in payload:
            values[key] = payload[key]
    return RuntimeControlState(**values)


def _write_state_files(workspace: Path, state: RuntimeControlState) -> None:
    runtime_dir = _runtime_dir(workspace)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_control_path(workspace).write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    runtime_panel_path(workspace).write_text(render_runtime_panel(state), encoding="utf-8")


def ensure_runtime_control(workspace: Path) -> RuntimeControlState:
    path = runtime_control_path(workspace)
    if path.is_file():
        return load_runtime_control(workspace)
    state = _default_state()
    _write_state_files(workspace, state)
    return state


def load_runtime_control(workspace: Path) -> RuntimeControlState:
    path = runtime_control_path(workspace)
    if not path.is_file():
        return ensure_runtime_control(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = _default_state()
        _write_state_files(workspace, state)
        return state
    state = _coerce_state(payload if isinstance(payload, dict) else {})
    _write_state_files(workspace, state)
    return state


def update_runtime_control(
    workspace: Path,
    *,
    actor: str,
    source_text: str,
    note: str,
    **changes: object,
) -> RuntimeControlState:
    current = load_runtime_control(workspace)
    updated = replace(
        current,
        updated_at=_utc_now(),
        updated_by=actor,
        last_operator_text=source_text.strip()[:500],
        note=note,
        **changes,
    )
    _write_state_files(workspace, updated)
    return updated


def render_runtime_panel(state: RuntimeControlState) -> str:
    lines = [
        "# Runtime Control Panel",
        "",
        f"- task_intake: {'active' if state.accepting_tasks else 'paused'}",
        f"- background_master: {'enabled' if state.background_master_enabled else 'disabled'}",
        f"- idle_autotick: {'enabled' if state.idle_autotick_enabled else 'disabled'}",
        f"- logic_chain: {'enabled' if state.logic_chain_enabled else 'disabled'}",
        f"- autonomous_life: {'enabled' if state.autonomous_life_enabled else 'disabled'}",
        f"- trading_mode: {state.trading_mode}",
        f"- live_trading_enabled: {state.live_trading_enabled}",
        f"- manual_trading_approval_required: {state.manual_trading_approval_required}",
        f"- updated_at: {state.updated_at or _utc_now()}",
        f"- updated_by: {state.updated_by}",
    ]
    if state.last_operator_text:
        lines.append(f"- last_operator_text: {state.last_operator_text}")
    if state.note:
        lines.append(f"- note: {state.note}")
    return "\n".join(lines) + "\n"


def panel_summary(state: RuntimeControlState) -> str:
    return (
        "控制面板\n"
        f"- 接任务: {'开' if state.accepting_tasks else '停'}\n"
        f"- 自主循环: {'开' if state.background_master_enabled else '停'}\n"
        f"- 空闲自检: {'开' if state.idle_autotick_enabled else '关'}\n"
        f"- 逻辑链: {'开' if state.logic_chain_enabled else '关'}\n"
        f"- 自主心跳: {'开' if state.autonomous_life_enabled else '关'}\n"
        f"- 交易模式: {state.trading_mode}\n"
        f"- 真钱交易: {'允许' if state.live_trading_enabled else '禁用'}\n"
        f"- 人工批准: {'需要' if state.manual_trading_approval_required else '未要求'}\n"
        f"- 最近更新: {state.updated_at or 'n/a'}"
    )


def allows_idle_autotick(state: RuntimeControlState) -> bool:
    return state.background_master_enabled and state.idle_autotick_enabled


def allows_logic_chain(state: RuntimeControlState) -> bool:
    return state.background_master_enabled and state.logic_chain_enabled


def allows_autonomous_life(state: RuntimeControlState) -> bool:
    return state.background_master_enabled and state.autonomous_life_enabled


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _strip_control_phrases(text: str) -> str:
    cleaned = text
    phrase_bag = sorted(
        set(
            _START_PHRASES
            + _PAUSE_PHRASES
            + _STATUS_PHRASES
            + _AUTONOMY_ON_PHRASES
            + _AUTONOMY_OFF_PHRASES
            + _SIMULATION_PHRASES
            + _TRADING_DISABLE_PHRASES
            + _LIVE_TRADING_PHRASES
            + _CONTROL_FILLERS
        ),
        key=len,
        reverse=True,
    )
    for phrase in phrase_bag:
        cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[，,。.!！？:：;；/\\|\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _residual_instruction(text: str) -> str | None:
    residual = _strip_control_phrases(text)
    if residual.lower() in _LOW_SIGNAL_RESIDUALS:
        return None
    return residual or None


def interpret_control_message(
    workspace: Path,
    text: str,
    *,
    actor: str = "operator",
) -> ControlMessageOutcome | None:
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    wants_status = _contains_any(lowered, _STATUS_PHRASES)
    wants_pause = _contains_any(lowered, _PAUSE_PHRASES)
    wants_start = _contains_any(lowered, _START_PHRASES)
    wants_autonomy_on = _contains_any(lowered, _AUTONOMY_ON_PHRASES)
    wants_autonomy_off = _contains_any(lowered, _AUTONOMY_OFF_PHRASES)
    wants_simulation = _contains_any(lowered, _SIMULATION_PHRASES)
    wants_disable_trading = _contains_any(lowered, _TRADING_DISABLE_PHRASES)
    wants_live_trading = _contains_any(lowered, _LIVE_TRADING_PHRASES)

    if not any(
        (
            wants_status,
            wants_pause,
            wants_start,
            wants_autonomy_on,
            wants_autonomy_off,
            wants_simulation,
            wants_disable_trading,
            wants_live_trading,
        )
    ):
        return None

    state = load_runtime_control(workspace)
    changes: dict[str, object] = {}
    notes: list[str] = []
    queue_instruction: str | None = None

    if wants_live_trading:
        notes.append("已拒绝开启真钱交易；当前版本只允许 simulation/manual-review。")

    if wants_disable_trading:
        changes["trading_mode"] = "disabled"
        changes["live_trading_enabled"] = False
        notes.append("已关闭交易相关动作。")
    elif wants_simulation:
        changes["trading_mode"] = "simulation"
        changes["live_trading_enabled"] = False
        notes.append("已锁定为 simulation only，不会触发真钱下单。")

    if wants_pause:
        changes["accepting_tasks"] = False
        changes["background_master_enabled"] = False
        notes.append("已暂停接任务，并关闭自主循环。")

    if wants_autonomy_off:
        changes["background_master_enabled"] = False
        if not wants_pause:
            changes.setdefault("accepting_tasks", True)
        notes.append("已切到手动模式，只在你发消息时工作。")

    if wants_start:
        changes["accepting_tasks"] = True
        changes.setdefault("background_master_enabled", True)
        notes.append("已恢复开工。")

    if wants_autonomy_on:
        changes["background_master_enabled"] = True
        changes.setdefault("accepting_tasks", True)
        notes.append("已恢复自主循环。")

    note = " ".join(dict.fromkeys(notes)) or state.note or "Operator update."
    if changes:
        state = update_runtime_control(
            workspace,
            actor=actor,
            source_text=raw,
            note=note,
            **changes,
        )
    elif wants_status:
        state = load_runtime_control(workspace)

    if (wants_start or wants_autonomy_on or wants_simulation or wants_disable_trading) and not wants_pause:
        queue_instruction = _residual_instruction(raw)
        if wants_status and queue_instruction == "状态":
            queue_instruction = None

    reply_lines = []
    if notes:
        reply_lines.append(" ".join(dict.fromkeys(notes)))
    reply_lines.append(panel_summary(state))
    if queue_instruction:
        reply_lines.append(f"已恢复接单，并准备执行: {queue_instruction}")
    return ControlMessageOutcome(
        handled=True,
        reply="\n\n".join(reply_lines),
        state=state,
        queue_instruction=queue_instruction,
    )
