"""
Meta-Creator — DevClaw grows new organs at runtime.

When DevClaw encounters a capability it lacks, it writes new Python
code, validates it through AST + sandbox, and hot-loads it.

Pipeline:
1. ast_filter_skill: Syntax + safety check (block os/subprocess/shutil)
2. load_skill_module: Dynamic importlib loading
3. register_generated_skill: Add to skills.json + SKILL.md
4. forge_skill: Full cycle — validate → write → load → register

Contract: every generated skill MUST have def execute(**kwargs).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any


# Forbidden imports in generated skills
_FORBIDDEN_IMPORTS = {"os", "subprocess", "shutil", "sys", "ctypes", "signal"}


# ═══════════════════════════════════════
# Defense 1: AST Filter
# ═══════════════════════════════════════

def ast_filter_skill(code: str) -> tuple[bool, str]:
    """
    Validate skill code: syntax + safety + contract.

    Blocks: dangerous imports, missing execute(), syntax errors.
    """
    # Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e.msg} (line {e.lineno})"

    # Check for forbidden imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_module = alias.name.split(".")[0]
                if root_module in _FORBIDDEN_IMPORTS:
                    return False, f"Forbidden import blocked: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_module = node.module.split(".")[0]
                if root_module in _FORBIDDEN_IMPORTS:
                    return False, f"Forbidden import blocked: {node.module}"

    # Check for dangerous function calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("eval", "exec", "compile", "__import__"):
                    return False, f"Dangerous call blocked: {node.func.id}()"

    # Contract check: must have execute() function
    has_execute = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            has_execute = True
            break

    if not has_execute:
        return False, "Contract violation: missing required execute(**kwargs) function"

    return True, ""


# ═══════════════════════════════════════
# Defense 2: Dynamic Loading
# ═══════════════════════════════════════

def load_skill_module(file_path: str, skill_name: str) -> Any:
    """
    Dynamically load a Python module from file path.

    Returns the module object or None on failure.
    """
    try:
        spec = importlib.util.spec_from_file_location(skill_name, file_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


# ═══════════════════════════════════════
# Defense 3: Registration
# ═══════════════════════════════════════

def register_generated_skill(
    workspace: str | Path,
    skill_name: str,
    skill_dir: str | Path,
) -> bool:
    """Register a skill in skills.json."""
    ws = Path(workspace).resolve()
    sj_path = ws / "skills" / "skills.json"

    # Load existing registry
    registry: list[dict] = []
    if sj_path.is_file():
        try:
            registry = json.loads(sj_path.read_text(encoding="utf-8"))
            if not isinstance(registry, list):
                registry = []
        except (json.JSONDecodeError, OSError):
            registry = []

    # Check for duplicate
    if any(s.get("name") == skill_name for s in registry):
        return True  # Already registered

    # Add entry
    registry.append({
        "name": skill_name,
        "path": str(skill_dir),
        "generated": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })

    try:
        sj_path.parent.mkdir(parents=True, exist_ok=True)
        sj_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


# ═══════════════════════════════════════
# Full Forge Cycle
# ═══════════════════════════════════════

def forge_skill(
    workspace: str | Path,
    skill_name: str,
    skill_code: str,
    skill_description: str = "",
) -> dict[str, Any]:
    """
    Full cycle: validate → write → load → verify → register.

    Returns {success, skill_path, error}.
    """
    ws = Path(workspace).resolve()

    # Step 1: AST filter
    ok, err = ast_filter_skill(skill_code)
    if not ok:
        return {"success": False, "error": err, "phase": "ast_filter"}

    # Step 2: Write to skills directory
    skill_dir = ws / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    runner_path = skill_dir / "runner.py"
    skill_md_path = skill_dir / "SKILL.md"

    try:
        runner_path.write_text(skill_code, encoding="utf-8")
        skill_md_path.write_text(
            f"---\nname: {skill_name}\n"
            f"description: {skill_description or skill_name}\n"
            f"generated: true\n"
            f"created_at: {time.strftime('%Y-%m-%d %H:%M')}\n"
            f"---\n\n{skill_description or 'Auto-generated skill.'}\n",
            encoding="utf-8",
        )
    except OSError as e:
        return {"success": False, "error": f"Write failed: {e}", "phase": "write"}

    # Step 3: Dynamic load test
    module = load_skill_module(str(runner_path), skill_name)
    if module is None:
        # Rollback
        try:
            runner_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"success": False, "error": "Module failed to load", "phase": "load"}

    # Step 4: Verify contract
    if not hasattr(module, "execute"):
        try:
            runner_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"success": False, "error": "Loaded but execute() not found", "phase": "verify"}

    # Step 5: Dry-run execute with empty args
    try:
        test_result = module.execute()
        if not isinstance(test_result, dict):
            return {"success": False, "error": f"execute() returned {type(test_result)}, expected dict", "phase": "dry_run"}
    except Exception as e:
        return {"success": False, "error": f"execute() crashed: {e}", "phase": "dry_run"}

    # Step 6: Register
    register_generated_skill(ws, skill_name, str(skill_dir))

    return {
        "success": True,
        "skill_path": str(runner_path),
        "skill_name": skill_name,
        "phase": "complete",
    }
