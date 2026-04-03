"""
Predictive sandbox: world-model pre-simulation before dangerous actions.

Before executing a potentially harmful action, simulate its outcome
in a "mental sandbox" — predict file-system changes, process effects,
network effects, and import-graph impact — without actually running
the action.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Terminal command prediction
# ---------------------------------------------------------------------------

# Map of command prefixes to predicted effect categories
_CMD_EFFECTS: list[tuple[re.Pattern[str], dict[str, Any]]] = [
    # --- file destructive ---
    (re.compile(r"^\s*rm\s"), {"category": "file_delete", "risk": 8, "reversible": False}),
    (re.compile(r"^\s*del\s", re.I), {"category": "file_delete", "risk": 7, "reversible": False}),
    (re.compile(r"^\s*rd\s", re.I), {"category": "dir_delete", "risk": 8, "reversible": False}),
    (re.compile(r"^\s*rmdir\s"), {"category": "dir_delete", "risk": 6, "reversible": False}),
    (re.compile(r"^\s*shred\s"), {"category": "file_destroy", "risk": 10, "reversible": False}),

    # --- file create/modify ---
    (re.compile(r"^\s*touch\s"), {"category": "file_create", "risk": 1, "reversible": True}),
    (re.compile(r"^\s*mkdir\s"), {"category": "dir_create", "risk": 1, "reversible": True}),
    (re.compile(r"^\s*cp\s"), {"category": "file_copy", "risk": 2, "reversible": True}),
    (re.compile(r"^\s*mv\s"), {"category": "file_move", "risk": 4, "reversible": True}),
    (re.compile(r"^\s*echo\s.*>"), {"category": "file_write", "risk": 3, "reversible": False}),
    (re.compile(r"^\s*tee\s"), {"category": "file_write", "risk": 3, "reversible": False}),

    # --- process ---
    (re.compile(r"^\s*kill\s"), {"category": "process_kill", "risk": 6, "reversible": False}),
    (re.compile(r"^\s*killall\s"), {"category": "process_kill", "risk": 7, "reversible": False}),
    (re.compile(r"^\s*pkill\s"), {"category": "process_kill", "risk": 7, "reversible": False}),
    (re.compile(r"^\s*(python|node|npm|cargo|go)\s"), {"category": "process_start", "risk": 3, "reversible": True}),

    # --- network ---
    (re.compile(r"^\s*curl\s"), {"category": "network_request", "risk": 3, "reversible": True}),
    (re.compile(r"^\s*wget\s"), {"category": "network_download", "risk": 3, "reversible": True}),
    (re.compile(r"^\s*ssh\s"), {"category": "remote_connect", "risk": 5, "reversible": True}),
    (re.compile(r"^\s*scp\s"), {"category": "remote_copy", "risk": 5, "reversible": True}),
    (re.compile(r"^\s*rsync\s"), {"category": "remote_sync", "risk": 5, "reversible": False}),

    # --- git ---
    (re.compile(r"^\s*git\s+push"), {"category": "git_push", "risk": 5, "reversible": False}),
    (re.compile(r"^\s*git\s+reset\s+--hard"), {"category": "git_reset_hard", "risk": 8, "reversible": False}),
    (re.compile(r"^\s*git\s+checkout\s"), {"category": "git_checkout", "risk": 3, "reversible": True}),
    (re.compile(r"^\s*git\s+commit"), {"category": "git_commit", "risk": 2, "reversible": True}),
    (re.compile(r"^\s*git\s+add"), {"category": "git_stage", "risk": 1, "reversible": True}),
    (re.compile(r"^\s*git\s+stash"), {"category": "git_stash", "risk": 2, "reversible": True}),

    # --- package management ---
    (re.compile(r"^\s*pip\s+install"), {"category": "package_install", "risk": 4, "reversible": True}),
    (re.compile(r"^\s*pip\s+uninstall"), {"category": "package_uninstall", "risk": 5, "reversible": True}),
    (re.compile(r"^\s*npm\s+install"), {"category": "package_install", "risk": 4, "reversible": True}),
    (re.compile(r"^\s*npm\s+uninstall"), {"category": "package_uninstall", "risk": 5, "reversible": True}),

    # --- system ---
    (re.compile(r"^\s*chmod\s"), {"category": "permission_change", "risk": 5, "reversible": True}),
    (re.compile(r"^\s*chown\s"), {"category": "ownership_change", "risk": 5, "reversible": True}),
    (re.compile(r"^\s*sudo\s"), {"category": "elevated_execution", "risk": 7, "reversible": False}),
    (re.compile(r"^\s*dd\s"), {"category": "block_device_write", "risk": 10, "reversible": False}),
]


def _extract_paths_from_command(command: str) -> list[str]:
    """Best-effort extraction of file paths from a shell command."""
    paths: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for token in tokens:
        # Skip flags
        if token.startswith("-"):
            continue
        # Heuristic: looks like a path if it has a slash or dot-extension
        if "/" in token or "\\" in token or (
            "." in token and not token.startswith("-")
        ):
            paths.append(token)
    return paths


def predict_terminal_outcome(
    command: str,
    workspace: str | Path,
) -> dict[str, Any]:
    """
    Predict what a terminal command will do without executing it.

    Returns:
        {predicted_effects: list, risk_score: int (0-10), reversible: bool}
    """
    ws = Path(workspace).resolve()
    effects: list[dict[str, Any]] = []
    max_risk = 0
    all_reversible = True

    # Match against known command patterns
    for pattern, info in _CMD_EFFECTS:
        if pattern.search(command):
            effect = dict(info)
            effect["matched_command"] = command.strip()[:120]
            effects.append(effect)
            max_risk = max(max_risk, info["risk"])
            if not info["reversible"]:
                all_reversible = False

    # Extract referenced paths and annotate
    paths = _extract_paths_from_command(command)
    for p in paths[:10]:
        resolved = (ws / p) if not os.path.isabs(p) else Path(p)
        effects.append({
            "category": "path_reference",
            "path": str(resolved),
            "exists": resolved.exists(),
            "is_dir": resolved.is_dir() if resolved.exists() else None,
        })

    # Detect piped commands (compound risk)
    if "|" in command:
        pipe_stages = [s.strip() for s in command.split("|")]
        effects.append({
            "category": "piped_command",
            "stages": len(pipe_stages),
            "note": "compound command — each stage may have independent effects",
        })
        max_risk = min(max_risk + 1, 10)

    if not effects:
        effects.append({"category": "unknown", "note": "command not recognized"})

    return {
        "predicted_effects": effects,
        "risk_score": max_risk,
        "reversible": all_reversible,
    }


# ---------------------------------------------------------------------------
# File edit impact prediction
# ---------------------------------------------------------------------------

def _find_python_files(workspace: Path, *, max_files: int = 500) -> list[Path]:
    """Collect Python files in the workspace (non-recursing into .git, node_modules, __pycache__)."""
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache"}
    result: list[Path] = []
    stack = [workspace]
    while stack and len(result) < max_files:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in skip_dirs:
                    stack.append(entry)
            elif entry.suffix == ".py":
                result.append(entry)
    return result


def _extract_imports_from_file(path: Path) -> set[str]:
    """Extract imported module names from a Python file."""
    try:
        code = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(code)
    except (OSError, SyntaxError):
        return set()

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


def predict_file_edit_impact(
    file_path: str,
    new_content: str,
    workspace: str | Path,
) -> dict[str, Any]:
    """
    Predict the impact of editing a file.

    Checks:
        - Which other files import this one?
        - What tests might break?

    Returns:
        {affected_files: list, test_files: list, breaking_risk: str}
    """
    ws = Path(workspace).resolve()
    target = Path(file_path)

    # Determine the module name that other files might import
    try:
        rel = target.resolve().relative_to(ws)
    except ValueError:
        rel = target

    # Build possible import names for this file
    stem = target.stem
    import_names: set[str] = {stem}
    # dotted path: claw_runtime.foo -> "claw_runtime.foo"
    parts = list(rel.with_suffix("").parts)
    if parts:
        import_names.add(".".join(parts))
        # Also partial: just the last two
        if len(parts) >= 2:
            import_names.add(".".join(parts[-2:]))

    # Scan workspace for files that import the target
    py_files = _find_python_files(ws)
    affected: list[str] = []
    test_files: list[str] = []

    for pf in py_files:
        if pf.resolve() == target.resolve():
            continue
        imported = _extract_imports_from_file(pf)
        for imp in imported:
            if any(name in imp for name in import_names):
                rel_path = str(pf.relative_to(ws))
                affected.append(rel_path)
                if pf.name.startswith("test_") or "/tests/" in str(pf) or "\\tests\\" in str(pf):
                    test_files.append(rel_path)
                break

    # Determine breaking risk
    # Check if new_content is valid Python (if it looks like Python)
    syntax_ok = True
    if file_path.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError:
            syntax_ok = False

    if not syntax_ok:
        breaking_risk = "high"
    elif len(affected) > 10:
        breaking_risk = "high"
    elif len(affected) > 3:
        breaking_risk = "medium"
    elif affected:
        breaking_risk = "low"
    else:
        breaking_risk = "minimal"

    return {
        "affected_files": affected,
        "test_files": test_files,
        "breaking_risk": breaking_risk,
    }


# ---------------------------------------------------------------------------
# Mental sandbox (unified pre-simulation)
# ---------------------------------------------------------------------------

def run_in_mental_sandbox(
    action_type: str,
    action_data: dict[str, Any],
    workspace: str | Path,
) -> dict[str, Any]:
    """
    Pre-simulate an action without executing it.

    Supported action_types:
        "command"  -> predict terminal outcome
        "file_edit" -> predict file edit impact
        "trade"    -> run through neuro_symbolic_verifier

    Returns:
        {proceed: bool, predictions: dict, warnings: list}
    """
    ws = Path(workspace).resolve()
    warnings: list[str] = []
    predictions: dict[str, Any] = {}
    proceed = True

    if action_type == "command":
        cmd = action_data.get("command", "")
        predictions = predict_terminal_outcome(cmd, ws)
        if predictions["risk_score"] >= 7:
            proceed = False
            warnings.append(
                f"High risk command (score={predictions['risk_score']}/10). "
                "Manual review recommended."
            )
        if not predictions["reversible"]:
            warnings.append("Action is not easily reversible.")

    elif action_type == "file_edit":
        file_path = action_data.get("file_path", "")
        new_content = action_data.get("new_content", "")
        predictions = predict_file_edit_impact(file_path, new_content, ws)
        if predictions["breaking_risk"] in ("high",):
            proceed = False
            warnings.append(
                f"Breaking risk is '{predictions['breaking_risk']}' — "
                f"{len(predictions['affected_files'])} files affected."
            )
        if predictions["test_files"]:
            warnings.append(
                f"{len(predictions['test_files'])} test file(s) may need updating."
            )

    elif action_type == "trade":
        # Delegate to neuro_symbolic_verifier
        try:
            from claw_runtime.neuro_symbolic_verifier import verify_trade_proposal
            predictions = verify_trade_proposal(action_data)
            proceed = predictions.get("approved", False)
            if not proceed:
                warnings.append(
                    f"Trade vetoed: {predictions.get('vetoed_reason', 'unknown')}"
                )
        except ImportError:
            predictions = {"error": "neuro_symbolic_verifier not available"}
            proceed = False
            warnings.append("Cannot verify trade — verifier module missing.")

    else:
        proceed = False
        warnings.append(f"Unknown action_type: {action_type!r}")

    return {
        "proceed": proceed,
        "predictions": predictions,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Dry-run command execution
# ---------------------------------------------------------------------------

# Commands that support --dry-run natively
_DRY_RUN_COMMANDS: dict[str, list[str]] = {
    "git push": ["--dry-run"],
    "git merge": ["--no-commit", "--no-ff"],
    "git pull": ["--dry-run"],
    "git clean": ["--dry-run"],
    "pip install": ["--dry-run"],
    "pip uninstall": ["--dry-run"],
    "npm install": ["--dry-run"],
    "npm uninstall": ["--dry-run"],
    "rsync": ["--dry-run"],
    "make": ["--dry-run"],
}


def _find_dry_run_variant(command: str) -> tuple[str | None, str]:
    """
    If the command supports --dry-run, return the modified command.
    Returns (modified_command_or_None, explanation).
    """
    stripped = command.strip()
    for prefix, flags in _DRY_RUN_COMMANDS.items():
        if stripped.startswith(prefix):
            return stripped + " " + " ".join(flags), f"added {' '.join(flags)}"
    return None, ""


def _simulate_rm(command: str, workspace: Path) -> dict[str, Any]:
    """For rm/del commands, list what would be deleted without actually deleting."""
    paths = _extract_paths_from_command(command)
    would_delete: list[str] = []
    for p in paths:
        resolved = (workspace / p) if not os.path.isabs(p) else Path(p)
        if resolved.exists():
            if resolved.is_dir():
                try:
                    count = sum(1 for _ in resolved.rglob("*"))
                except OSError:
                    count = -1
                would_delete.append(f"directory {resolved} ({count} items)")
            else:
                would_delete.append(f"file {resolved}")
        else:
            would_delete.append(f"(does not exist) {resolved}")

    return {
        "would_happen": f"Would delete: {', '.join(would_delete) or 'nothing identified'}",
        "safe_to_execute": False,
        "items": would_delete,
    }


def dry_run_command(
    command: str,
    workspace: str | Path,
    timeout: int = 10,
) -> dict[str, Any]:
    """
    Execute a command in a restricted/dry-run way.

    Strategy:
        - For git/pip/npm: add --dry-run flag and execute
        - For rm/del: list what would be deleted (no execution)
        - For others: return prediction only (no execution)

    Returns:
        {would_happen: str, safe_to_execute: bool}
    """
    ws = Path(workspace).resolve()
    stripped = command.strip()

    # --- rm/del: simulate without executing ---
    if re.match(r"^\s*(rm|del)\s", stripped, re.I):
        return _simulate_rm(command, ws)

    # --- dry-run capable commands: execute with flag ---
    dry_cmd, explanation = _find_dry_run_variant(stripped)
    if dry_cmd is not None:
        try:
            result = subprocess.run(
                dry_cmd,
                shell=True,
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return {
                "would_happen": output.strip()[:3000] or "(no output)",
                "safe_to_execute": result.returncode == 0,
                "dry_run_modification": explanation,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "would_happen": f"dry-run timed out after {timeout}s",
                "safe_to_execute": False,
            }
        except OSError as exc:
            return {
                "would_happen": f"dry-run failed: {exc}",
                "safe_to_execute": False,
            }

    # --- everything else: prediction only ---
    prediction = predict_terminal_outcome(command, ws)
    return {
        "would_happen": (
            f"Predicted effects: "
            f"{', '.join(e.get('category', '?') for e in prediction['predicted_effects'])}. "
            f"Risk score: {prediction['risk_score']}/10."
        ),
        "safe_to_execute": prediction["risk_score"] <= 3,
        "note": "no dry-run available — prediction only",
    }
