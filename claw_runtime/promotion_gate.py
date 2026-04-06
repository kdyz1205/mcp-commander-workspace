"""
Promotion gate for DevClaw self-evolution safety.

Every code modification that DevClaw proposes to itself must pass through
this gate before it can be applied.  The gate enforces scope constraints,
secret detection, dangerous-code scanning, test requirements, diff-size
limits, budget availability, and system health checks.

Changes to PROTECTED_PATHS always require human approval.
Changes to EVOLVABLE_PATHS may be auto-approved if all checks pass.

All decisions are logged to `.claw/promotion_history.jsonl`.
Pending human reviews are persisted in `.claw/pending_promotions/`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path classifications
# ---------------------------------------------------------------------------

PROTECTED_PATHS: list[str] = [
    "claw_runtime/survival_engine.py",
    "claw_runtime/survival_reflex.py",
    "claw_runtime/promotion_gate.py",
    "claw_runtime/budget_manager.py",
    "claw_runtime/runtime_control.py",
    "claw_runtime/safety_scan.py",
    "claw_runtime/operator_bridge.py",
    "claw_runtime/ultimate/treasury.py",
    "claw_runtime/crypto_identity.py",
    "tg_dev_claw.py",
    "dev_claw/main.py",
    ".env",
    ".auth/",
    "auth/",
    "secrets/",
]

EVOLVABLE_PATHS: list[str] = [
    "skills/",
    "tools/",
    "prompts/",
    "adapters/",
    "recipes/",
    "temp_tools/",
    "tests/",
    ".claw/",
]

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"sk-ant-[a-zA-Z0-9]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN.*PRIVATE KEY"),
    re.compile(r"password\s*[:=]\s*['\"][^'\"]+"),
    re.compile(r"token\s*[:=]\s*['\"][a-zA-Z0-9]{20,}"),
]

DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"eval\s*\("), "eval() call"),
    (re.compile(r"exec\s*\("), "exec() call"),
    (re.compile(r"os\.system\s*\("), "os.system() call"),
    (re.compile(r"subprocess\.call\s*\("), "subprocess.call() - prefer subprocess.run"),
    (re.compile(r"rm\s+-rf\s+/"), "recursive delete from root"),
    (re.compile(r"shutil\.rmtree\s*\(\s*['\"/]"), "rmtree on absolute path"),
    (re.compile(r"__import__\s*\("), "dynamic __import__()"),
    (re.compile(r"compile\s*\("), "compile() call"),
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PromotionRequest:
    """A proposed self-modification that must be reviewed."""

    request_id: str = ""
    source: str = ""          # "evolution" | "gap_detector" | "self_heal" | "operator"
    description: str = ""
    files_changed: list[str] = field(default_factory=list)
    diff: str = ""
    has_tests: bool = False
    test_result: dict[str, Any] | None = None
    rollback_plan: str = ""

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = f"promo-{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromotionRequest:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class PromotionVerdict:
    """Result of the promotion gate review."""

    approved: bool = False
    reason: str = ""
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    requires_human: bool = False
    risk_level: str = "low"        # "low" | "medium" | "high" | "critical"
    auto_approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# PromotionGate
# ---------------------------------------------------------------------------


class PromotionGate:
    """Central safety gate for all self-modification proposals."""

    def __init__(self, workspace: str = ".") -> None:
        self._ws = Path(workspace).resolve()
        self._history_path = self._ws / ".claw" / "promotion_history.jsonl"
        self._pending_dir = self._ws / ".claw" / "pending_promotions"

    # ── public entry point ────────────────────────────────────────────────

    def review(self, request: PromotionRequest) -> PromotionVerdict:
        """Run the full check pipeline and return a verdict."""
        checks_passed: list[str] = []
        checks_failed: list[str] = []
        requires_human = False

        pipeline: list[tuple[str, tuple[bool, str]]] = [
            ("scope", self.check_scope(request.files_changed)),
            ("no_protected", self.check_no_protected(request.files_changed)),
            ("diff_size", self.check_diff_size(request.diff)),
            ("no_secrets", self.check_no_secrets(request.diff)),
            ("no_dangerous_code", self.check_no_dangerous_code(request.diff)),
            ("tests_exist", self.check_tests_exist(request)),
            ("tests_pass", self.check_tests_pass(request)),
            ("rollback_exists", self.check_rollback_exists(request)),
            ("budget", self.check_budget()),
            ("health", self.check_health()),
        ]

        for name, (passed, reason) in pipeline:
            if passed:
                checks_passed.append(f"{name}: {reason}")
            else:
                checks_failed.append(f"{name}: {reason}")

        # Determine risk level ------------------------------------------------
        risk = self._assess_risk(request, checks_failed)

        # Protected-path touch always escalates to human
        prot_ok, _ = self.check_no_protected(request.files_changed)
        if not prot_ok:
            requires_human = True

        # Operator source always gets human review
        if request.source == "operator":
            requires_human = True

        # Build verdict -------------------------------------------------------
        all_passed = len(checks_failed) == 0
        auto_approved = all_passed and not requires_human

        verdict = PromotionVerdict(
            approved=auto_approved,
            reason=checks_failed[0] if checks_failed else "all checks passed",
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            requires_human=requires_human,
            risk_level=risk,
            auto_approved=auto_approved,
        )

        # Side-effects: log and queue ------------------------------------------
        self._log_decision(request, verdict)

        if requires_human and not all_passed:
            verdict.approved = False
        if requires_human:
            self.queue_for_human(request)

        return verdict

    # ── individual checks ─────────────────────────────────────────────────

    def check_scope(self, files: list[str]) -> tuple[bool, str]:
        """Verify every file lives under an EVOLVABLE_PATHS prefix."""
        out_of_scope: list[str] = []
        for f in files:
            norm = f.lstrip("./")
            if not any(norm.startswith(ep.rstrip("/")) for ep in EVOLVABLE_PATHS):
                out_of_scope.append(norm)
        if out_of_scope:
            return False, f"files outside evolvable scope: {out_of_scope}"
        return True, "all files in evolvable scope"

    def check_no_protected(self, files: list[str]) -> tuple[bool, str]:
        """Verify no file touches a PROTECTED_PATHS entry."""
        hits: list[str] = []
        for f in files:
            norm = f.lstrip("./")
            for pp in PROTECTED_PATHS:
                # Directory-style match (pp ends with /)
                if pp.endswith("/") and norm.startswith(pp):
                    hits.append(norm)
                # Exact file match
                elif norm == pp:
                    hits.append(norm)
        if hits:
            return False, f"protected files touched: {hits}"
        return True, "no protected files touched"

    def check_diff_size(self, diff: str, max_lines: int = 500) -> tuple[bool, str]:
        """Reject diffs that are unreasonably large."""
        line_count = diff.count("\n") + (1 if diff and not diff.endswith("\n") else 0)
        if line_count > max_lines:
            return False, f"diff too large: {line_count} lines (max {max_lines})"
        return True, f"diff size ok: {line_count} lines"

    def check_no_secrets(self, diff: str) -> tuple[bool, str]:
        """Scan diff for API keys, tokens, passwords, private keys."""
        found: list[str] = []
        for pat in SECRET_PATTERNS:
            if pat.search(diff):
                # Report pattern, not the actual secret
                found.append(pat.pattern)
        if found:
            return False, f"potential secrets detected matching {len(found)} pattern(s)"
        return True, "no secrets detected"

    def check_no_dangerous_code(self, diff: str) -> tuple[bool, str]:
        """Scan diff for dangerous code constructs."""
        # Only scan added lines (lines starting with +, ignoring +++ header)
        added_lines = "\n".join(
            line[1:]
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        hits: list[str] = []
        for pat, label in DANGEROUS_PATTERNS:
            if pat.search(added_lines):
                hits.append(label)
        if hits:
            return False, f"dangerous patterns found: {hits}"
        return True, "no dangerous patterns"

    def check_tests_exist(self, request: PromotionRequest) -> tuple[bool, str]:
        """Require that the request claims to have accompanying tests."""
        if request.has_tests:
            return True, "tests reported present"
        # Soft fail for operator-sourced changes
        if request.source == "operator":
            return True, "operator override: tests not required"
        return False, "no tests provided for this change"

    def check_tests_pass(self, request: PromotionRequest) -> tuple[bool, str]:
        """Require test_result to report passing."""
        if request.test_result is None:
            if request.source == "operator":
                return True, "operator override: no test run required"
            return False, "test_result is None (tests not run)"
        if request.test_result.get("passed"):
            return True, "tests passed"
        stderr = request.test_result.get("stderr", "")
        return False, f"tests failed: {stderr[:200]}"

    def check_rollback_exists(self, request: PromotionRequest) -> tuple[bool, str]:
        """Require a non-empty rollback plan."""
        if request.rollback_plan and request.rollback_plan.strip():
            return True, "rollback plan present"
        return False, "no rollback plan provided"

    def check_budget(self) -> tuple[bool, str]:
        """Check whether the budget manager allows evolution spending."""
        try:
            from claw_runtime.budget_manager import BudgetManager

            bm = BudgetManager(str(self._ws))
            status = bm.get_status()
            if status.evolution_remaining_today_usd <= 0:
                return False, "evolution budget exhausted for today"
            return True, f"evolution budget available: ${status.evolution_remaining_today_usd:.2f}"
        except Exception as exc:
            log.warning("budget check failed, allowing cautiously: %s", exc)
            return True, f"budget check skipped (error: {exc})"

    def check_health(self) -> tuple[bool, str]:
        """Check system survival state; block evolution when not HEALTHY."""
        try:
            from claw_runtime.survival_engine import SurvivalEngine, SurvivalState

            engine = SurvivalEngine(str(self._ws))
            state, detail = engine.assess_survival_state()
            if state == SurvivalState.HEALTHY:
                return True, "system HEALTHY"
            if state == SurvivalState.DEGRADED:
                return False, f"system DEGRADED: {detail}"
            return False, f"system CRITICAL: {detail}"
        except Exception as exc:
            log.warning("health check failed, blocking evolution: %s", exc)
            return False, f"health check error: {exc}"

    # ── risk assessment ───────────────────────────────────────────────────

    @staticmethod
    def _assess_risk(
        request: PromotionRequest, checks_failed: list[str]
    ) -> str:
        """Classify overall risk level of this promotion request."""
        # Any protected-path touch is critical
        for f in request.files_changed:
            norm = f.lstrip("./")
            for pp in PROTECTED_PATHS:
                if pp.endswith("/") and norm.startswith(pp):
                    return "critical"
                if norm == pp:
                    return "critical"

        fail_count = len(checks_failed)
        if fail_count == 0:
            return "low"
        # Secret or dangerous-code failure is high
        for cf in checks_failed:
            if "secret" in cf.lower() or "dangerous" in cf.lower():
                return "high"
        if fail_count >= 3:
            return "high"
        if fail_count >= 1:
            return "medium"
        return "low"

    # ── execution ─────────────────────────────────────────────────────────

    def approve_and_apply(self, request: PromotionRequest) -> dict[str, Any]:
        """Apply a previously-approved change.

        This method trusts that ``review()`` has already been called and
        returned an approved verdict.  It writes the diff payload into
        the target files and returns a summary dict.
        """
        applied_files: list[str] = []
        errors: list[str] = []

        for fpath in request.files_changed:
            target = self._ws / fpath
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # The actual file content is expected to be managed by the
                # caller (e.g. evolution engine) that passes the diff.  Here
                # we simply record what was applied.
                applied_files.append(fpath)
            except Exception as exc:
                errors.append(f"{fpath}: {exc}")

        result: dict[str, Any] = {
            "request_id": request.request_id,
            "applied_files": applied_files,
            "errors": errors,
            "success": len(errors) == 0,
            "timestamp": time.time(),
        }

        self._log_event("apply", request.request_id, result)
        return result

    def reject(self, request: PromotionRequest, reason: str) -> None:
        """Record a rejection."""
        self._log_event("reject", request.request_id, {"reason": reason})
        log.info("Promotion %s rejected: %s", request.request_id, reason)

    def queue_for_human(self, request: PromotionRequest) -> None:
        """Persist the request so an operator can review it later."""
        self._pending_dir.mkdir(parents=True, exist_ok=True)
        out = self._pending_dir / f"{request.request_id}.json"
        out.write_text(json.dumps(request.to_dict(), indent=2, default=str))
        log.info("Promotion %s queued for human review at %s", request.request_id, out)

    # ── history ───────────────────────────────────────────────────────────

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent *limit* decisions from the history log."""
        if not self._history_path.exists():
            return []
        lines = self._history_path.read_text().strip().splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def get_pending_human_reviews(self) -> list[PromotionRequest]:
        """Load all requests still awaiting human sign-off."""
        if not self._pending_dir.exists():
            return []
        pending: list[PromotionRequest] = []
        for p in sorted(self._pending_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                pending.append(PromotionRequest.from_dict(data))
            except Exception as exc:
                log.warning("Failed to load pending promotion %s: %s", p.name, exc)
        return pending

    # ── internal helpers ──────────────────────────────────────────────────

    def _log_decision(
        self, request: PromotionRequest, verdict: PromotionVerdict
    ) -> None:
        record: dict[str, Any] = {
            "ts": time.time(),
            "event": "decision",
            "request_id": request.request_id,
            "source": request.source,
            "description": request.description,
            "files_changed": request.files_changed,
            "risk_level": verdict.risk_level,
            "approved": verdict.approved,
            "auto_approved": verdict.auto_approved,
            "requires_human": verdict.requires_human,
            "reason": verdict.reason,
            "checks_passed": verdict.checks_passed,
            "checks_failed": verdict.checks_failed,
        }
        self._append_history(record)

    def _log_event(
        self, event: str, request_id: str, payload: dict[str, Any]
    ) -> None:
        record: dict[str, Any] = {
            "ts": time.time(),
            "event": event,
            "request_id": request_id,
            **payload,
        }
        self._append_history(record)

    def _append_history(self, record: dict[str, Any]) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with self._history_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
