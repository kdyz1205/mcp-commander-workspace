"""
Evolution Scheduler — Controlled self-improvement for DevClaw.

Four evolution types, strict gate pipeline:
  Type A: Skill Evolution      — from failures, generate new skills
  Type B: Prompt/Recipe        — optimize system prompts, planning recipes
  Type C: Tool Adapter         — new desktop/browser/CLI adapters
  Type D: Runtime Refactor     — most dangerous, strictest gates

Pipeline:
  failure clustering -> candidate drafting -> sandbox materialization
  -> verification -> promotion or rejection -> persist lesson
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("devclaw.evolution_scheduler")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EvolutionCandidate:
    candidate_id: str
    evolution_type: str  # "skill" | "prompt" | "adapter" | "refactor"
    description: str
    source_failures: list[str] = field(default_factory=list)
    draft_path: str = ""
    target_path: str = ""
    risk_level: str = "low"  # "low" | "medium" | "high"
    created_at: float = 0.0
    files: list[str] = field(default_factory=list)
    diff: str = ""


@dataclass
class EvolutionResult:
    candidate_id: str
    status: str  # "promoted" | "rejected" | "failed_test" | "failed_gate" | "error"
    tests_passed: bool = False
    gate_approved: bool = False
    lesson: str = ""
    duration_sec: float = 0.0


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: str = "0") -> bool:
    return os.environ.get(key, default).strip().lower() in ("1", "true", "yes")


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# EvolutionScheduler
# ---------------------------------------------------------------------------

class EvolutionScheduler:
    """Manages the full evolution pipeline with safety gates."""

    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve()
        self.claw_dir = self.workspace / ".claw"
        self.drafts_dir = self.claw_dir / "evolution_drafts"
        self.state_file = self.claw_dir / "evolution_scheduler_state.json"
        self.lessons_file = self.claw_dir / "evolution_lessons.jsonl"
        self.failures_file = self.claw_dir / "evolution_failures.jsonl"

        self.claw_dir.mkdir(parents=True, exist_ok=True)
        self.drafts_dir.mkdir(parents=True, exist_ok=True)

        self._state = self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"last_run_ts": 0, "runs_today": 0, "today_date": "", "history": []}

    def _save_state(self) -> None:
        self.state_file.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Gate checks
    # ------------------------------------------------------------------

    def can_evolve(self) -> tuple[bool, str]:
        """Check all preconditions for evolution."""
        # Env gate
        if not _env_bool("DEVCLAW_ALLOW_AUTONOMOUS_EVOLUTION", "0"):
            return False, "DEVCLAW_ALLOW_AUTONOMOUS_EVOLUTION is disabled"

        # Health gate
        if _env_bool("DEVCLAW_EVOLVE_ONLY_WHEN_HEALTHY", "1"):
            try:
                from claw_runtime.survival_engine import SurvivalEngine, SurvivalState
                se = SurvivalEngine(self.workspace)
                state = se.assess_survival_state()
                if state.health != "HEALTHY":
                    return False, f"System health is {state.health}, not HEALTHY"
            except Exception as exc:
                log.warning("Cannot check health: %s", exc)

        # Budget gate
        try:
            from claw_runtime.budget_manager import BudgetManager
            bm = BudgetManager(str(self.claw_dir))
            ok, reason = bm.can_evolve()
            if not ok:
                return False, f"Budget gate: {reason}"
        except Exception as exc:
            log.warning("Cannot check budget: %s", exc)

        # Interval gate
        min_interval = _env_float("DEVCLAW_EVOLVE_MIN_INTERVAL_SEC", 3600)
        elapsed = time.time() - self._state.get("last_run_ts", 0)
        if elapsed < min_interval:
            return False, f"Too soon: {elapsed:.0f}s since last run (min {min_interval:.0f}s)"

        # Daily run cap
        max_daily = _env_int("DEVCLAW_EVOLVE_MAX_RUNS_PER_DAY", 3)
        today = time.strftime("%Y-%m-%d")
        if self._state.get("today_date") != today:
            self._state["today_date"] = today
            self._state["runs_today"] = 0
        if self._state["runs_today"] >= max_daily:
            return False, f"Daily cap reached: {self._state['runs_today']}/{max_daily}"

        return True, "All gates passed"

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run_evolution_cycle(self) -> list[EvolutionResult]:
        """Run one full evolution cycle."""
        ok, reason = self.can_evolve()
        if not ok:
            log.info("[evolution] Blocked: %s", reason)
            return []

        results: list[EvolutionResult] = []
        t0 = time.time()

        # Step 1: Cluster failures
        clusters = self.cluster_failures()
        if not clusters:
            log.info("[evolution] No failure clusters found")
            return []

        # Step 2: Draft candidates
        candidates = self.draft_candidates(clusters)
        if not candidates:
            log.info("[evolution] No candidates drafted")
            return []

        for candidate in candidates[:3]:  # max 3 per cycle
            ct0 = time.time()
            result = EvolutionResult(candidate_id=candidate.candidate_id)

            try:
                # Step 3+4: Test in sandbox
                test_result = self.test_candidate(candidate)
                result.tests_passed = test_result.get("passed", False)

                if not result.tests_passed:
                    result.status = "failed_test"
                    result.lesson = f"Tests failed: {test_result.get('stderr', '')[:500]}"
                    self.reject(candidate, result.lesson)
                else:
                    # Step 5: Promotion gate
                    gate_ok = self.submit_to_gate(candidate, test_result)
                    result.gate_approved = gate_ok

                    if gate_ok:
                        promoted = self.promote(candidate)
                        result.status = "promoted" if promoted else "error"
                        result.lesson = "Promoted successfully" if promoted else "Promotion copy failed"
                    else:
                        result.status = "failed_gate"
                        result.lesson = "Promotion gate rejected"
                        self.reject(candidate, result.lesson)

            except Exception as exc:
                result.status = "error"
                result.lesson = f"Exception: {exc!s}"
                log.error("[evolution] Candidate %s error: %s", candidate.candidate_id, exc)

            result.duration_sec = time.time() - ct0
            results.append(result)

            # Step 6: Persist lesson
            self.record_lesson(candidate, result)

        # Update state
        self._state["last_run_ts"] = time.time()
        self._state["runs_today"] = self._state.get("runs_today", 0) + 1
        self._save_state()

        log.info("[evolution] Cycle complete: %d candidates, %.1fs",
                 len(results), time.time() - t0)
        return results

    # ------------------------------------------------------------------
    # Individual steps
    # ------------------------------------------------------------------

    def cluster_failures(self, limit: int = 100) -> list[dict]:
        """Group recent failures by type."""
        if not self.failures_file.is_file():
            return []

        failures: list[dict] = []
        try:
            for line in self.failures_file.read_text(encoding="utf-8").splitlines()[-limit:]:
                line = line.strip()
                if line:
                    failures.append(json.loads(line))
        except Exception:
            return []

        if not failures:
            return []

        # Cluster by error category
        clusters: dict[str, list[dict]] = {}
        for f in failures:
            cat = f.get("category", f.get("error_type", "unknown"))
            clusters.setdefault(cat, []).append(f)

        return [
            {"category": cat, "count": len(items), "samples": items[:5],
             "first_seen": items[0].get("timestamp", 0),
             "last_seen": items[-1].get("timestamp", 0)}
            for cat, items in sorted(clusters.items(), key=lambda x: -len(x[1]))
            if len(items) >= 2  # only clusters with 2+ failures
        ]

    def draft_candidates(self, clusters: list[dict]) -> list[EvolutionCandidate]:
        """Generate evolution candidates from failure clusters."""
        candidates: list[EvolutionCandidate] = []

        for cluster in clusters[:5]:
            cat = cluster["category"]
            samples = cluster["samples"]
            cid = f"evo_{uuid.uuid4().hex[:8]}"
            draft_dir = self.drafts_dir / cid
            draft_dir.mkdir(parents=True, exist_ok=True)

            # Determine evolution type based on failure category
            if cat in ("import_error", "missing_dependency", "module_not_found"):
                etype = "skill"
                risk = "low"
                desc = f"Create skill to handle: {samples[0].get('error', cat)[:200]}"
            elif cat in ("selector_stale", "browser_error", "network_error"):
                etype = "adapter"
                risk = "medium"
                desc = f"Create adapter for: {cat}"
            elif cat in ("prompt_quality", "planning_error"):
                etype = "prompt"
                risk = "low"
                desc = f"Improve prompt/recipe for: {cat}"
            else:
                etype = "skill"
                risk = "low"
                desc = f"Handle recurring failure: {cat} ({cluster['count']} occurrences)"

            # Try to use existing nightly_evolution to draft
            try:
                from claw_runtime.nightly_evolution import materialize_draft_skill
                draft_out = materialize_draft_skill(self.workspace)
                if draft_out:
                    (draft_dir / "draft_info.txt").write_text(str(draft_out), encoding="utf-8")
            except Exception:
                pass

            # Write cluster info
            (draft_dir / "cluster.json").write_text(
                json.dumps(cluster, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            candidates.append(EvolutionCandidate(
                candidate_id=cid,
                evolution_type=etype,
                description=desc,
                source_failures=[s.get("id", "") for s in samples],
                draft_path=str(draft_dir),
                target_path=str(self.workspace / "skills" / cid) if etype == "skill" else str(draft_dir),
                risk_level=risk,
                created_at=time.time(),
            ))

        return candidates

    def test_candidate(self, candidate: EvolutionCandidate) -> dict:
        """Run tests on a candidate in sandbox."""
        pytest_cmd = os.environ.get("AUTO_EVOLVE_PYTEST_CMD", "pytest")
        draft_path = Path(candidate.draft_path)

        # If there are Python files in the draft, try to syntax-check them
        py_files = list(draft_path.glob("**/*.py"))
        for pf in py_files:
            try:
                compile(pf.read_text(encoding="utf-8"), str(pf), "exec")
            except SyntaxError as e:
                return {"passed": False, "stdout": "", "stderr": f"Syntax error in {pf}: {e}"}

        # Run pytest if available and configured
        if _env_bool("AUTO_EVOLVE_PYTEST_PROMOTE", "0"):
            try:
                r = subprocess.run(
                    pytest_cmd.split(),
                    capture_output=True, text=True, timeout=120,
                    cwd=str(self.workspace),
                )
                return {
                    "passed": r.returncode == 0,
                    "stdout": r.stdout[-2000:],
                    "stderr": r.stderr[-2000:],
                    "exit_code": r.returncode,
                }
            except Exception as exc:
                return {"passed": False, "stdout": "", "stderr": f"Test run failed: {exc}"}

        # If no pytest required, syntax check is sufficient
        return {"passed": True, "stdout": "Syntax check only (pytest not required)", "stderr": ""}

    def submit_to_gate(self, candidate: EvolutionCandidate, test_result: dict) -> bool:
        """Submit candidate to PromotionGate."""
        try:
            from claw_runtime.promotion_gate import PromotionGate, PromotionRequest
            gate = PromotionGate(str(self.workspace))
            request = PromotionRequest(
                request_id=candidate.candidate_id,
                source="evolution",
                description=candidate.description,
                files_changed=candidate.files or [],
                diff=candidate.diff or "",
                has_tests=test_result.get("passed", False),
                test_result=test_result,
                rollback_plan=f"rm -rf {candidate.target_path}" if candidate.evolution_type == "skill" else "git checkout",
            )
            verdict = gate.review(request)
            return verdict.approved
        except Exception as exc:
            log.warning("[evolution] Gate check error: %s — auto-rejecting", exc)
            return False

    def promote(self, candidate: EvolutionCandidate) -> bool:
        """Copy draft to target location."""
        try:
            src = Path(candidate.draft_path)
            dst = Path(candidate.target_path)
            if src.is_dir() and any(src.iterdir()):
                dst.mkdir(parents=True, exist_ok=True)
                for item in src.iterdir():
                    if item.is_file():
                        shutil.copy2(str(item), str(dst / item.name))
                log.info("[evolution] Promoted %s → %s", candidate.candidate_id, dst)
                return True
            return False
        except Exception as exc:
            log.error("[evolution] Promote failed: %s", exc)
            return False

    def reject(self, candidate: EvolutionCandidate, reason: str) -> None:
        """Log rejection."""
        log.info("[evolution] Rejected %s: %s", candidate.candidate_id, reason)

    def record_lesson(self, candidate: EvolutionCandidate, result: EvolutionResult) -> None:
        """Persist lesson learned."""
        lesson = {
            "timestamp": time.time(),
            "candidate_id": candidate.candidate_id,
            "evolution_type": candidate.evolution_type,
            "description": candidate.description,
            "status": result.status,
            "tests_passed": result.tests_passed,
            "gate_approved": result.gate_approved,
            "lesson": result.lesson,
            "duration_sec": result.duration_sec,
            "risk_level": candidate.risk_level,
        }
        with open(self.lessons_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(lesson, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def should_run_now(self) -> bool:
        ok, _ = self.can_evolve()
        return ok

    def get_last_run(self) -> float | None:
        ts = self._state.get("last_run_ts", 0)
        return ts if ts > 0 else None

    def get_run_count_today(self) -> int:
        today = time.strftime("%Y-%m-%d")
        if self._state.get("today_date") != today:
            return 0
        return self._state.get("runs_today", 0)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_history(self, limit: int = 50) -> list[dict]:
        if not self.lessons_file.is_file():
            return []
        lines = self.lessons_file.read_text(encoding="utf-8").splitlines()
        results = []
        for line in lines[-limit:]:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return results

    def get_pending_drafts(self) -> list[EvolutionCandidate]:
        drafts: list[EvolutionCandidate] = []
        for d in self.drafts_dir.iterdir():
            if d.is_dir():
                info_file = d / "cluster.json"
                if info_file.is_file():
                    try:
                        cluster = json.loads(info_file.read_text(encoding="utf-8"))
                        drafts.append(EvolutionCandidate(
                            candidate_id=d.name,
                            evolution_type="skill",
                            description=cluster.get("category", "unknown"),
                            draft_path=str(d),
                            created_at=info_file.stat().st_mtime,
                        ))
                    except Exception:
                        pass
        return sorted(drafts, key=lambda x: x.created_at, reverse=True)
