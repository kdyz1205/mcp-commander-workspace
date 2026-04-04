"""
Tests for the Autopsy (physical verification) system.

These tests verify that the autopsy catches LLM lies:
- Missing files → FAIL
- Empty files → FAIL
- Syntax errors → FAIL
- Real files with valid Python → PASS
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.autopsy import (
    verify_files_exist,
    verify_python_syntax,
    run_autopsy,
)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / ".claw").mkdir()
    return tmp_path


class TestFileVerification:
    def test_missing_file_detected(self, workspace):
        failures = verify_files_exist(workspace, ["ghost.py"])
        assert len(failures) == 1
        assert "FILE_MISSING" in failures[0]

    def test_empty_file_detected(self, workspace):
        (workspace / "empty.py").write_text("", encoding="utf-8")
        failures = verify_files_exist(workspace, ["empty.py"])
        assert len(failures) == 1
        assert "FILE_EMPTY" in failures[0]

    def test_real_file_passes(self, workspace):
        (workspace / "real.py").write_text("x = 1\n", encoding="utf-8")
        failures = verify_files_exist(workspace, ["real.py"])
        assert failures == []


class TestSyntaxVerification:
    def test_syntax_error_caught(self, workspace):
        (workspace / "bad.py").write_text("def foo(\n", encoding="utf-8")
        failures = verify_python_syntax(workspace, ["bad.py"])
        assert len(failures) == 1
        assert "SYNTAX_ERROR" in failures[0]

    def test_valid_python_passes(self, workspace):
        (workspace / "good.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
        failures = verify_python_syntax(workspace, ["good.py"])
        assert failures == []

    def test_non_python_skipped(self, workspace):
        (workspace / "data.json").write_text("{bad json", encoding="utf-8")
        failures = verify_python_syntax(workspace, ["data.json"])
        assert failures == []


class TestFullAutopsy:
    def test_all_pass(self, workspace):
        (workspace / "module.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
        result = run_autopsy(
            workspace,
            expected_files=["module.py"],
            run_tests=False,
            check_git=False,
        )
        assert result.passed is True
        assert result.checks_passed == 2  # file_check + syntax_check
        assert result.failures == []

    def test_missing_file_fails_autopsy(self, workspace):
        result = run_autopsy(
            workspace,
            expected_files=["does_not_exist.py"],
            run_tests=False,
            check_git=False,
        )
        assert result.passed is False
        assert any("FILE_MISSING" in f for f in result.failures)

    def test_syntax_error_fails_autopsy(self, workspace):
        (workspace / "broken.py").write_text("def (\n", encoding="utf-8")
        result = run_autopsy(
            workspace,
            expected_files=["broken.py"],
            run_tests=False,
            check_git=False,
        )
        assert result.passed is False
        assert any("SYNTAX_ERROR" in f for f in result.failures)

    def test_llm_hallucination_scenario(self, workspace):
        """LLM claims it created 3 files. Only 1 actually exists. MUST fail."""
        (workspace / "real.py").write_text("x = 1\n", encoding="utf-8")
        result = run_autopsy(
            workspace,
            expected_files=["real.py", "fake1.py", "fake2.py"],
            run_tests=False,
            check_git=False,
        )
        assert result.passed is False
        assert len(result.failures) == 2  # 2 missing files


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
