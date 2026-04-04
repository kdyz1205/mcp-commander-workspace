"""
TDD Execution Engine — The absolute heart of DevClaw's evolution.

No code enters DevClaw's body without passing through this engine.
The LLM is a "guessing calculator". This engine is the judge.

Pipeline:
1. ast_validate: Syntax + safety check in memory (no execution)
2. run_test_subprocess: Physical pytest with timeout
3. run_tdd_cycle: Full cycle — write files, run test, return verdict

The LLM has NO say in whether code passes. Only Exit Code 0 counts.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════
# Defense 1: AST Gatekeeper
# ═══════════════════════════════════════

# Dangerous function calls that must NEVER appear in generated code
_DANGEROUS_CALLS = {
    "os.system", "os.popen", "os.exec", "os.execv", "os.execve",
    "subprocess.call", "subprocess.Popen",  # allow subprocess.run with timeout
    "eval", "exec", "compile",
    "shutil.rmtree",
    "__import__",
}

_DANGEROUS_ATTRS = {
    "system", "popen", "rmtree",
}


class _DangerousCallVisitor(ast.NodeVisitor):
    """AST visitor that flags dangerous function calls."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        # Check direct calls like eval(), exec()
        if isinstance(node.func, ast.Name):
            if node.func.id in ("eval", "exec", "compile", "__import__"):
                self.violations.append(f"Blocked dangerous call: {node.func.id}()")

        # Check attribute calls like os.system()
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in _DANGEROUS_ATTRS:
                self.violations.append(f"Blocked dangerous call: *.{attr}()")

            # Check full dotted name like os.system
            if isinstance(node.func.value, ast.Name):
                full = f"{node.func.value.id}.{attr}"
                if full in _DANGEROUS_CALLS:
                    self.violations.append(f"Blocked dangerous call: {full}()")

        self.generic_visit(node)


def ast_validate(code: str) -> tuple[bool, str]:
    """
    Validate code in memory without executing.

    Returns (True, "") if valid and safe.
    Returns (False, error_message) if syntax error or dangerous call found.
    """
    # Step 1: Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"

    # Step 2: Safety check
    visitor = _DangerousCallVisitor()
    visitor.visit(tree)
    if visitor.violations:
        return False, f"Blocked dangerous code: {'; '.join(visitor.violations)}"

    return True, ""


# ═══════════════════════════════════════
# Defense 2: Subprocess Execution
# ═══════════════════════════════════════

def run_test_subprocess(
    test_file: str,
    timeout: int = 15,
) -> dict[str, Any]:
    """
    Run pytest in a subprocess with hard timeout.

    Returns {exit_code, passed, error_log, duration_sec}
    The LLM has ZERO input into this verdict. Only exit_code matters.
    """
    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "--maxfail=1",
             "--disable-warnings", "--tb=short", "-q"],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - start
        output = (result.stdout or "") + (result.stderr or "")

        # Truncate to last 50 lines
        lines = output.strip().split("\n")
        truncated = "\n".join(lines[-50:])

        return {
            "exit_code": result.returncode,
            "passed": result.returncode == 0,
            "error_log": truncated if result.returncode != 0 else "",
            "duration_sec": round(elapsed, 2),
        }

    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "passed": False,
            "error_log": f"TIMEOUT: Code execution exceeded {timeout}s — possible infinite loop",
            "duration_sec": timeout,
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "passed": False,
            "error_log": f"Execution error: {e!s}",
            "duration_sec": time.time() - start,
        }


# ═══════════════════════════════════════
# Defense 3: Full TDD Cycle
# ═══════════════════════════════════════

def run_tdd_cycle(
    workspace: str | Path,
    test_code: str,
    impl_code: str,
    impl_filename: str = "impl.py",
    test_filename: str = "test_generated.py",
    timeout: int = 15,
) -> dict[str, Any]:
    """
    Full TDD cycle: validate → write → run → verdict.

    1. AST validate both test and implementation
    2. Write to workspace
    3. Run pytest
    4. Return binary verdict

    The LLM calls this. It cannot override the verdict.
    """
    ws = Path(workspace).resolve()

    # AST gate — test code
    ok, err = ast_validate(test_code)
    if not ok:
        return {"success": False, "exit_code": -1,
                "error": f"Test code AST failure: {err}", "phase": "ast_test"}

    # AST gate — implementation code
    ok, err = ast_validate(impl_code)
    if not ok:
        return {"success": False, "exit_code": -1,
                "error": f"Implementation AST failure: {err}", "phase": "ast_impl"}

    # Write files
    test_path = ws / test_filename
    impl_path = ws / impl_filename
    try:
        impl_path.write_text(impl_code, encoding="utf-8")
        test_path.write_text(test_code, encoding="utf-8")
    except OSError as e:
        return {"success": False, "exit_code": -1,
                "error": f"File write error: {e}", "phase": "write"}

    # Run pytest
    result = run_test_subprocess(str(test_path), timeout=timeout)

    # Clean up test file (impl stays if passed)
    try:
        test_path.unlink(missing_ok=True)
        if not result["passed"]:
            impl_path.unlink(missing_ok=True)
    except OSError:
        pass

    return {
        "success": result["passed"],
        "exit_code": result["exit_code"],
        "error": result.get("error_log", ""),
        "duration_sec": result.get("duration_sec", 0),
        "phase": "pytest",
    }


# ═══════════════════════════════════════
# The Torture Loop (used by autonomous_engineer)
# ═══════════════════════════════════════

def torture_loop(
    workspace: str | Path,
    task_description: str,
    generate_fn,  # Callable[[str, str], tuple[str, str]]  — (test_code, impl_code)
    max_retries: int = 5,
    timeout: int = 15,
) -> dict[str, Any]:
    """
    The loop that forces the LLM to keep trying until tests pass.

    generate_fn(task, error_feedback) -> (test_code, impl_code)

    The LLM cannot escape this loop except by writing correct code.
    """
    ws = Path(workspace).resolve()
    error_feedback = ""

    for attempt in range(max_retries):
        # Ask LLM to generate code
        test_code, impl_code = generate_fn(task_description, error_feedback)

        # Run TDD cycle
        result = run_tdd_cycle(
            ws, test_code, impl_code,
            impl_filename=f"impl_attempt_{attempt}.py",
            timeout=timeout,
        )

        if result["success"]:
            return {
                "success": True,
                "attempts": attempt + 1,
                "impl_code": impl_code,
                "test_code": test_code,
            }

        # Extract error for next attempt
        error_feedback = result.get("error", "Unknown error")

    return {
        "success": False,
        "attempts": max_retries,
        "last_error": error_feedback,
    }
