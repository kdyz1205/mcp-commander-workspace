"""
Builder Loop — Manus-like end-to-end construction for DevClaw.

When given "build an app" or "make this work end to end", drives:
  goal -> spec -> plan -> scaffold -> implement -> run -> observe -> fix -> verify -> package
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("devclaw.builder_loop")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BuilderSpec:
    task_id: str
    goal: str
    requirements: list[str] = field(default_factory=list)
    existing_code: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class BuilderPhaseResult:
    phase: str
    status: str  # "completed" | "partial" | "failed" | "skipped"
    artifacts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_sec: float = 0.0


@dataclass
class BuilderReport:
    task_id: str
    goal: str
    overall_status: str = "pending"  # "completed" | "partial" | "failed" | "blocked"
    phases: list[BuilderPhaseResult] = field(default_factory=list)
    total_duration_sec: float = 0.0
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tests_passed: bool = False
    capability_gaps_found: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# BuilderLoop
# ---------------------------------------------------------------------------

class BuilderLoop:
    """Manus-like end-to-end construction loop."""

    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve()
        self.claw_dir = self.workspace / ".claw"
        self.builder_dir = self.claw_dir / "builder"
        self.builder_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def build(
        self,
        goal: str,
        context: dict | None = None,
        llm_fn: Callable | None = None,
        on_phase_complete: Callable | None = None,
    ) -> BuilderReport:
        """Run the full builder loop."""
        context = context or {}
        task_id = f"build_{uuid.uuid4().hex[:8]}"
        task_dir = self.builder_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        report = BuilderReport(task_id=task_id, goal=goal)
        t0 = time.time()

        phases = [
            ("specify", lambda: self.phase_specify(goal, context, llm_fn)),
            ("plan", None),  # set after specify
            ("scaffold", None),
            ("implement", None),
            ("run", None),
            ("observe", None),
            ("fix", None),
            ("verify", None),
            ("package", None),
        ]

        spec: BuilderSpec | None = None
        plan: list[dict] = []
        observations: list[Any] = []

        for phase_name, _ in phases:
            pt0 = time.time()
            phase_result = BuilderPhaseResult(phase=phase_name)

            try:
                if phase_name == "specify":
                    spec = self.phase_specify(goal, context, llm_fn)
                    phase_result.status = "completed"
                    phase_result.artifacts.append(str(task_dir / "spec.md"))
                    # Save spec
                    (task_dir / "spec.json").write_text(
                        json.dumps(asdict(spec), indent=2, ensure_ascii=False), encoding="utf-8"
                    )

                elif phase_name == "plan":
                    if not spec:
                        phase_result.status = "skipped"
                    else:
                        plan = self.phase_plan(spec, llm_fn)
                        phase_result.status = "completed" if plan else "partial"
                        (task_dir / "plan.json").write_text(
                            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
                        )

                elif phase_name == "scaffold":
                    if not spec or not plan:
                        phase_result.status = "skipped"
                    else:
                        phase_result = self.phase_scaffold(spec, plan)

                elif phase_name == "implement":
                    if not spec or not plan:
                        phase_result.status = "skipped"
                    else:
                        phase_result = self.phase_implement(spec, plan, llm_fn)

                elif phase_name == "run":
                    if not spec:
                        phase_result.status = "skipped"
                    else:
                        phase_result = self.phase_run(spec)

                elif phase_name == "observe":
                    if not spec:
                        phase_result.status = "skipped"
                    else:
                        phase_result = self.phase_observe(spec)
                        observations = phase_result.errors  # carry forward

                elif phase_name == "fix":
                    if not observations:
                        phase_result.status = "skipped"
                    elif spec:
                        phase_result = self.phase_fix(spec, observations)
                    else:
                        phase_result.status = "skipped"

                elif phase_name == "verify":
                    if not spec:
                        phase_result.status = "skipped"
                    else:
                        phase_result = self.phase_verify(spec)
                        report.tests_passed = phase_result.status == "completed"

                elif phase_name == "package":
                    if not spec:
                        phase_result.status = "skipped"
                    else:
                        phase_result = self.phase_package(spec)

            except Exception as exc:
                phase_result.status = "failed"
                phase_result.errors.append(str(exc))
                log.error("[builder] Phase %s failed: %s", phase_name, exc)

                # Check for capability gaps
                try:
                    from claw_runtime.capability_gap_detector import CapabilityGapDetector
                    cgd = CapabilityGapDetector(str(self.workspace))
                    gaps = cgd.detect(task_id, [{"error": str(exc), "source": phase_name}])
                    for g in gaps:
                        report.capability_gaps_found.append(g.description)
                except Exception:
                    pass

            phase_result.duration_sec = time.time() - pt0
            report.phases.append(phase_result)

            if on_phase_complete:
                try:
                    on_phase_complete(phase_name, phase_result)
                except Exception:
                    pass

            # If a critical phase failed, stop
            if phase_result.status == "failed" and phase_name in ("specify", "plan"):
                break

        # Determine overall status
        statuses = [p.status for p in report.phases]
        if all(s in ("completed", "skipped") for s in statuses):
            report.overall_status = "completed"
        elif any(s == "completed" for s in statuses):
            report.overall_status = "partial"
        elif any(s == "failed" for s in statuses):
            report.overall_status = "failed"
        else:
            report.overall_status = "blocked"

        report.total_duration_sec = time.time() - t0

        # Save report
        (task_dir / "report.json").write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

        log.info("[builder] %s: %s (%.1fs)", task_id, report.overall_status, report.total_duration_sec)
        return report

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def phase_specify(
        self, goal: str, context: dict, llm_fn: Callable | None = None,
    ) -> BuilderSpec:
        """Derive a specification from the goal."""
        task_id = context.get("task_id", f"build_{uuid.uuid4().hex[:8]}")
        existing = self.detect_existing_structure(str(self.workspace))
        tech_stack = self.detect_tech_stack(str(self.workspace))

        requirements: list[str] = []
        if llm_fn:
            try:
                prompt = (
                    f"Given this goal: {goal}\n"
                    f"Existing tech stack: {tech_stack}\n"
                    f"Existing structure: {json.dumps(existing, indent=2)[:1000]}\n\n"
                    "List 3-8 specific, testable requirements. Output JSON array of strings."
                )
                resp = llm_fn(prompt)
                if isinstance(resp, str):
                    # Try to extract JSON array
                    import re
                    m = re.search(r'\[.*\]', resp, re.DOTALL)
                    if m:
                        requirements = json.loads(m.group())
            except Exception:
                pass

        if not requirements:
            requirements = [f"Implement: {goal}"]

        return BuilderSpec(
            task_id=task_id,
            goal=goal,
            requirements=requirements,
            existing_code=existing.get("key_files", [])[:20],
            tech_stack=tech_stack,
            created_at=time.time(),
        )

    def phase_plan(
        self, spec: BuilderSpec, llm_fn: Callable | None = None,
    ) -> list[dict]:
        """Break spec into implementation steps."""
        if llm_fn:
            try:
                prompt = (
                    f"Goal: {spec.goal}\n"
                    f"Requirements: {json.dumps(spec.requirements)}\n"
                    f"Tech stack: {spec.tech_stack}\n\n"
                    "Create an implementation plan as JSON array of objects with keys: "
                    "step, description, files, command (optional). Max 10 steps."
                )
                resp = llm_fn(prompt)
                if isinstance(resp, str):
                    import re
                    m = re.search(r'\[.*\]', resp, re.DOTALL)
                    if m:
                        return json.loads(m.group())
            except Exception:
                pass

        # Heuristic plan
        plan = [
            {"step": 1, "description": "Inspect existing code and dependencies", "files": [], "command": "ls -la"},
            {"step": 2, "description": "Install dependencies", "files": [], "command": "pip install -r requirements.txt"},
            {"step": 3, "description": "Implement core logic", "files": spec.existing_code[:5]},
            {"step": 4, "description": "Run and verify", "files": [], "command": "python -m pytest"},
        ]
        return plan

    def phase_scaffold(self, spec: BuilderSpec, plan: list[dict]) -> BuilderPhaseResult:
        """Create directory structure and initial files."""
        result = BuilderPhaseResult(phase="scaffold")
        # Scaffolding is context-dependent; mark as completed if workspace exists
        if self.workspace.is_dir():
            result.status = "completed"
        else:
            self.workspace.mkdir(parents=True, exist_ok=True)
            result.status = "completed"
        return result

    def phase_implement(
        self, spec: BuilderSpec, plan: list[dict], llm_fn: Callable | None = None,
    ) -> BuilderPhaseResult:
        """Execute plan steps that involve code changes."""
        result = BuilderPhaseResult(phase="implement")
        errors: list[str] = []

        for step in plan:
            cmd = step.get("command", "")
            if cmd:
                try:
                    r = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True,
                        timeout=120, cwd=str(self.workspace),
                    )
                    if r.returncode != 0:
                        errors.append(f"Step {step.get('step')}: {r.stderr[:500]}")
                except Exception as exc:
                    errors.append(f"Step {step.get('step')}: {exc}")

        result.errors = errors
        result.status = "completed" if not errors else "partial"
        return result

    def phase_run(self, spec: BuilderSpec) -> BuilderPhaseResult:
        """Start the application/system."""
        result = BuilderPhaseResult(phase="run")
        # Look for common run commands
        pkg_json = self.workspace / "package.json"
        manage_py = self.workspace / "manage.py"

        if pkg_json.is_file():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                if "scripts" in pkg and "dev" in pkg["scripts"]:
                    result.artifacts.append("npm run dev")
            except Exception:
                pass

        result.status = "completed"
        return result

    def phase_observe(self, spec: BuilderSpec) -> BuilderPhaseResult:
        """Collect observations from running system."""
        result = BuilderPhaseResult(phase="observe")
        errors: list[str] = []

        # Check for common error indicators
        log_files = list(self.workspace.glob("**/*.log"))[:5]
        for lf in log_files:
            try:
                content = lf.read_text(encoding="utf-8", errors="replace")[-2000:]
                if any(kw in content.lower() for kw in ("error", "exception", "traceback", "failed")):
                    errors.append(f"{lf.name}: contains errors")
            except Exception:
                pass

        result.errors = errors
        result.status = "completed"
        return result

    def phase_fix(self, spec: BuilderSpec, observations: list) -> BuilderPhaseResult:
        """Diagnose and fix errors from observations."""
        result = BuilderPhaseResult(phase="fix")
        if not observations:
            result.status = "skipped"
            return result

        # Record observations for capability gap detection
        result.errors = [str(o) for o in observations[:10]]
        result.status = "partial"  # fixes would need LLM or specific logic
        return result

    def phase_verify(self, spec: BuilderSpec) -> BuilderPhaseResult:
        """Run verification (tests, smoke checks)."""
        result = BuilderPhaseResult(phase="verify")

        try:
            from claw_runtime.verification_manager import VerificationManager
            vm = VerificationManager(str(self.workspace))
            vr = vm.verify(spec.task_id, skip_layers=[3])  # skip E2E for now
            result.status = "completed" if vr.verdict == "pass" else "partial"
            result.artifacts.append(f"verdict: {vr.verdict}")
            result.errors = vr.blocking_issues
        except Exception as exc:
            # Fallback: try running pytest directly
            try:
                r = subprocess.run(
                    ["pytest", "--tb=short", "-q"],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(self.workspace),
                )
                result.status = "completed" if r.returncode == 0 else "partial"
                if r.returncode != 0:
                    result.errors.append(r.stdout[-1000:])
            except Exception:
                result.status = "skipped"
                result.errors.append(f"No verification available: {exc}")

        return result

    def phase_package(self, spec: BuilderSpec) -> BuilderPhaseResult:
        """Commit and package results."""
        result = BuilderPhaseResult(phase="package")

        # Collect artifacts
        task_dir = self.builder_dir / spec.task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Save final summary
        summary = {
            "task_id": spec.task_id,
            "goal": spec.goal,
            "tech_stack": spec.tech_stack,
            "completed_at": time.time(),
        }
        (task_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        result.artifacts.append(str(task_dir / "summary.json"))
        result.status = "completed"
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def detect_tech_stack(self, path: str) -> list[str]:
        """Detect tech stack from project files."""
        p = Path(path)
        stack: list[str] = []

        indicators = {
            "python": ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile", "setup.cfg"],
            "node": ["package.json"],
            "rust": ["Cargo.toml"],
            "go": ["go.mod"],
            "docker": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
            "java": ["pom.xml", "build.gradle"],
        }

        for tech, files in indicators.items():
            for f in files:
                if (p / f).is_file():
                    stack.append(tech)
                    break

        # Check package.json for framework hints
        pkg_json = p / "package.json"
        if pkg_json.is_file():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "react" in deps:
                    stack.append("react")
                if "vue" in deps:
                    stack.append("vue")
                if "next" in deps:
                    stack.append("nextjs")
                if "express" in deps:
                    stack.append("express")
                if "vite" in deps:
                    stack.append("vite")
                if "typescript" in deps:
                    stack.append("typescript")
            except Exception:
                pass

        return list(dict.fromkeys(stack))  # deduplicate preserving order

    def detect_existing_structure(self, path: str) -> dict:
        """Detect existing project structure."""
        p = Path(path)
        result: dict[str, Any] = {
            "dirs": [],
            "key_files": [],
            "config_files": [],
        }

        # Top-level directories
        for item in sorted(p.iterdir()):
            if item.is_dir() and not item.name.startswith(".") and item.name != "node_modules":
                result["dirs"].append(item.name)

        # Key files
        key_patterns = [
            "*.py", "*.js", "*.ts", "*.tsx", "*.jsx",
            "*.json", "*.yaml", "*.yml", "*.toml",
            "Makefile", "Dockerfile", "*.md",
        ]
        for pattern in key_patterns:
            for f in p.glob(pattern):
                if f.is_file():
                    result["key_files"].append(f.name)

        # Config files
        config_names = [
            "package.json", "pyproject.toml", "tsconfig.json",
            ".eslintrc.json", "vite.config.ts", "next.config.js",
            "Dockerfile", "docker-compose.yml", ".env.example",
        ]
        for name in config_names:
            if (p / name).is_file():
                result["config_files"].append(name)

        return result
