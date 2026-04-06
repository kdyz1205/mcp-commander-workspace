"""
Sleep-cycle digest: turn failures into logic-backed skill drafts with test gating.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def evolution_log_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "evolution_failures.jsonl"


def append_evolution_failure(workspace: Path, *, kind: str, detail: str) -> None:
    p = evolution_log_path(workspace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": time.time(), "kind": kind, "detail": (detail or "")[:4000]},
            ensure_ascii=False,
        )
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_tail_jsonl(path: Path, max_lines: int = 200) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def task_plan_reasoning_excerpt(workspace: Path, *, max_chars: int = 3500) -> str:
    p = Path(workspace).resolve() / "task_plan.md"
    if not p.is_file():
        return "(no task_plan.md)"
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return "(task_plan.md unreadable)"
    raw = raw.strip()
    if not raw:
        return "(task_plan.md empty)"
    lowered = raw.lower()
    idx = lowered.rfind("[现状]")
    if idx == -1:
        idx = lowered.rfind("## ")
    chunk = raw[idx:] if idx >= 0 else raw
    if len(chunk) > max_chars:
        chunk = "...\n" + chunk[-max_chars:]
    return chunk


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class RetrievedLogic:
    summary: str
    rationale: str
    root_causes: list[str]
    next_steps: list[str]
    failure_bullets: list[str]


class LogicRetriever:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()

    def retrieve(self, failures: list[dict[str, Any]]) -> RetrievedLogic:
        plan_excerpt = task_plan_reasoning_excerpt(self.workspace, max_chars=2200)
        buckets: dict[str, int] = {
            "quota_or_billing": 0,
            "env_or_dependency": 0,
            "network_or_proxy": 0,
            "logic_or_tests": 0,
            "other": 0,
        }
        bullets: list[str] = []
        for failure in failures[-20:]:
            detail = str(failure.get("detail") or "")
            kind = str(failure.get("kind") or "unknown")
            lowered = f"{kind}\n{detail}".lower()
            if any(x in lowered for x in ("quota", "billing", "insufficient_quota")):
                buckets["quota_or_billing"] += 1
            elif any(x in lowered for x in ("proxy", "network", "timeout", "429", "connection")):
                buckets["network_or_proxy"] += 1
            elif any(x in lowered for x in ("import", "module", "venv", "dependency", "package", "python")):
                buckets["env_or_dependency"] += 1
            elif any(x in lowered for x in ("assert", "test", "traceback", "logic", "unexpected")):
                buckets["logic_or_tests"] += 1
            else:
                buckets["other"] += 1
            bullets.append(f"- ({kind}) {detail[:180]}")

        sorted_causes = sorted(buckets.items(), key=lambda item: item[1], reverse=True)
        root_causes = [name for name, count in sorted_causes if count > 0][:3] or ["other"]
        rationale = (
            "I am changing the skill surface because recent failures cluster around "
            + ", ".join(root_causes)
            + ". The update should preserve the reasoning trace from task_plan.md, "
            "add a minimal executable runner, and require tests before promotion."
        )
        next_steps = [
            "Preserve the top failure cluster as the main repair target.",
            "Materialize a skill package with rationale plus a deterministic runner.",
            "Run pytest on the generated skill before any promotion step.",
        ]
        summary = "Top failure clusters: " + ", ".join(f"{name}={count}" for name, count in sorted_causes if count > 0)
        if plan_excerpt and plan_excerpt != "(no task_plan.md)":
            rationale += "\n\nTask plan excerpt:\n" + plan_excerpt[:1200]
        return RetrievedLogic(
            summary=summary,
            rationale=rationale,
            root_causes=root_causes,
            next_steps=next_steps,
            failure_bullets=bullets or ["- (none)"],
        )


def _python_launchers() -> list[list[str]]:
    launchers: list[list[str]] = []
    if os.name == "nt":
        launchers.append(["py", "-m"])
    launchers.append([sys.executable, "-m"])
    return launchers


def run_skill_pytest_gate(workspace: Path, test_target: Path) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    test_target = Path(test_target).resolve()
    timeout_sec = int(os.environ.get("AUTO_EVOLVE_PYTEST_TIMEOUT_SEC", "600") or "600")
    for launcher in _python_launchers():
        cmd = launcher + ["pytest", str(test_target), "-q"]
        try:
            cp = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except FileNotFoundError:
            continue
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "command": cmd, "error": str(exc)}
        return {
            "ok": cp.returncode == 0,
            "command": cmd,
            "returncode": cp.returncode,
            "stdout": (cp.stdout or "")[:3000],
            "stderr": (cp.stderr or "")[:3000],
        }
    return {"ok": False, "error": "pytest unavailable"}


def maybe_promote_draft_skill(workspace: Path, draft_skill_dir: Path, *, gate_result: dict[str, Any]) -> Path | None:
    if not _env_truthy("AUTO_EVOLVE_PYTEST_PROMOTE"):
        return None
    if not gate_result.get("ok"):
        return None

    ws = Path(workspace).resolve()
    draft_skill_dir = Path(draft_skill_dir).resolve()
    src = draft_skill_dir / "SKILL.md"
    if not src.is_file():
        return None

    from claw_runtime.survival_engine import SurvivalEngine, SurvivalState

    eng = SurvivalEngine(ws)
    if eng.assess_survival_state()[0] != SurvivalState.HEALTHY:
        return None

    target = os.environ.get("AUTO_EVOLVE_PROMOTE_TARGET", "trading_evolved").strip() or "trading_evolved"
    dest_dir = ws / "skills" / target
    dest = dest_dir / "SKILL.md"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest.is_file():
            bak = dest_dir / f"SKILL.md.bak.{int(time.time())}"
            shutil.copy2(dest, bak)
        shutil.copy2(src, dest)
    except OSError:
        return None
    return dest


def _format_causes(causes: list[str]) -> str:
    return "".join(f"- {c}\n" for c in causes)


def _write_generated_skill_files(
    workspace: Path,
    skill_dir: Path,
    *,
    name: str,
    logic: RetrievedLogic,
) -> tuple[Path, Path]:
    body = (
        f"---\nname: {name}\n"
        "description: Logic-backed evolution draft from recent failures; review before trusting.\n"
        'metadata: {"openclaw":{"always":false}}\n'
        "---\n\n"
        "## Evolution Summary\n\n"
        f"{logic.summary}\n\n"
        "## Why This Skill Exists\n\n"
        f"{logic.rationale}\n\n"
        "## Recent Pain Signals\n\n"
        + "\n".join(logic.failure_bullets)
        + "\n\n## Next Agent Steps\n\n"
        + "\n".join(f"- {step}" for step in logic.next_steps)
        + "\n"
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(body, encoding="utf-8")

    rationale_md = skill_dir / "RATIONALE.md"
    rationale_md.write_text(
        textwrap.dedent(
            f"""\
            # Logic Retriever Output

            {logic.summary}

            ## Root Causes

            {_format_causes(logic.root_causes)}

            ## Rationale

            {logic.rationale}
            """
        ),
        encoding="utf-8",
    )

    runner_py = skill_dir / "runner.py"
    runner_py.write_text(
        textwrap.dedent(
            f'''\
            from __future__ import annotations

            import json


            ROOT_CAUSES = {logic.root_causes!r}
            NEXT_STEPS = {logic.next_steps!r}
            SUMMARY = {logic.summary!r}


            def run(task: str) -> str:
                return json.dumps(
                    {{
                        "skill": "{name}",
                        "summary": SUMMARY,
                        "root_causes": ROOT_CAUSES,
                        "next_steps": NEXT_STEPS,
                        "task_preview": (task[:200] or "(empty)"),
                    }},
                    ensure_ascii=False,
                )
            '''
        ),
        encoding="utf-8",
    )

    tests_dir = skill_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_py = tests_dir / f"test_{name}.py"
    test_py.write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations

            import json

            from skills.{name}.runner import run


            def test_{name}_runner_contains_logic() -> None:
                payload = json.loads(run("repair task"))
                assert payload["skill"] == "{name}"
                assert payload["root_causes"]
                assert payload["next_steps"]
            """
        ),
        encoding="utf-8",
    )
    return skill_md, test_py


def materialize_draft_skill(workspace: Path, *, max_lines: int = 80) -> Path | None:
    workspace = Path(workspace).resolve()
    failures = _read_tail_jsonl(evolution_log_path(workspace), max_lines=max_lines)
    if not failures:
        return None

    logic = LogicRetriever(workspace).retrieve(failures)
    name = f"auto_evolve_{int(time.time())}"
    skill_dir = workspace / "skills" / name
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    out, test_py = _write_generated_skill_files(workspace, skill_dir, name=name, logic=logic)
    gate_result = run_skill_pytest_gate(workspace, test_py)
    gate_path = skill_dir / "pytest_gate.json"
    gate_path.write_text(json.dumps(gate_result, indent=2, ensure_ascii=False), encoding="utf-8")

    append_evolution_failure(
        workspace,
        kind="logic_retrieved",
        detail=f"{logic.summary}; gate_ok={gate_result.get('ok')}",
    )

    promoted = maybe_promote_draft_skill(workspace, skill_dir, gate_result=gate_result)
    if promoted is not None:
        append_evolution_failure(
            workspace,
            kind="skill_promoted",
            detail=f"Draft {name} promoted to {promoted} after pytest gate.",
        )

    return out


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Nightly evolution: draft skill from failure log")
    p.add_argument(
        "--workspace",
        default=os.environ.get("DEVCLAW_WORKSPACE", "."),
        help="Workspace root",
    )
    args = p.parse_args()
    ws = Path(args.workspace).resolve()
    path = materialize_draft_skill(ws)
    if path:
        print(path)
        return 0
    print("No failures logged; nothing to materialize.", file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
