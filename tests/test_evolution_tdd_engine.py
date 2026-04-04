"""
TDD Test: The TDD Execution Engine itself.

Tests that the engine can:
1. Force LLM to output structured code+test via tool schema
2. AST-validate generated code before execution
3. Run pytest in subprocess with timeout
4. Loop on failure with error feedback
5. Commit on success
"""
import ast
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_ast_gatekeeper_rejects_bad_syntax():
    """AST parser must reject code with syntax errors."""
    from claw_runtime.tdd_engine import ast_validate

    ok, err = ast_validate("def foo(\n  x = 1")  # broken
    assert not ok
    assert "SyntaxError" in err or "syntax" in err.lower()


def test_ast_gatekeeper_accepts_good_code():
    """AST parser must accept valid Python."""
    from claw_runtime.tdd_engine import ast_validate

    ok, err = ast_validate("def foo():\n    return 42\n")
    assert ok
    assert err == ""


def test_ast_gatekeeper_blocks_dangerous_calls():
    """AST must block os.system, eval, exec in generated code."""
    from claw_runtime.tdd_engine import ast_validate

    ok, err = ast_validate("import os\nos.system('rm -rf /')")
    assert not ok
    assert "dangerous" in err.lower() or "blocked" in err.lower()


def test_run_pytest_in_subprocess_passes():
    """Subprocess must run pytest and return exit code 0 for passing test."""
    from claw_runtime.tdd_engine import run_test_subprocess

    with tempfile.TemporaryDirectory() as td:
        # Write a passing test
        test_file = os.path.join(td, "test_ok.py")
        with open(test_file, "w") as f:
            f.write("def test_add():\n    assert 1 + 1 == 2\n")

        result = run_test_subprocess(test_file, timeout=10)
        assert result["exit_code"] == 0
        assert result["passed"] is True


def test_run_pytest_in_subprocess_fails():
    """Subprocess must catch test failure and return logs."""
    from claw_runtime.tdd_engine import run_test_subprocess

    with tempfile.TemporaryDirectory() as td:
        test_file = os.path.join(td, "test_fail.py")
        with open(test_file, "w") as f:
            f.write("def test_bad():\n    assert 1 == 2\n")

        result = run_test_subprocess(test_file, timeout=10)
        assert result["exit_code"] != 0
        assert result["passed"] is False
        assert len(result["error_log"]) > 0


def test_run_pytest_timeout_kills_infinite_loop():
    """Subprocess must kill infinite loops within timeout."""
    from claw_runtime.tdd_engine import run_test_subprocess

    with tempfile.TemporaryDirectory() as td:
        test_file = os.path.join(td, "test_hang.py")
        with open(test_file, "w") as f:
            f.write("import time\ndef test_hang():\n    time.sleep(100)\n")

        result = run_test_subprocess(test_file, timeout=3)
        assert result["passed"] is False
        assert "timeout" in result["error_log"].lower() or "超时" in result["error_log"]


def test_full_tdd_cycle_with_simple_task():
    """Full TDD cycle: generate code → AST check → run test → pass."""
    from claw_runtime.tdd_engine import run_tdd_cycle

    with tempfile.TemporaryDirectory() as td:
        # Provide pre-written code and test (skip LLM for unit test)
        test_code = "def test_add():\n    from impl import add\n    assert add(2, 3) == 5\n"
        impl_code = "def add(a, b):\n    return a + b\n"

        result = run_tdd_cycle(
            workspace=td,
            test_code=test_code,
            impl_code=impl_code,
            impl_filename="impl.py",
        )
        assert result["success"] is True
        assert result["exit_code"] == 0
