"""
Meta-driving: periodic autonomous reasoning with cost-aware execution planning.

This loop stays within repository safety boundaries:
- no automated top-up APIs
- no unattended financial execution
- no wallet spend
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from claw_runtime.bot_task_queue import enqueue_persisted_task, persisted_queue_depth, pop_persisted_tasks
from claw_runtime.memory import append_memory
from claw_runtime.reasoning_episode import append_reasoning_episode, maybe_auto_reasoning_stub
from claw_runtime.survival_engine import SurvivalEngine, SurvivalState


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def _append_tick_log(workspace: Path, payload: dict[str, Any]) -> None:
    log = workspace / ".claw" / "meta_tick_log.jsonl"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _task_intent_from_env() -> str:
    return (
        os.environ.get("META_TASK_INTENT", "").strip()
        or "Periodically inspect survival health and choose the cheapest safe execution lane."
    )


@dataclass
class ReasoningDecision:
    lane: str
    complexity: str
    complexity_score: int
    rationale: str
    verify_plan: list[str]
    should_rotate_proxy: bool
    should_offload: bool
    should_prepare_funding_review: bool
    should_prefer_local: bool


class ReasoningOptimizer:
    def __init__(self, workspace: Path, engine: SurvivalEngine) -> None:
        self.workspace = Path(workspace).resolve()
        self.engine = engine

    def _complexity_score(self, task_intent: str) -> tuple[int, str]:
        text = task_intent.lower()
        score = 1
        if any(x in text for x in ("format", "lint", "readme", "docs", "single file", "rename")):
            score += 1
        if any(x in text for x in ("refactor", "cross-file", "architecture", "multi-agent", "integration", "migration")):
            score += 3
        if len(task_intent) > 180:
            score += 1
        if any(x in text for x in ("trading", "strategy", "evolution", "nomad", "survival")):
            score += 1
        if score <= 2:
            return score, "simple"
        if score <= 4:
            return score, "moderate"
        return score, "complex"

    def decide(self, *, task_intent: str, snapshot: dict[str, Any]) -> ReasoningDecision:
        state = str(snapshot.get("state") or "HEALTHY")
        quota = snapshot.get("quota", {})
        vitals = snapshot.get("vitals", {})
        score, complexity = self._complexity_score(task_intent)
        rate_pain = int(quota.get("rate_like_events_1h", 0) or 0)
        quota_pain = int(quota.get("insufficient_quota_events_24h", 0) or 0)
        mem = float(vitals.get("mem_percent") or 0.0)

        should_rotate_proxy = rate_pain >= 1 and bool(os.environ.get("PROXY_LIST_FILE", "").strip())
        should_prepare_funding_review = quota_pain > 0 or bool(quota.get("soft_budget_exhausted"))
        should_offload = state == SurvivalState.DEGRADED.value and score >= 4
        should_prefer_local = (
            state == SurvivalState.CRITICAL.value
            or complexity == "simple"
            or quota_pain > 0
            or not bool(quota.get("openai_key_configured"))
        )

        if state == SurvivalState.CRITICAL.value:
            lane = "critical_survival"
        elif should_offload:
            lane = "degraded_offload"
        elif should_prefer_local:
            lane = "local_parasite"
        elif complexity == "complex":
            lane = "cloud_limited"
        else:
            lane = "hybrid_balanced"

        rationale = (
            f"Task intent='{task_intent[:160]}'; complexity={complexity}({score}); "
            f"state={state}; quota_pain={quota_pain}; rate_pain={rate_pain}; mem={mem:.1f}%. "
        )
        if lane == "local_parasite":
            rationale += "Choose local/parasite because the task is cheap enough or cloud survival is constrained."
        elif lane == "cloud_limited":
            rationale += "Choose cloud_limited because the task spans multiple files and current vitals are stable."
        elif lane == "degraded_offload":
            rationale += "Choose degraded_offload because host resources are tight while the task is complex."
        elif lane == "critical_survival":
            rationale += "Choose critical_survival because preserving continuity is more important than feature throughput."
        else:
            rationale += "Choose hybrid_balanced because no single lane dominates on cost or complexity."

        verify_plan = [
            "Check survival snapshot before and after the chosen lane.",
            "Leave a task_plan reasoning trace with cost and risk trade-offs.",
            "Prefer read-only or simulation-first actions when finances or external services are involved.",
        ]
        return ReasoningDecision(
            lane=lane,
            complexity=complexity,
            complexity_score=score,
            rationale=rationale,
            verify_plan=verify_plan,
            should_rotate_proxy=should_rotate_proxy,
            should_offload=should_offload,
            should_prepare_funding_review=should_prepare_funding_review,
            should_prefer_local=should_prefer_local,
        )

    def persist_reasoning(self, *, task_intent: str, decision: ReasoningDecision) -> Path:
        return append_reasoning_episode(
            self.workspace,
            trigger=f"meta_tick:{decision.lane}",
            hypothesis=decision.rationale,
            verify_plan=decision.verify_plan + [f"Execution lane chosen: {decision.lane}"],
            revise_hint=(
                "If the lane underperforms, switch to a cheaper path, rotate proxy if rate pain persists, "
                "or materialize a new skill via nightly evolution."
            ),
        )


def autonomous_tick(workspace: Path | str, task_intent: str | None = None) -> dict[str, Any]:
    ws = Path(workspace).resolve()
    actions: list[dict[str, Any]] = []

    # ── Queue-driven self-dispatch: prioritize atom tasks from the queue ──
    queue_depth = persisted_queue_depth(ws)
    if queue_depth > 0 and _env_bool("META_QUEUE_AUTO_DISPATCH", default=True):
        pending = pop_persisted_tasks(ws, max_n=1)
        for task_rec in pending:
            if task_rec.get("kind") == "atom_task":
                meta = task_rec.get("meta", {})
                phase = meta.get("phase", "?")
                total = meta.get("total_phases", "?")
                title = meta.get("title", "unknown")
                task_text = task_rec.get("text", "")
                actions.append({
                    "name": "queue_auto_dispatch",
                    "detail": f"Phase {phase}/{total}: {title}",
                    "task_text": task_text[:500],
                })
                # Override task_intent with the queued atom task
                task_intent = task_text[:500]
                append_memory(
                    ws,
                    "decision",
                    f"[meta_tick] 自动从工作队列取出原子任务: 阶段 {phase}/{total} — {title}",
                )
            else:
                # Not an atom task — re-enqueue so it isn't lost
                enqueue_persisted_task(
                    ws,
                    kind=task_rec.get("kind", "unknown"),
                    text=task_rec.get("text", ""),
                    meta=task_rec.get("meta"),
                )

    eng = SurvivalEngine(ws)
    eng.heartbeat()
    fund_note = eng.autonomous_fund_check()
    if fund_note:
        actions.append({"name": "fund_low_persisted_task", "detail": fund_note[:240]})

    state, reason = eng.assess_survival_state()
    q = eng.check_quota()
    snap = eng.snapshot()
    task_intent = task_intent or _task_intent_from_env()
    optimizer = ReasoningOptimizer(ws, eng)
    decision = optimizer.decide(task_intent=task_intent, snapshot=snap)
    reasoning_path = optimizer.persist_reasoning(task_intent=task_intent, decision=decision)
    actions.append({"name": "reasoning_optimizer", "detail": asdict(decision)})
    actions.append({"name": "reasoning_trace", "detail": str(reasoning_path)})

    debounce = float(os.environ.get("META_REFLEX_DEBOUNCE_SEC", "120") or "120")

    if state == SurvivalState.CRITICAL:
        try:
            from claw_runtime.survival_reflex import run_critical_reflex

            if run_critical_reflex(ws, eng, debounce_sec=debounce, notify=None):
                actions.append({"name": "critical_reflex", "detail": "CURSOR_OUTBOX + parasite + treasury stub"})
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "critical_reflex_error", "detail": str(e)[:500]})

    colab_degraded = (
        state == SurvivalState.DEGRADED
        and _env_bool("META_COLAB_ON_DEGRADED", default=True)
        and (_env_bool("META_COLAB_ANY_DEGRADED", default=True) or decision.should_offload)
    )
    if colab_degraded:
        paths = [p.strip() for p in os.environ.get("META_COLAB_PATHS", "task_plan.md").split(",") if p.strip()]
        try:
            from claw_runtime.ultimate.colab_bundle import build_colab_job_bundle

            z = build_colab_job_bundle(
                ws,
                relative_paths=paths,
                instruction=os.environ.get(
                    "META_COLAB_INSTRUCTION",
                    "Host DEGRADED — Colab offload bundle (manual upload; no automated Google login).",
                ),
            )
            actions.append({"name": "colab_bundle", "detail": str(z)})
            append_memory(ws, "lesson", f"[meta_tick] Offload bundle {z} because {decision.rationale}")
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "colab_bundle_error", "detail": str(e)[:500]})

    if state == SurvivalState.DEGRADED and _env_bool("META_SELF_HEAL_EMIT_ON_DEGRADED", default=True):
        sh_last = ws / ".claw" / "meta_last_self_heal_emit_ts"
        now_sh = time.time()
        sh_gap = float(os.environ.get("META_SELF_HEAL_MIN_SEC", "7200") or "7200")
        try:
            last_sh = float(sh_last.read_text(encoding="utf-8").strip()) if sh_last.is_file() else 0.0
        except ValueError:
            last_sh = 0.0
        if now_sh - last_sh >= sh_gap:
            try:
                from claw_runtime.ultimate.self_heal import emit_rebuild_venv_scripts

                ps1, sh = emit_rebuild_venv_scripts(ws)
                actions.append({"name": "self_heal_emit", "detail": f"{ps1.name}; {sh.name}"})
                sh_last.write_text(str(now_sh), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                actions.append({"name": "self_heal_emit_error", "detail": str(e)[:500]})

    if _env_bool("META_REASONING_EPISODE", default=True):
        try:
            rp = maybe_auto_reasoning_stub(ws, state_value=state.value, reason=reason, snapshot=snap)
            if rp:
                actions.append({"name": "reasoning_episode", "detail": str(rp)})
                append_memory(
                    ws,
                    "lesson",
                    f"[meta_tick] Appended reasoning episode to task_plan.md ({state.value}, fails={snap.get('consecutive_failures', 0)}).",
                )
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "reasoning_episode_error", "detail": str(e)[:500]})

    if decision.should_rotate_proxy and _env_bool("META_PROXY_ROTATE_ON_NET_PAIN", default=True):
        try:
            from claw_runtime.ultimate.proxy_env import rotate_proxy_index

            r = rotate_proxy_index(ws)
            actions.append({"name": "proxy_rotate", "detail": r})
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "proxy_rotate_error", "detail": str(e)[:500]})

    if int(q.get("insufficient_quota_events_24h", 0) or 0) > 0:
        try:
            from claw_runtime.ultimate.treasury import write_chain_action_draft, write_treasury_proposal_stub

            write_treasury_proposal_stub(
                ws,
                reason="insufficient_quota",
                suggested_actions=[
                    "Fix OpenAI billing or use parasite / Ollama",
                    "Human-reviewed trading research only",
                    "Never auto-call top-up APIs from this tick",
                ],
            )
            actions.append({"name": "treasury_proposal", "detail": ".claw/treasury_proposal.json"})
            if _env_bool("META_CHAIN_DRAFT_ON_QUOTA", default=True):
                cd = write_chain_action_draft(ws, reason="insufficient_quota — optional treasury top-up path")
                actions.append({"name": "chain_action_draft", "detail": str(cd)})
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "treasury_proposal_error", "detail": str(e)[:500]})

    if decision.should_prepare_funding_review:
        try:
            from claw_runtime.ultimate.treasury import prepare_self_funding_review, write_survival_revenue_plan_stub

            review = prepare_self_funding_review(ws, reason=decision.rationale)
            plan = write_survival_revenue_plan_stub(ws, reason=decision.rationale, okx_enabled=False)
            actions.append({"name": "self_funding_review", "detail": str(review)})
            actions.append({"name": "revenue_plan", "detail": str(plan)})
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "self_funding_review_error", "detail": str(e)[:500]})

    if _env_bool("META_OBSERVE_FUNDING", default=True):
        script = ws / "tools" / "trading_funding_binance.py"
        if script.is_file() and decision.lane in {"cloud_limited", "hybrid_balanced"}:
            try:
                sym = os.environ.get("META_FUNDING_SYMBOL", "BTCUSDT").strip() or "BTCUSDT"
                cp = subprocess.run(
                    [sys.executable, str(script), sym],
                    cwd=str(ws),
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                actions.append(
                    {
                        "name": "funding_observe",
                        "detail": (cp.stdout or cp.stderr or "")[:1500],
                        "returncode": cp.returncode,
                    }
                )
            except Exception as e:  # noqa: BLE001
                actions.append({"name": "funding_observe_error", "detail": str(e)[:500]})

    if _env_bool("META_SYNTHESIS_ON_TICK", default=True):
        syn_last = ws / ".claw" / "meta_last_synthesis_ts"
        now_syn = time.time()
        syn_gap = float(os.environ.get("META_SYNTHESIS_MIN_SEC", "86400") or "86400")
        try:
            last_syn = float(syn_last.read_text(encoding="utf-8").strip()) if syn_last.is_file() else 0.0
        except ValueError:
            last_syn = 0.0
        if now_syn - last_syn >= syn_gap:
            try:
                from claw_runtime.skill_registry import SkillRegistry
                from claw_runtime.ultimate.skill_synthesis import synthesize_two_skills

                reg = SkillRegistry(ws)
                names = sorted(reg.refresh().keys())
                pair_raw = os.environ.get("META_SYNTHESIS_PAIR", "").strip()
                if pair_raw and "," in pair_raw:
                    a, b = [x.strip() for x in pair_raw.split(",", 1)]
                elif len(names) >= 2:
                    a, b = names[0], names[1]
                else:
                    a, b = "", ""
                if a and b and a != b:
                    out_dir, _note = synthesize_two_skills(ws, a, b)
                    actions.append({"name": "skill_synthesis", "detail": str(out_dir)})
                    syn_last.write_text(str(now_syn), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                actions.append({"name": "skill_synthesis_error", "detail": str(e)[:500]})

    if _env_bool("META_EVOLVE_ON_TICK", default=False):
        last_path = ws / ".claw" / "meta_last_evolve_ts"
        now = time.time()
        min_gap = float(os.environ.get("META_EVOLVE_MIN_SEC", "3600") or "3600")
        try:
            last = float(last_path.read_text(encoding="utf-8").strip()) if last_path.is_file() else 0.0
        except ValueError:
            last = 0.0
        if now - last >= min_gap:
            ev = ws / ".claw" / "evolution_failures.jsonl"
            if ev.is_file() and ev.stat().st_size > 0:
                try:
                    from claw_runtime.nightly_evolution import materialize_draft_skill

                    out = materialize_draft_skill(ws)
                    if out:
                        actions.append({"name": "evolve_draft", "detail": str(out)})
                        last_path.write_text(str(now), encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    actions.append({"name": "evolve_draft_error", "detail": str(e)[:500]})

    # ── Report remaining queue depth ──
    remaining_queue = persisted_queue_depth(ws)
    if remaining_queue > 0:
        actions.append({"name": "queue_status", "detail": f"{remaining_queue} atom tasks remaining in queue"})

    out: dict[str, Any] = {
        "ts": time.time(),
        "task_intent": task_intent,
        "state": state.value,
        "reason": reason,
        "snapshot": snap,
        "decision": asdict(decision),
        "actions": actions,
        "queue_depth": remaining_queue,
    }
    _append_tick_log(ws, out)
    return out


def run_autonomous_loop(workspace: Path | str, interval_sec: float) -> None:
    ws = Path(workspace).resolve()
    interval_sec = max(15.0, float(interval_sec))
    print(f"[meta-driving] workspace={ws} interval={interval_sec}s (Ctrl+C to stop)")
    while True:
        try:
            summary = autonomous_tick(ws)
            print(json.dumps({k: summary[k] for k in ("state", "decision", "actions")}, ensure_ascii=False, indent=2))
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[meta-driving] tick error: {e!s}")
        time.sleep(interval_sec)
