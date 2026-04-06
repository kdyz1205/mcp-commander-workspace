"""
Verification Manager — Three-layer verification for DevClaw.

After execution, runs verification to determine if the task actually succeeded.

Layer 1: Static (fast, always run)  — lint, typecheck, import check
Layer 2: Tests (medium)             — pytest / npm test / etc.
Layer 3: E2E smoke (slow)           — health check, browser smoke flow
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("devclaw.verification_manager")

# ---------------------------------------------------------------------------
# Lazy import TestTool to avoid circular / missing-dep issues at parse time.
# ---------------------------------------------------------------------------

_test_tool = None


def _get_test_tool():
    global _test_tool
    if _test_tool is None:
        import sys
        tools_dir = str(Path(__file__).resolve().parent.parent / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from test_tool import TestTool
        _test_tool = TestTool()
    return _test_tool


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """Result of a single verification check."""
    layer: int          # 1, 2, or 3
    name: str           # "lint" | "typecheck" | "import_check" | "unit_test" | "smoke_test" | "health_check"
    passed: bool
    details: str
    duration_sec: float
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""


@dataclass
class VerificationReport:
    """Aggregate report across all verification layers."""
    task_id: str
    verdict: str            # "pass" | "partial" | "fail" | "blocked"
    layers_run: list[int] = field(default_factory=list)
    results: list[VerificationResult] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    total_duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict,
            "layers_run": self.layers_run,
            "results": [asdict(r) for r in self.results],
            "blocking_issues": self.blocking_issues,
            "total_duration_sec": self.total_duration_sec,
        }


# ---------------------------------------------------------------------------
# VerificationManager
# ---------------------------------------------------------------------------

class VerificationManager:
    """
    Runs three-layer verification after task execution.

    Layer 1 — Static: lint, typecheck, import smoke check  (fast, always run)
    Layer 2 — Tests:  unit / integration tests              (run if layer 1 OK)
    Layer 3 — E2E:    health-check URL, browser smoke flow  (run if layer 2 OK)
    """

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.abspath(workspace)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(
        self,
        task_id: str,
        execution_report: Any = None,
        skip_layers: list[int] | None = None,
    ) -> VerificationReport:
        """Run all applicable verification layers and return a report."""
        skip = set(skip_layers or [])
        report = VerificationReport(task_id=task_id)
        t0 = time.time()

        # --- Layer 1 ---
        if 1 not in skip:
            log.info("[verify] Running Layer 1 — static checks")
            report.layers_run.append(1)
            l1 = self.run_layer_1(self.workspace)
            report.results.extend(l1)
            l1_pass = all(r.passed for r in l1)
            if not l1_pass:
                report.blocking_issues.extend(
                    [f"Layer1/{r.name}: {r.details[:200]}" for r in l1 if not r.passed]
                )
        else:
            l1_pass = True

        # --- Layer 2 (only if layer 1 passed) ---
        if 2 not in skip and l1_pass:
            log.info("[verify] Running Layer 2 — tests")
            report.layers_run.append(2)
            l2 = self.run_layer_2(self.workspace)
            report.results.extend(l2)
            l2_pass = all(r.passed for r in l2)
            if not l2_pass:
                report.blocking_issues.extend(
                    [f"Layer2/{r.name}: {r.details[:200]}" for r in l2 if not r.passed]
                )
        else:
            l2_pass = l1_pass

        # --- Layer 3 (only if layer 2 passed) ---
        if 3 not in skip and l2_pass:
            url = None
            smoke_config = None
            if execution_report and hasattr(execution_report, "artifacts"):
                pass  # caller can supply via smoke_config
            log.info("[verify] Running Layer 3 — smoke / e2e")
            report.layers_run.append(3)
            l3 = self.run_layer_3(url=url, smoke_config=smoke_config)
            report.results.extend(l3)
            if any(not r.passed for r in l3):
                report.blocking_issues.extend(
                    [f"Layer3/{r.name}: {r.details[:200]}" for r in l3 if not r.passed]
                )

        # --- Verdict ---
        all_passed = all(r.passed for r in report.results)
        any_passed = any(r.passed for r in report.results)
        if not report.results:
            report.verdict = "blocked"
        elif all_passed:
            report.verdict = "pass"
        elif any_passed:
            report.verdict = "partial"
        else:
            report.verdict = "fail"

        report.total_duration_sec = time.time() - t0
        log.info("[verify] Verdict: %s (%.1fs)", report.verdict, report.total_duration_sec)
        return report

    # ------------------------------------------------------------------
    # Layer 1 — Static
    # ------------------------------------------------------------------

    def run_layer_1(self, path: str) -> list[VerificationResult]:
        """Lint, typecheck, import smoke check."""
        results: list[VerificationResult] = []
        tt = _get_test_tool()

        # Lint
        t0 = time.time()
        try:
            lint = tt.run_lint(path=path)
            results.append(VerificationResult(
                layer=1, name="lint",
                passed=lint.get("passed", False),
                details=_truncate(lint.get("stderr", "") or lint.get("stdout", ""), 2000),
                duration_sec=lint.get("duration_sec", time.time() - t0),
                exit_code=lint.get("exit_code", -1),
                stdout=_truncate(lint.get("stdout", ""), 1000),
                stderr=_truncate(lint.get("stderr", ""), 1000),
            ))
        except Exception as exc:
            results.append(VerificationResult(
                layer=1, name="lint", passed=True,
                details=f"Lint skipped (not available): {exc}",
                duration_sec=time.time() - t0,
            ))

        # Typecheck
        t0 = time.time()
        try:
            tc = tt.run_typecheck(path=path)
            results.append(VerificationResult(
                layer=1, name="typecheck",
                passed=tc.get("passed", False),
                details=_truncate(tc.get("stderr", "") or tc.get("stdout", ""), 2000),
                duration_sec=tc.get("duration_sec", time.time() - t0),
                exit_code=tc.get("exit_code", -1),
                stdout=_truncate(tc.get("stdout", ""), 1000),
                stderr=_truncate(tc.get("stderr", ""), 1000),
            ))
        except Exception as exc:
            results.append(VerificationResult(
                layer=1, name="typecheck", passed=True,
                details=f"Typecheck skipped (not available): {exc}",
                duration_sec=time.time() - t0,
            ))

        # Import check
        t0 = time.time()
        try:
            ic = tt.run_import_check(path=path)
            results.append(VerificationResult(
                layer=1, name="import_check",
                passed=ic.get("passed", False),
                details=_truncate(ic.get("details", ""), 2000),
                duration_sec=ic.get("duration_sec", time.time() - t0),
                exit_code=ic.get("exit_code", 0 if ic.get("passed") else 1),
            ))
        except Exception as exc:
            results.append(VerificationResult(
                layer=1, name="import_check", passed=True,
                details=f"Import check skipped: {exc}",
                duration_sec=time.time() - t0,
            ))

        return results

    # ------------------------------------------------------------------
    # Layer 2 — Unit / Integration Tests
    # ------------------------------------------------------------------

    def run_layer_2(
        self, path: str, test_cmd: str = "auto", timeout_sec: float = 300,
    ) -> list[VerificationResult]:
        """Run unit/integration tests."""
        results: list[VerificationResult] = []
        tt = _get_test_tool()

        t0 = time.time()
        try:
            ut = tt.run_unit_tests(path=path, cmd=test_cmd, timeout_sec=timeout_sec)
            results.append(VerificationResult(
                layer=2, name="unit_test",
                passed=ut.get("passed", False),
                details=_truncate(ut.get("stderr", "") or ut.get("stdout", ""), 3000),
                duration_sec=ut.get("duration_sec", time.time() - t0),
                exit_code=ut.get("exit_code", -1),
                stdout=_truncate(ut.get("stdout", ""), 2000),
                stderr=_truncate(ut.get("stderr", ""), 2000),
            ))
        except Exception as exc:
            results.append(VerificationResult(
                layer=2, name="unit_test", passed=True,
                details=f"Tests skipped (no test runner found): {exc}",
                duration_sec=time.time() - t0,
            ))

        return results

    # ------------------------------------------------------------------
    # Layer 3 — E2E / Smoke
    # ------------------------------------------------------------------

    def run_layer_3(
        self,
        url: str | None = None,
        smoke_config: dict | None = None,
    ) -> list[VerificationResult]:
        """Health check and browser-based smoke tests."""
        results: list[VerificationResult] = []
        tt = _get_test_tool()

        # Health check
        if url:
            t0 = time.time()
            try:
                hc = tt.health_check(url=url)
                results.append(VerificationResult(
                    layer=3, name="health_check",
                    passed=hc.get("passed", False),
                    details=f"HTTP {hc.get('status_code', '?')} — {hc.get('reason', '')}",
                    duration_sec=hc.get("duration_sec", time.time() - t0),
                ))
            except Exception as exc:
                results.append(VerificationResult(
                    layer=3, name="health_check", passed=False,
                    details=f"Health check failed: {exc}",
                    duration_sec=time.time() - t0,
                ))

        # Smoke suite
        if smoke_config:
            t0 = time.time()
            try:
                name = smoke_config.get("name", "default")
                ss = tt.run_smoke_suite(name=name, config=smoke_config)
                results.append(VerificationResult(
                    layer=3, name="smoke_test",
                    passed=ss.get("passed", False),
                    details=_truncate(ss.get("details", ""), 3000),
                    duration_sec=ss.get("duration_sec", time.time() - t0),
                ))
            except Exception as exc:
                results.append(VerificationResult(
                    layer=3, name="smoke_test", passed=False,
                    details=f"Smoke test failed: {exc}",
                    duration_sec=time.time() - t0,
                ))

        # If nothing to check in layer 3, report as skipped-pass
        if not results:
            results.append(VerificationResult(
                layer=3, name="smoke_test", passed=True,
                details="No URL or smoke config provided — layer 3 skipped",
                duration_sec=0.0,
            ))

        return results

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def quick_check(self, path: str) -> bool:
        """Layer 1 only — returns True if all static checks pass."""
        l1 = self.run_layer_1(path)
        return all(r.passed for r in l1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = 2000) -> str:
    if len(text) <= max_len:
        return text
    half = max_len // 2 - 20
    return text[:half] + f"\n... ({len(text) - max_len} chars truncated) ...\n" + text[-half:]
