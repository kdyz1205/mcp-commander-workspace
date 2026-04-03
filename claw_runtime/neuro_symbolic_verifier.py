"""
System 2 verification gate: symbolic checks before dangerous actions.

LLM provides System 1 (intuition / fast pattern matching).
This module provides System 2 (slow, deliberate, mathematical verification).

Every dangerous action (trade, file mutation, terminal command) must pass
through the symbolic gate before execution.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """Uniform return type for all verifiers."""
    approved: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    vetoed_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "checks": self.checks,
            "warnings": self.warnings,
            "vetoed_reason": self.vetoed_reason,
        }


@dataclass
class CommandSafetyResult:
    """Return type for terminal command verification."""
    safe: bool
    risk_level: str  # "low" | "medium" | "high" | "critical"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "risk_level": self.risk_level,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Trade verification (Kelly, risk/reward, drawdown, Sharpe)
# ---------------------------------------------------------------------------

def _check_kelly_criterion(proposal: dict[str, Any]) -> dict[str, Any]:
    """Validate position size against Kelly criterion."""
    win_prob = proposal.get("win_probability", 0.0)
    win_loss_ratio = proposal.get("win_loss_ratio", 0.0)
    position_size = proposal.get("position_size", 0.0)
    bankroll = proposal.get("bankroll", 0.0)

    check: dict[str, Any] = {"name": "kelly_criterion", "passed": False, "detail": ""}

    if bankroll <= 0:
        check["detail"] = "bankroll must be positive"
        return check
    if not (0.0 < win_prob < 1.0):
        check["detail"] = f"win_probability={win_prob} out of range (0, 1)"
        return check
    if win_loss_ratio <= 0:
        check["detail"] = f"win_loss_ratio={win_loss_ratio} must be positive"
        return check

    # Kelly fraction: f* = p - q / b  where p=win_prob, q=1-p, b=win_loss_ratio
    q = 1.0 - win_prob
    kelly_fraction = win_prob - (q / win_loss_ratio)
    kelly_fraction = max(kelly_fraction, 0.0)  # never negative

    max_position = kelly_fraction * bankroll
    check["kelly_fraction"] = round(kelly_fraction, 6)
    check["max_position"] = round(max_position, 2)

    if position_size <= max_position:
        check["passed"] = True
        check["detail"] = (
            f"position_size={position_size} <= kelly_max={max_position:.2f} "
            f"(kelly_f={kelly_fraction:.4f})"
        )
    else:
        check["detail"] = (
            f"position_size={position_size} EXCEEDS kelly_max={max_position:.2f} "
            f"(kelly_f={kelly_fraction:.4f}) — over-leveraged"
        )
    return check


def _check_risk_reward(proposal: dict[str, Any], *, min_ratio: float = 1.5) -> dict[str, Any]:
    """Risk/reward ratio must exceed threshold."""
    reward = proposal.get("expected_reward", 0.0)
    risk = proposal.get("expected_risk", 0.0)

    check: dict[str, Any] = {"name": "risk_reward_ratio", "passed": False, "detail": ""}

    if risk <= 0:
        check["detail"] = "expected_risk must be positive"
        return check

    ratio = reward / risk
    check["ratio"] = round(ratio, 4)

    if ratio >= min_ratio:
        check["passed"] = True
        check["detail"] = f"ratio={ratio:.2f} >= min={min_ratio}"
    else:
        check["detail"] = f"ratio={ratio:.2f} < min={min_ratio} — unfavorable risk/reward"
    return check


def _check_max_drawdown(proposal: dict[str, Any], *, max_risk_pct: float = 0.05) -> dict[str, Any]:
    """Position risk must be < max_risk_pct of portfolio."""
    position_risk = proposal.get("expected_risk", 0.0) * proposal.get("position_size", 0.0)
    portfolio_value = proposal.get("bankroll", 0.0)

    check: dict[str, Any] = {"name": "max_drawdown", "passed": False, "detail": ""}

    if portfolio_value <= 0:
        check["detail"] = "bankroll must be positive"
        return check

    risk_pct = position_risk / portfolio_value if portfolio_value else 1.0
    check["risk_pct"] = round(risk_pct, 6)

    if risk_pct <= max_risk_pct:
        check["passed"] = True
        check["detail"] = f"risk={risk_pct:.2%} <= max={max_risk_pct:.2%}"
    else:
        check["detail"] = (
            f"risk={risk_pct:.2%} > max={max_risk_pct:.2%} — "
            "position risks too much of portfolio"
        )
    return check


def _check_sharpe_sanity(proposal: dict[str, Any]) -> dict[str, Any]:
    """If historical returns are available, check Sharpe ratio sanity."""
    returns = proposal.get("historical_returns")
    check: dict[str, Any] = {"name": "sharpe_sanity", "passed": True, "detail": ""}

    if not returns or not isinstance(returns, (list, tuple)) or len(returns) < 2:
        check["detail"] = "no historical returns — skipped (pass by default)"
        return check

    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
    std_ret = math.sqrt(variance) if variance > 0 else 0.0

    if std_ret == 0:
        check["detail"] = "zero volatility — suspicious but passing"
        return check

    risk_free = proposal.get("risk_free_rate", 0.0)
    sharpe = (mean_ret - risk_free) / std_ret
    check["sharpe"] = round(sharpe, 4)

    if sharpe < -1.0:
        check["passed"] = False
        check["detail"] = f"sharpe={sharpe:.2f} — deeply negative, reconsider strategy"
    elif sharpe > 5.0:
        check["passed"] = False
        check["detail"] = f"sharpe={sharpe:.2f} — unrealistically high, possible data error"
    else:
        check["detail"] = f"sharpe={sharpe:.2f} — within plausible range"
    return check


def verify_trade_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """
    Run mathematical checks on a trade proposal.

    Expected proposal keys:
        position_size, bankroll, win_probability, win_loss_ratio,
        expected_reward, expected_risk,
        historical_returns (optional list[float]),
        risk_free_rate (optional float)

    Returns:
        {approved: bool, checks: list, vetoed_reason: str}
    """
    checks = [
        _check_kelly_criterion(proposal),
        _check_risk_reward(proposal),
        _check_max_drawdown(proposal),
        _check_sharpe_sanity(proposal),
    ]

    failed = [c for c in checks if not c["passed"]]
    approved = len(failed) == 0
    vetoed_reason = ""
    if not approved:
        reasons = [f"{c['name']}: {c['detail']}" for c in failed]
        vetoed_reason = "; ".join(reasons)

    return VerificationResult(
        approved=approved,
        checks=checks,
        vetoed_reason=vetoed_reason,
    ).to_dict()


# ---------------------------------------------------------------------------
# Code change verification (AST, imports, signatures, safety)
# ---------------------------------------------------------------------------

_DANGEROUS_CODE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("os.system call", re.compile(r"\bos\.system\s*\(")),
    ("subprocess.call with shell=True", re.compile(r"subprocess\.\w+\(.*shell\s*=\s*True", re.S)),
    ("eval() usage", re.compile(r"\beval\s*\(")),
    ("exec() usage", re.compile(r"\bexec\s*\(")),
    ("__import__ usage", re.compile(r"__import__\s*\(")),
    ("pickle.loads (deserialization)", re.compile(r"pickle\.loads?\s*\(")),
    ("shutil.rmtree on root-like path", re.compile(r"shutil\.rmtree\s*\(\s*['\"/]")),
]


def _check_ast_parse(code: str) -> dict[str, Any]:
    """Check that the code is syntactically valid Python."""
    check: dict[str, Any] = {"name": "ast_parse", "passed": False, "detail": ""}
    try:
        ast.parse(code)
        check["passed"] = True
        check["detail"] = "syntax valid"
    except SyntaxError as exc:
        check["detail"] = f"SyntaxError at line {exc.lineno}: {exc.msg}"
    return check


def _extract_imports(tree: ast.AST) -> set[str]:
    """Extract all imported module names from an AST."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
    return modules


def _check_circular_imports(file_path: str, code: str, workspace: Path) -> dict[str, Any]:
    """
    Heuristic circular import check: if the new code imports module X,
    and module X already imports the file being edited, flag it.
    """
    check: dict[str, Any] = {"name": "circular_import_check", "passed": True, "detail": ""}

    try:
        tree = ast.parse(code)
    except SyntaxError:
        check["detail"] = "cannot parse — skipped"
        return check

    new_imports = _extract_imports(tree)
    if not new_imports:
        check["detail"] = "no imports — no circular risk"
        return check

    file_stem = Path(file_path).stem
    warnings: list[str] = []

    for mod_name in new_imports:
        # Look for the module in workspace
        candidate = workspace / f"{mod_name}.py"
        if not candidate.is_file():
            candidate = workspace / mod_name / "__init__.py"
        if not candidate.is_file():
            continue

        try:
            mod_code = candidate.read_text(encoding="utf-8", errors="replace")
            mod_tree = ast.parse(mod_code)
        except (OSError, SyntaxError):
            continue

        mod_imports = _extract_imports(mod_tree)
        if file_stem in mod_imports:
            warnings.append(
                f"{mod_name} already imports '{file_stem}' — "
                f"adding import of '{mod_name}' may create a cycle"
            )

    if warnings:
        check["passed"] = False
        check["detail"] = "; ".join(warnings)
    else:
        check["detail"] = "no circular import risk detected"
    return check


def _extract_function_signatures(tree: ast.AST) -> dict[str, list[str]]:
    """Extract top-level function names and their parameter names."""
    sigs: dict[str, list[str]] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [arg.arg for arg in node.args.args]
            sigs[node.name] = params
    return sigs


def _check_signature_compatibility(file_path: str, code: str) -> dict[str, Any]:
    """
    Check if public function signatures changed in a breaking way
    (removed parameters from existing functions).
    """
    check: dict[str, Any] = {"name": "signature_compatibility", "passed": True, "detail": ""}

    fp = Path(file_path)
    if not fp.is_file():
        check["detail"] = "new file — no compatibility concern"
        return check

    try:
        old_code = fp.read_text(encoding="utf-8", errors="replace")
        old_tree = ast.parse(old_code)
        new_tree = ast.parse(code)
    except (OSError, SyntaxError):
        check["detail"] = "cannot parse old or new — skipped"
        return check

    old_sigs = _extract_function_signatures(old_tree)
    new_sigs = _extract_function_signatures(new_tree)

    breaking: list[str] = []
    for fname, old_params in old_sigs.items():
        if fname.startswith("_"):
            continue  # skip private functions
        if fname not in new_sigs:
            breaking.append(f"public function '{fname}' removed")
            continue
        new_params = new_sigs[fname]
        removed = set(old_params) - set(new_params)
        if removed:
            breaking.append(
                f"'{fname}' lost parameters: {', '.join(sorted(removed))}"
            )

    if breaking:
        check["passed"] = False
        check["detail"] = "; ".join(breaking)
    else:
        check["detail"] = "no breaking signature changes"
    return check


def _check_dangerous_patterns(code: str) -> dict[str, Any]:
    """Scan code for dangerous patterns."""
    check: dict[str, Any] = {"name": "safety_scan", "passed": True, "detail": ""}
    found: list[str] = []

    for label, pattern in _DANGEROUS_CODE_PATTERNS:
        if pattern.search(code):
            found.append(label)

    if found:
        check["passed"] = False
        check["detail"] = f"dangerous patterns: {', '.join(found)}"
    else:
        check["detail"] = "no dangerous patterns detected"
    return check


def verify_code_change(
    file_path: str,
    new_code: str,
    workspace: str | Path,
) -> dict[str, Any]:
    """
    Static verification of a proposed code change.

    Checks: AST parse, circular imports, signature compatibility, safety scan.

    Returns:
        {approved: bool, checks: list, warnings: list}
    """
    workspace = Path(workspace).resolve()

    checks = [
        _check_ast_parse(new_code),
        _check_circular_imports(file_path, new_code, workspace),
        _check_signature_compatibility(file_path, new_code),
        _check_dangerous_patterns(new_code),
    ]

    warnings = [c["detail"] for c in checks if not c["passed"]]
    approved = all(c["passed"] for c in checks)

    return VerificationResult(
        approved=approved,
        checks=checks,
        warnings=warnings,
    ).to_dict()


# ---------------------------------------------------------------------------
# Terminal command verification
# ---------------------------------------------------------------------------

_DESTRUCTIVE_COMMANDS: list[tuple[str, re.Pattern[str]]] = [
    ("recursive force delete", re.compile(r"\brm\s+(-[a-z]*r[a-z]*\s+)?-[a-z]*f", re.I)),
    ("recursive delete root", re.compile(r"\brm\s+.*\s+/\s", re.I)),
    ("drop table", re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.I)),
    ("truncate table", re.compile(r"\bTRUNCATE\s+TABLE\b", re.I)),
    ("kill -9", re.compile(r"\bkill\s+-9\b")),
    ("killall", re.compile(r"\bkillall\b")),
    ("format disk", re.compile(r"\b(mkfs|format)\b", re.I)),
    ("dd write", re.compile(r"\bdd\s+if=.*of=", re.I)),
    ("chmod 777", re.compile(r"\bchmod\s+777\b")),
    ("git force push", re.compile(r"\bgit\s+push\s+.*--force\b", re.I)),
    ("git reset hard", re.compile(r"\bgit\s+reset\s+--hard\b", re.I)),
    ("registry delete", re.compile(r"\breg\s+delete\b", re.I)),
    ("del /s /q", re.compile(r"\bdel\s+/[sq]", re.I)),
    ("rd /s /q", re.compile(r"\brd\s+/s\b", re.I)),
]

_EXFIL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("curl + sensitive path", re.compile(
        r"\bcurl\b.*(/etc/passwd|/etc/shadow|\.ssh/|\.env|credentials|\.aws/)", re.I
    )),
    ("upload sensitive file", re.compile(
        r"\bcurl\b.*(-F|--data-binary|--upload-file).*"
        r"(\.env|id_rsa|credentials|\.aws/|secret)", re.I
    )),
    ("wget post sensitive", re.compile(
        r"\bwget\b.*--post-file.*"
        r"(\.env|id_rsa|credentials|secret)", re.I
    )),
    ("nc/netcat pipe", re.compile(r"\b(nc|netcat)\b.*<\s*\S+", re.I)),
    ("base64 encode and send", re.compile(r"base64.*\|\s*(curl|wget|nc)\b", re.I)),
]

_RESOURCE_BOMB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fork bomb", re.compile(r":\(\)\s*\{.*:\|:.*\}\s*;:")),
    ("fork bomb (bash)", re.compile(r"\bfork\s*bomb\b", re.I)),
    ("infinite while true", re.compile(r"\bwhile\s+(true|1)\s*;\s*do\s*[^d]*done", re.I)),
    ("yes pipe", re.compile(r"\byes\s*\|", re.I)),
    ("dev zero fill", re.compile(r"cat\s+/dev/zero\s*>", re.I)),
    ("python infinite", re.compile(r"python.*while\s+True", re.I)),
]


def verify_terminal_command(command: str) -> dict[str, Any]:
    """
    Check a terminal command for destructive, exfiltration, or resource-bomb patterns.

    Returns:
        {safe: bool, risk_level: str, warnings: list}
    """
    if not command or not command.strip():
        return CommandSafetyResult(safe=True, risk_level="low", warnings=[]).to_dict()

    warnings: list[str] = []
    risk_scores: list[int] = []  # 1=low, 2=medium, 3=high, 4=critical

    for label, pattern in _DESTRUCTIVE_COMMANDS:
        if pattern.search(command):
            warnings.append(f"destructive: {label}")
            risk_scores.append(4)

    for label, pattern in _EXFIL_PATTERNS:
        if pattern.search(command):
            warnings.append(f"exfiltration: {label}")
            risk_scores.append(3)

    for label, pattern in _RESOURCE_BOMB_PATTERNS:
        if pattern.search(command):
            warnings.append(f"resource bomb: {label}")
            risk_scores.append(4)

    if not risk_scores:
        return CommandSafetyResult(safe=True, risk_level="low", warnings=[]).to_dict()

    max_risk = max(risk_scores)
    level_map = {1: "low", 2: "medium", 3: "high", 4: "critical"}
    risk_level = level_map.get(max_risk, "critical")
    safe = max_risk <= 1

    return CommandSafetyResult(
        safe=safe,
        risk_level=risk_level,
        warnings=warnings,
    ).to_dict()


# ---------------------------------------------------------------------------
# Universal gate
# ---------------------------------------------------------------------------

def symbolic_gate(
    action_type: str,
    action_data: dict[str, Any],
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """
    Universal verification gate.  Routes to the appropriate verifier based
    on *action_type*.

    Supported action_types:
        "trade"   -> verify_trade_proposal
        "code"    -> verify_code_change   (needs file_path, new_code in action_data)
        "command" -> verify_terminal_command (needs "command" in action_data)

    Returns the verifier's dict result, or an error dict for unknown types.
    """
    ws = Path(workspace).resolve() if workspace else Path.cwd()

    if action_type == "trade":
        return verify_trade_proposal(action_data)

    if action_type == "code":
        file_path = action_data.get("file_path", "")
        new_code = action_data.get("new_code", "")
        return verify_code_change(file_path, new_code, ws)

    if action_type == "command":
        cmd = action_data.get("command", "")
        result = verify_terminal_command(cmd)
        # Normalize to VerificationResult shape for the gate API
        return {
            "approved": result["safe"],
            "checks": [{"name": "terminal_safety", "passed": result["safe"],
                         "detail": result["risk_level"]}],
            "warnings": result["warnings"],
            "vetoed_reason": "; ".join(result["warnings"]) if not result["safe"] else "",
        }

    return {
        "approved": False,
        "checks": [],
        "warnings": [f"unknown action_type: {action_type!r}"],
        "vetoed_reason": f"unsupported action_type '{action_type}'",
    }
