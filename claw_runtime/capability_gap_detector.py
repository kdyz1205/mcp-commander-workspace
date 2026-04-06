"""
Capability Gap Detector — the "Manus-like intelligence" layer for DevClaw.

When a task fails, this module doesn't just report "failed". It analyses
observation signals, classifies the failure mode, detects whether DevClaw is
missing a capability, and proposes (or auto-applies) concrete fixes.

Integration points:
  - claw_runtime/skill_registry.py        — catalogs available skills
  - claw_runtime/code_synthesis_pipeline.py — can synthesize new skills
  - claw_runtime/nightly_evolution.py      — failure → draft skill → promote
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProposedFix:
    """A single concrete action that could close a capability gap."""
    fix_type: str       # "install_dep" | "create_skill" | "create_script" | "register_tool" | "configure_env" | "install_cli"
    description: str
    command: str | None  # e.g. "pip install playwright"
    code: str | None     # e.g. generated helper script content
    risk: str            # "low" | "medium" | "high"
    auto_approved: bool  # low-risk fixes can be auto-approved


@dataclass
class CapabilityGap:
    """A detected missing capability."""
    gap_id: str
    category: str        # "missing_tool" | "missing_skill" | "missing_dependency" | "missing_adapter" | "missing_knowledge"
    description: str
    severity: str        # "blocking" | "degrading" | "optional"
    detected_from: str   # error message or observation that revealed the gap
    proposed_fixes: list[ProposedFix] = field(default_factory=list)
    auto_fixable: bool = False
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Detection rules — each rule is a (compiled regex, handler) pair
# ---------------------------------------------------------------------------

@dataclass
class _DetectionRule:
    """Internal: one pattern→gap mapping."""
    pattern: re.Pattern[str]
    category: str
    severity: str
    description_template: str   # may contain {match} placeholder
    make_fixes: Any             # callable(re.Match) -> list[ProposedFix]


def _pip_install_fixes(match: re.Match) -> list[ProposedFix]:
    mod = match.group(1).split(".")[0]  # top-level package
    return [ProposedFix(
        fix_type="install_dep",
        description=f"Install missing Python package '{mod}'",
        command=f"pip install {mod}",
        code=None,
        risk="low",
        auto_approved=True,
    )]


def _command_not_found_fixes(match: re.Match) -> list[ProposedFix]:
    cmd = match.group(1)
    fixes: list[ProposedFix] = []
    # heuristic: node-ecosystem vs system package
    node_tools = {"prettier", "eslint", "tsc", "tsx", "npx", "yarn", "pnpm", "vitest", "jest"}
    if cmd in node_tools:
        fixes.append(ProposedFix(
            fix_type="install_cli",
            description=f"Install Node CLI tool '{cmd}' globally",
            command=f"npm install -g {cmd}",
            code=None,
            risk="medium",
            auto_approved=False,
        ))
    else:
        fixes.append(ProposedFix(
            fix_type="install_cli",
            description=f"Install system tool '{cmd}'",
            command=f"apt-get install -y {cmd}",
            code=None,
            risk="medium",
            auto_approved=False,
        ))
    return fixes


def _missing_skill_fixes(match: re.Match) -> list[ProposedFix]:
    skill = match.group(1)
    return [ProposedFix(
        fix_type="create_skill",
        description=f"Synthesize skill '{skill}' via code_synthesis_pipeline",
        command=None,
        code=None,
        risk="medium",
        auto_approved=False,
    )]


def _playwright_fixes(_match: re.Match) -> list[ProposedFix]:
    return [ProposedFix(
        fix_type="install_dep",
        description="Install Playwright and its browser binaries",
        command="pip install playwright && python -m playwright install --with-deps chromium",
        code=None,
        risk="low",
        auto_approved=True,
    )]


def _econnrefused_fixes(match: re.Match) -> list[ProposedFix]:
    port = match.group(1) if match.lastindex and match.lastindex >= 1 else "unknown"
    return [ProposedFix(
        fix_type="register_tool",
        description=f"Start or register the service on port {port}",
        command=None,
        code=None,
        risk="medium",
        auto_approved=False,
    )]


def _audio_pipeline_fixes(_match: re.Match) -> list[ProposedFix]:
    return [ProposedFix(
        fix_type="create_skill",
        description="Create an audio processing skill (STT/TTS adapter)",
        command=None,
        code=None,
        risk="medium",
        auto_approved=False,
    )]


def _parser_adapter_fixes(match: re.Match) -> list[ProposedFix]:
    fmt = match.group(1) if match.lastindex and match.lastindex >= 1 else "unknown"
    return [ProposedFix(
        fix_type="create_skill",
        description=f"Create parser adapter for format '{fmt}'",
        command=None,
        code=None,
        risk="medium",
        auto_approved=False,
    )]


def _browser_observer_fixes(_match: re.Match) -> list[ProposedFix]:
    return [ProposedFix(
        fix_type="create_skill",
        description="Register browser observation / instrumentation skill",
        command=None,
        code=None,
        risk="medium",
        auto_approved=False,
    )]


def _unknown_api_fixes(match: re.Match) -> list[ProposedFix]:
    endpoint = match.group(1) if match.lastindex and match.lastindex >= 1 else "unknown"
    return [ProposedFix(
        fix_type="create_skill",
        description=f"Research and create adapter for API endpoint '{endpoint}'",
        command=None,
        code=None,
        risk="high",
        auto_approved=False,
    )]


def _env_var_fixes(match: re.Match) -> list[ProposedFix]:
    var = match.group(1)
    return [ProposedFix(
        fix_type="configure_env",
        description=f"Set environment variable '{var}'",
        command=None,
        code=f"{var}=<value>  # TODO: set appropriate value",
        risk="low",
        auto_approved=True,
    )]


def _build_detection_rules() -> list[_DetectionRule]:
    """Construct the ordered rule table."""
    return [
        _DetectionRule(
            pattern=re.compile(r"ModuleNotFoundError:\s*No module named ['\"]?(\S+?)['\"]?", re.I),
            category="missing_dependency",
            severity="blocking",
            description_template="Python module '{match}' is not installed",
            make_fixes=_pip_install_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"(?:command not found|not recognized)(?:.*?):?\s*(\S+)", re.I),
            category="missing_tool",
            severity="blocking",
            description_template="CLI tool '{match}' is not available",
            make_fixes=_command_not_found_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"No skill found for ['\"]?(\S+?)['\"]?", re.I),
            category="missing_skill",
            severity="blocking",
            description_template="No registered skill for '{match}'",
            make_fixes=_missing_skill_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"playwright.*(?:not installed|not found|ModuleNotFoundError)", re.I),
            category="missing_dependency",
            severity="blocking",
            description_template="Playwright browser automation is not installed",
            make_fixes=_playwright_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"ECONNREFUSED.*?(?::(\d+))?", re.I),
            category="missing_tool",
            severity="blocking",
            description_template="Connection refused — service on port {match} is not running",
            make_fixes=_econnrefused_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"No (?:STT|TTS|voice|audio) pipeline", re.I),
            category="missing_adapter",
            severity="degrading",
            description_template="Audio processing pipeline is not available",
            make_fixes=_audio_pipeline_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"Cannot parse format ['\"]?(\S+?)['\"]?", re.I),
            category="missing_adapter",
            severity="degrading",
            description_template="No parser available for format '{match}'",
            make_fixes=_parser_adapter_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"No browser (?:instrumentation|observer)", re.I),
            category="missing_skill",
            severity="degrading",
            description_template="Browser instrumentation is not registered",
            make_fixes=_browser_observer_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(r"Unknown API endpoint[:\s]*(\S+)?", re.I),
            category="missing_knowledge",
            severity="degrading",
            description_template="Unknown API endpoint '{match}'",
            make_fixes=_unknown_api_fixes,
        ),
        _DetectionRule(
            pattern=re.compile(
                r"(?:env(?:ironment)?\s*var(?:iable)?\s+['\"]?(\w+)['\"]?\s*(?:not set|is not set|missing|undefined))"
                r"|(?:['\"]?(\w+)['\"]?\s*(?:env(?:ironment)?\s*var(?:iable)?)\s*(?:not set|is not set|missing|undefined))",
                re.I,
            ),
            category="missing_dependency",
            severity="blocking",
            description_template="Environment variable '{match}' is not set",
            make_fixes=_env_var_fixes,
        ),
    ]


# Additional transient-error patterns (match → classify as transient)
_TRANSIENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:ConnectionError|TimeoutError|ReadTimeout|ConnectTimeout)", re.I),
    re.compile(r"rate.?limit", re.I),
    re.compile(r"HTTP\s+(?:429|502|503|504)", re.I),
    re.compile(r"(?:ETIMEDOUT|ENETUNREACH|EHOSTUNREACH)", re.I),
    re.compile(r"flaky|intermittent|retry", re.I),
    re.compile(r"TLS.*handshake|SSL.*error", re.I),
]

_CONFIG_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:FileNotFoundError|No such file or directory).*(?:\.env|config|\.ya?ml|\.toml|\.json)", re.I),
    re.compile(r"(?:PermissionError|Permission denied)", re.I),
    re.compile(r"invalid.*(?:config|setting|option)", re.I),
    re.compile(r"(?:wrong|bad|invalid)\s+(?:path|directory|file)", re.I),
]


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class CapabilityGapDetector:
    """Detect, classify, and auto-remediate capability gaps in DevClaw."""

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = Path(workspace).resolve()
        self._claw_dir = self.workspace / ".claw"
        self._gaps_path = self._claw_dir / "capability_gaps.jsonl"
        self._fixes_path = self._claw_dir / "auto_fixes.jsonl"
        self._rules = _build_detection_rules()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        task_id: str,
        error_signals: list[str],
        observation_summary: dict[str, Any] | None = None,
    ) -> list[CapabilityGap]:
        """Analyse error signals and return detected capability gaps."""
        gaps: list[CapabilityGap] = []
        seen_categories: set[str] = set()

        combined = "\n".join(str(s) for s in error_signals)
        if observation_summary:
            combined += "\n" + json.dumps(observation_summary, default=str)

        for rule in self._rules:
            m = rule.pattern.search(combined)
            if m is None:
                continue
            # Avoid duplicate categories from same detection pass
            key = (rule.category, m.group(0)[:120])
            if key in seen_categories:
                continue
            seen_categories.add(key)

            match_val = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            # env var rule has two groups; pick whichever matched
            if match_val is None and m.lastindex and m.lastindex >= 2:
                match_val = m.group(2)
            if match_val is None:
                match_val = m.group(0)

            desc = rule.description_template.replace("{match}", match_val)
            fixes = rule.make_fixes(m)
            auto = any(f.auto_approved for f in fixes)

            gap = CapabilityGap(
                gap_id=self._make_gap_id(task_id, rule.category, desc),
                category=rule.category,
                description=desc,
                severity=rule.severity,
                detected_from=m.group(0)[:500],
                proposed_fixes=fixes,
                auto_fixable=auto,
            )
            gaps.append(gap)
            self.record_gap(gap)

        return gaps

    def classify_failure(self, error_msg: str) -> str:
        """Classify a single error message: 'transient' | 'config' | 'capability_gap'."""
        for pat in _TRANSIENT_PATTERNS:
            if pat.search(error_msg):
                return "transient"
        for pat in _CONFIG_PATTERNS:
            if pat.search(error_msg):
                return "config"
        for rule in self._rules:
            if rule.pattern.search(error_msg):
                return "capability_gap"
        # Default: if nothing matched, treat as config (safest fallback)
        return "config"

    def propose_fixes(self, gap: CapabilityGap) -> list[ProposedFix]:
        """Return (possibly augmented) fix proposals for an existing gap."""
        if gap.proposed_fixes:
            return gap.proposed_fixes
        # Fallback: re-run rule matching against the detection source text
        for rule in self._rules:
            m = rule.pattern.search(gap.detected_from)
            if m:
                fixes = rule.make_fixes(m)
                gap.proposed_fixes = fixes
                gap.auto_fixable = any(f.auto_approved for f in fixes)
                return fixes
        return []

    def auto_fix(self, gap: CapabilityGap, dry_run: bool = False) -> dict[str, Any]:
        """
        Attempt to automatically fix a capability gap.

        Returns a dict with keys: success, actions_taken, errors.
        High-risk fixes are NEVER auto-applied — they are queued as proposals.
        """
        result: dict[str, Any] = {
            "success": False,
            "actions_taken": [],
            "errors": [],
            "gap_id": gap.gap_id,
            "dry_run": dry_run,
        }

        fixes = gap.proposed_fixes or self.propose_fixes(gap)
        if not fixes:
            result["errors"].append("No proposed fixes available")
            return result

        applied_any = False
        for fix in fixes:
            if fix.risk == "high":
                result["actions_taken"].append({
                    "fix": fix.description,
                    "status": "queued_for_review",
                    "reason": "high-risk fix requires operator approval",
                })
                self._log_fix(gap, fix, status="queued_for_review", dry_run=dry_run)
                continue

            if not fix.auto_approved and fix.risk != "low":
                result["actions_taken"].append({
                    "fix": fix.description,
                    "status": "queued_for_review",
                    "reason": "not auto-approved",
                })
                self._log_fix(gap, fix, status="queued_for_review", dry_run=dry_run)
                continue

            action = {"fix": fix.description}

            if dry_run:
                action["status"] = "dry_run"
                action["would_run"] = fix.command or "(code write)"
                result["actions_taken"].append(action)
                self._log_fix(gap, fix, status="dry_run", dry_run=True)
                applied_any = True
                continue

            try:
                if fix.fix_type == "install_dep":
                    action.update(self._apply_install_dep(fix))
                elif fix.fix_type == "install_cli":
                    action.update(self._apply_install_cli(fix))
                elif fix.fix_type == "create_script":
                    action.update(self._apply_create_script(fix))
                elif fix.fix_type == "create_skill":
                    action.update(self._apply_create_skill(fix, gap))
                elif fix.fix_type == "configure_env":
                    action.update(self._apply_configure_env(fix))
                elif fix.fix_type == "register_tool":
                    action["status"] = "queued_for_review"
                    action["reason"] = "register_tool requires manual intervention"
                else:
                    action["status"] = "unsupported_fix_type"
            except Exception as exc:
                action["status"] = "error"
                action["error"] = str(exc)[:500]
                result["errors"].append(str(exc)[:500])

            result["actions_taken"].append(action)
            self._log_fix(gap, fix, status=action.get("status", "unknown"), dry_run=False)
            if action.get("status") == "applied":
                applied_any = True

        result["success"] = applied_any and not result["errors"]
        return result

    def record_gap(self, gap: CapabilityGap) -> None:
        """Persist a gap record to .claw/capability_gaps.jsonl."""
        self._claw_dir.mkdir(parents=True, exist_ok=True)
        record = _gap_to_dict(gap)
        try:
            with self._gaps_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def get_gap_history(self, limit: int = 50) -> list[CapabilityGap]:
        """Return recent gaps from the persisted log."""
        raw = _read_tail_jsonl(self._gaps_path, max_lines=limit)
        gaps: list[CapabilityGap] = []
        for obj in raw:
            try:
                fixes = [
                    ProposedFix(**f) for f in obj.get("proposed_fixes", [])
                ]
                gaps.append(CapabilityGap(
                    gap_id=obj["gap_id"],
                    category=obj["category"],
                    description=obj["description"],
                    severity=obj["severity"],
                    detected_from=obj["detected_from"],
                    proposed_fixes=fixes,
                    auto_fixable=obj.get("auto_fixable", False),
                    timestamp=obj.get("timestamp", 0.0),
                ))
            except (KeyError, TypeError):
                continue
        return gaps

    def has_recurring_gap(self, description: str) -> bool:
        """Return True if a gap with a similar description has been seen 2+ times."""
        norm = _normalise(description)
        history = self.get_gap_history(limit=200)
        count = sum(1 for g in history if _normalise(g.description) == norm)
        return count >= 2

    # ------------------------------------------------------------------
    # Private: fix application helpers
    # ------------------------------------------------------------------

    def _apply_install_dep(self, fix: ProposedFix) -> dict[str, Any]:
        cmd = fix.command or ""
        if not cmd:
            return {"status": "error", "error": "no install command"}
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            return {"status": "applied", "output": proc.stdout[-500:]}
        return {
            "status": "error",
            "error": (proc.stderr or proc.stdout)[-500:],
        }

    def _apply_install_cli(self, fix: ProposedFix) -> dict[str, Any]:
        cmd = fix.command or ""
        if not cmd:
            return {"status": "error", "error": "no install command"}
        # Safety: only allow apt-get and npm
        allowed_prefixes = ("apt-get ", "npm install", "brew install")
        if not any(cmd.startswith(p) for p in allowed_prefixes):
            return {"status": "queued_for_review", "reason": f"unrecognised installer: {cmd[:60]}"}
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 0:
            return {"status": "applied", "output": proc.stdout[-500:]}
        return {
            "status": "error",
            "error": (proc.stderr or proc.stdout)[-500:],
        }

    def _apply_create_script(self, fix: ProposedFix) -> dict[str, Any]:
        if not fix.code:
            return {"status": "error", "error": "no code provided for create_script"}
        target_dir = self.workspace / "tools"
        target_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", fix.description.lower())[:40].strip("_")
        path = target_dir / f"{slug}.py"
        path.write_text(fix.code, encoding="utf-8")
        return {"status": "applied", "path": str(path)}

    def _apply_create_skill(self, fix: ProposedFix, gap: CapabilityGap) -> dict[str, Any]:
        """Queue a skill creation request for the code synthesis pipeline."""
        # We don't invoke the pipeline directly here — we write a request file
        # that nightly_evolution or an operator can pick up.
        requests_dir = self._claw_dir / "skill_requests"
        requests_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "gap_id": gap.gap_id,
            "need_description": gap.description,
            "error_context": gap.detected_from[:1000],
            "proposed_solution": fix.description,
            "timestamp": time.time(),
        }
        path = requests_dir / f"{gap.gap_id}.json"
        path.write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"status": "applied", "skill_request": str(path)}

    def _apply_configure_env(self, fix: ProposedFix) -> dict[str, Any]:
        suggested_path = self.workspace / ".env.suggested"
        entry = fix.code or fix.description
        try:
            with suggested_path.open("a", encoding="utf-8") as f:
                f.write(f"# Auto-suggested by capability_gap_detector\n{entry}\n\n")
            return {"status": "applied", "path": str(suggested_path)}
        except OSError as exc:
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # Private: logging / helpers
    # ------------------------------------------------------------------

    def _log_fix(
        self,
        gap: CapabilityGap,
        fix: ProposedFix,
        *,
        status: str,
        dry_run: bool,
    ) -> None:
        self._claw_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "gap_id": gap.gap_id,
            "fix_type": fix.fix_type,
            "description": fix.description,
            "command": fix.command,
            "risk": fix.risk,
            "status": status,
            "dry_run": dry_run,
        }
        try:
            with self._fixes_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def _make_gap_id(task_id: str, category: str, description: str) -> str:
        raw = f"{task_id}:{category}:{description}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _gap_to_dict(gap: CapabilityGap) -> dict[str, Any]:
    d = asdict(gap)
    return d


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


def _normalise(s: str) -> str:
    """Lowercase, collapse whitespace, strip quotes — for fuzzy comparison."""
    s = s.lower().strip()
    s = re.sub(r"['\"]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s
