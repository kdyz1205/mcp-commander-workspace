"""
Tests for Pain-Driven Self-Healing Engine.

Verifies that exceptions trigger repair attempts,
and that the healing flow works end-to-end.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.self_healer import extract_source_file, heal, HealResult


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / ".claw").mkdir()
    (tmp_path / "core").mkdir()
    (tmp_path / "skills").mkdir()
    return tmp_path


class TestTracebackExtraction:
    def test_extracts_workspace_file(self):
        tb = '''Traceback (most recent call last):
  File "/usr/lib/python3.14/threading.py", line 1024, in run
    self._target()
  File "c:/workspace/skills/sk_trade_executor/executor.py", line 42, in execute
    raise ValueError("bad")
ValueError: bad'''
        result = extract_source_file(tb)
        assert result is not None
        assert "skills" in result

    def test_ignores_stdlib_files(self):
        tb = '''Traceback:
  File "/usr/lib/python3.14/json/__init__.py", line 10
JSONDecodeError: bad json'''
        result = extract_source_file(tb)
        assert result is None

    def test_picks_last_workspace_file(self):
        tb = '''File "core/supervisor.py", line 10
  File "skills/runner.py", line 20
  File "core/autopsy.py", line 30'''
        result = extract_source_file(tb)
        assert "autopsy" in result


class TestHealResult:
    def test_heal_returns_result_structure(self, workspace):
        """Even if Claude CLI isn't available, heal should return a structured result."""
        try:
            raise ValueError("test error for healing")
        except ValueError as e:
            result = heal(workspace, e, context="unit test")

        assert isinstance(result, HealResult)
        assert result.original_error.startswith("ValueError")
        assert result.total_attempts >= 1
        # Without Claude CLI, healing won't succeed
        # But the structure should be valid
        assert isinstance(result.attempts, list)

    def test_heal_logs_to_disk(self, workspace):
        """Heal attempts must be persisted to .claw/heal_log.jsonl."""
        try:
            raise RuntimeError("disk persistence test")
        except RuntimeError as e:
            heal(workspace, e)

        log_path = workspace / ".claw" / "heal_log.jsonl"
        assert log_path.is_file()
        content = log_path.read_text(encoding="utf-8")
        assert "disk persistence test" in content


class TestSelfHealingWrapper:
    def test_wrapper_catches_and_attempts_heal(self, workspace):
        """The decorator should catch exceptions and attempt healing."""
        from core.self_healer import self_healing_wrapper

        call_count = 0

        @self_healing_wrapper
        def flaky_function(workspace):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("first call fails")
            return "success"

        # With or without Claude CLI, the wrapper should handle the error
        # It either heals+retries (success) or raises RuntimeError
        try:
            result = flaky_function(workspace)
            # If Claude CLI is available and healed, retry succeeded
            assert call_count >= 2
        except RuntimeError:
            # Claude CLI unavailable or heal failed
            assert call_count == 1

        # Either way, heal log should exist
        log_path = workspace / ".claw" / "heal_log.jsonl"
        assert log_path.is_file()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
