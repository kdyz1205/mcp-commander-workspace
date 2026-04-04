"""
Offline/degraded local brain for DevClaw.

This path is intentionally bounded: it keeps the bot responsive without cloud keys
and focuses on deterministic survival, repair, synthesis, migration, and planning actions.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from claw_runtime.memory import append_memory
from claw_runtime.research_lab import run_public_research_scout
from claw_runtime.reasoning_episode import append_reasoning_episode
from claw_runtime.skill_registry import SkillRegistry
from claw_runtime.survival_engine import SurvivalEngine
from claw_runtime.ultimate.colab_bundle import build_colab_job_bundle
from claw_runtime.ultimate.nomad import write_nomad_snapshot
from claw_runtime.ultimate.proxy_env import rotate_proxy_index
from claw_runtime.ultimate.self_heal import emit_rebuild_venv_scripts
from claw_runtime.ultimate.skill_synthesis import synthesize_two_skills
from claw_runtime.ultimate.treasury import (
    write_survival_revenue_plan_stub,
    write_treasury_proposal_stub,
)


@dataclass
class OfflineBrainResult:
    summary: str
    actions: list[dict[str, str]]


def _emit(emit: Callable[[str], None] | None, text: str) -> None:
    if emit:
        emit(text)


def _default_bundle_paths(workspace: Path) -> list[str]:
    candidates = [
        "task_plan.md",
        "claw.config.json",
        "env.example",
        "docs/META_EVOLUTION_ARCHITECTURE.md",
    ]
    out: list[str] = []
    for rel in candidates:
        if (workspace / rel).is_file():
            out.append(rel)
    return out or ["task_plan.md"]


def _pick_synthesis_pair(registry: SkillRegistry, instruction: str) -> tuple[str, str] | None:
    skills = registry.refresh()
    if not skills:
        return None
    lowered = instruction.lower()
    matched: list[str] = []
    for name in sorted(skills.keys(), key=len, reverse=True):
        if name.lower() in lowered:
            matched.append(name)
        if len(matched) >= 2:
            return matched[0], matched[1]
    preferred = [name for name in ("git-commit", "summarize") if name in skills]
    if len(preferred) >= 2:
        return preferred[0], preferred[1]
    names = sorted(skills.keys())
    if len(names) >= 2:
        return names[0], names[1]
    return None


def _should(text: str, *needles: str) -> bool:
    lowered = text.lower()
    return any(n.lower() in lowered for n in needles)


def _research_request(text: str) -> bool:
    return _should(
        text,
        "\u8bba\u6587",
        "paper",
        "papers",
        "research",
        "arxiv",
        "\u6df1\u5ea6\u5b66\u4e60",
        "deep learning",
        "machine learning",
        "\u56e0\u5b50",
        "factor",
        "\u4ea4\u6613",
        "\u4ea4\u6613\u7814\u7a76",
        "\u91cf\u5316",
        "\u91cf\u5316\u7814\u7a76",
        "quant",
        "trading",
        "alpha",
    )


def run_offline_brain(
    workspace: Path | str,
    user_instruction: str,
    *,
    emit: Callable[[str], None] | None = None,
    failure_reason: str = "",
) -> OfflineBrainResult:
    workspace = Path(workspace).resolve()
    registry = SkillRegistry(workspace)
    survival = SurvivalEngine(workspace)
    survival.heartbeat()
    snapshot = survival.snapshot()
    state, reason = survival.assess_survival_state()
    text = (user_instruction or "").strip()
    actions: list[dict[str, str]] = []

    # ── PRIORITY 1: Answer the user's actual question FIRST ──────────
    # The user asked something. They don't care about our maintenance.
    # Try every possible brain to give them a real answer.
    _user_answered = False
    if text and not text.startswith("[") and not text.startswith("/"):
        # Try 1: Claude CLI (free, Tier 1)
        try:
            from claw_runtime.cognitive_outsourcing import delegate_to_claude_cli
            result = delegate_to_claude_cli(
                f"用户问了这个问题，请用中文简洁友好地回答：\n\n{text}",
                workspace,
                timeout_sec=120,
            )
            if result.success and result.output.strip():
                _emit(emit, result.output.strip()[:3000])
                actions.append({"name": "cognitive_outsource", "detail": f"claude answered ({len(result.output)} chars)"})
                _user_answered = True
        except Exception:
            pass

        # Try 2: Ollama directly via subprocess (in case OpenAI client probe fails but ollama works)
        if not _user_answered:
            try:
                import subprocess
                import shutil
                _ollama_bin = shutil.which("ollama") or os.path.expanduser("~/AppData/Local/Programs/Ollama/ollama.exe")
                for model_cmd in ("gemma3:4b", "qwen2.5-coder:3b", "gemma4:latest"):
                    try:
                        r = subprocess.run(
                            [_ollama_bin, "run", model_cmd, text[:2000]],
                            capture_output=True, text=True, timeout=60,
                            encoding="utf-8", errors="replace",
                        )
                        if r.returncode == 0 and r.stdout.strip():
                            _emit(emit, r.stdout.strip()[:3000])
                            actions.append({"name": "ollama_direct", "detail": f"{model_cmd} answered"})
                            _user_answered = True
                            break
                    except (subprocess.TimeoutExpired, FileNotFoundError):
                        continue
            except Exception:
                pass

        if not _user_answered:
            _emit(
                emit,
                "抱歉，当前所有AI大脑都不可用（云端API无额度、本地Ollama未响应、Claude CLI不可用）。"
                "请启动Ollama (ollama serve) 或充值API后再试。",
            )

    # ── PRIORITY 2: Silent maintenance (user never sees this) ────────
    ps1, sh = emit_rebuild_venv_scripts(workspace)
    actions.append(
        {
            "name": "self_heal",
            "detail": f"rebuild scripts emitted: {ps1.name}, {sh.name}",
        }
    )
    _emit(emit, f"[离线动作] 已生成环境自愈脚本: {ps1.name}, {sh.name}")

    pair = _pick_synthesis_pair(registry, text)
    if pair:
        synth_path, synth_msg = synthesize_two_skills(workspace, pair[0], pair[1], out_skill_name="survival_hybrid")
        actions.append({"name": "skill_synthesis", "detail": f"{synth_msg}: {synth_path}"})
        _emit(emit, f"[离线动作] 已融合技能 {pair[0]} + {pair[1]} -> {synth_path.name}")

    need_bundle = state.value == "DEGRADED" or _should(text, "colab", "重型", "offload", "bundle")
    if need_bundle:
        bundle = build_colab_job_bundle(
            workspace,
            relative_paths=_default_bundle_paths(workspace),
            instruction="Offline brain degraded-mode offload bundle.",
        )
        actions.append({"name": "colab_bundle", "detail": str(bundle)})
        _emit(emit, f"[离线动作] 已生成离线算力迁移包: {bundle.name}")

    if snapshot["quota"]["rate_like_events_1h"] > 0 or _should(text, "429", "proxy", "代理", "限流"):
        rotated = rotate_proxy_index(workspace)
        actions.append({"name": "proxy_rotate", "detail": json.dumps(rotated, ensure_ascii=False)})
        _emit(emit, f"[离线动作] 代理轮换结果: {json.dumps(rotated, ensure_ascii=False)}")

    if _research_request(text):
        research = run_public_research_scout(workspace, text, emit=emit)
        actions.append(
            {
                "name": "research_scout",
                "detail": f"{research.markdown_path} ({len(research.papers)} papers, {len(research.factors)} factors)",
            }
        )
        _emit(
            emit,
            f"[离线研究] 已输出论文/因子报告: {research.markdown_path.name}；"
            f"候选因子 {len(research.factors)} 个。",
        )

    proposal = write_treasury_proposal_stub(
        workspace,
        reason=failure_reason or reason or "offline_brain_request",
        suggested_actions=[
            "Keep OKX/API signing disabled until explicit human approval.",
            "Use paper trading, backtests, and read-only probes first.",
            "Restore cloud billing or keep parasite/local mode.",
        ],
    )
    actions.append({"name": "treasury_proposal", "detail": str(proposal)})
    _emit(emit, f"[离线动作] 已写入 treasury proposal: {proposal.name}")

    if _should(text, "赚钱", "treasury", "fund", "survival", "proposal", "收益", "revenue"):
        revenue = write_survival_revenue_plan_stub(
            workspace,
            reason=failure_reason or reason or "offline_brain_request",
            okx_enabled=False,
        )
        actions.append({"name": "revenue_plan", "detail": str(revenue)})
        _emit(emit, f"[离线动作] 已写入只读收益计划: {revenue.name}")

    if _should(text, "nomad", "snapshot", "迁移", "灵魂转移", "漂移"):
        nomad = write_nomad_snapshot(workspace, extra={"source": "offline_brain", "instruction": text[:500]})
        actions.append({"name": "nomad_snapshot", "detail": str(nomad)})
        _emit(emit, f"[离线动作] 已写入 nomad snapshot: {nomad.name}")

    episode = append_reasoning_episode(
        workspace,
        trigger="offline_brain",
        hypothesis=(
            f"用户请求: {text[:160] or '(empty)'}；"
            f"当前系统处于 {state.value}，需优先保证生存/修复/迁移闭环，再等待云端大脑恢复。"
        ),
        verify_plan=[
            "检查 .claw/ 下是否生成 parasite/outbox/treasury/self-test 等产物",
            "确认代理状态、Colab bundle、自愈脚本、skill synthesis 是否完整",
            "若云端模型恢复，再让 DevClaw 进入完整工具循环处理更复杂代码改造",
        ],
        revise_hint="若当前离线动作不足以完成任务，恢复 Ollama/OpenAI 后继续执行生产级修复。",
    )
    actions.append({"name": "reasoning_episode", "detail": str(episode)})

    try:
        append_memory(
            workspace,
            "lesson",
            f"[offline_brain] state={state.value} failure_reason={failure_reason or reason} actions="
            + ", ".join(a["name"] for a in actions),
        )
    except Exception:
        pass

    lines = [
        "离线降级脑已完成一轮自治动作。",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"状态: {state.value}",
        f"原因: {failure_reason or reason}",
        "动作:",
    ]
    lines.extend(f"- {item['name']}: {item['detail']}" for item in actions)
    summary = "\n".join(lines)
    return OfflineBrainResult(summary=summary, actions=actions)
