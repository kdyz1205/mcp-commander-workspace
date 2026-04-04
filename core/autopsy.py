"""
Autopsy — 物理验尸系统。

不相信 LLM 的自述。只相信磁盘上的字节。

每次 LLM 声称完成任务后，Autopsy 执行物理验证：
1. 文件是否真的存在？
2. 文件是否非空？
3. Python 文件语法能否通过 AST 编译？
4. git diff 是否有真实改动？
5. 测试是否真的通过？

只有全部通过，任务才算 SUCCESS。否则强制标记 FAILED 并记录证据。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("autopsy")


@dataclass
class AutopsyResult:
    """Physical verification result — no LLM involved."""
    passed: bool
    checks_run: int
    checks_passed: int
    failures: list[str]
    evidence: dict[str, str]   # check_name → detail
    timestamp: float


def verify_files_exist(workspace: Path, files: list[str]) -> list[str]:
    """Check that files physically exist and are non-empty."""
    failures = []
    for f in files:
        p = workspace / f
        if not p.is_file():
            failures.append(f"FILE_MISSING: {f}")
        elif p.stat().st_size == 0:
            failures.append(f"FILE_EMPTY: {f} (0 bytes)")
    return failures


def verify_python_syntax(workspace: Path, files: list[str]) -> list[str]:
    """Parse Python files through AST. Catches syntax errors LLM might introduce."""
    failures = []
    for f in files:
        if not f.endswith(".py"):
            continue
        p = workspace / f
        if not p.is_file():
            continue
        try:
            source = p.read_text(encoding="utf-8")
            ast.parse(source, filename=f)
        except SyntaxError as e:
            failures.append(f"SYNTAX_ERROR: {f} line {e.lineno}: {e.msg}")
    return failures


def verify_git_diff(workspace: Path) -> tuple[bool, str]:
    """Check if git has real changes (not just LLM claiming it changed files)."""
    try:
        r = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(workspace), encoding="utf-8", errors="replace",
        )
        diff = r.stdout.strip()
        has_changes = bool(diff)
        return has_changes, diff[:500]
    except Exception as e:
        return False, f"git diff failed: {e}"


def verify_tests_pass(workspace: Path, test_path: str = "tests/") -> tuple[bool, str]:
    """Run pytest physically and check exit code. Don't trust LLM's '13/13 PASSED'."""
    try:
        r = subprocess.run(
            ["python", "-m", "pytest", test_path, "-q", "--tb=short"],
            capture_output=True, text=True, timeout=120,
            cwd=str(workspace), encoding="utf-8", errors="replace",
        )
        output = r.stdout.strip()[-500:]
        passed = r.returncode == 0
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT: tests took > 120s"
    except Exception as e:
        return False, f"TEST_RUN_ERROR: {e}"


def run_autopsy(
    workspace: Path,
    *,
    expected_files: list[str] | None = None,
    run_tests: bool = True,
    test_path: str = "tests/",
    check_git: bool = True,
) -> AutopsyResult:
    """
    Full physical verification. Call this AFTER LLM claims task completion.

    Returns AutopsyResult with pass/fail and hard evidence.
    """
    failures: list[str] = []
    evidence: dict[str, str] = {}
    checks_run = 0
    checks_passed = 0

    # ── Check 1: Files exist and non-empty ──
    if expected_files:
        checks_run += 1
        file_failures = verify_files_exist(workspace, expected_files)
        if file_failures:
            failures.extend(file_failures)
            evidence["file_check"] = "; ".join(file_failures)
        else:
            checks_passed += 1
            evidence["file_check"] = f"OK: {len(expected_files)} files verified"

    # ── Check 2: Python syntax ──
    py_files = [f for f in (expected_files or []) if f.endswith(".py")]
    if py_files:
        checks_run += 1
        syntax_failures = verify_python_syntax(workspace, py_files)
        if syntax_failures:
            failures.extend(syntax_failures)
            evidence["syntax_check"] = "; ".join(syntax_failures)
        else:
            checks_passed += 1
            evidence["syntax_check"] = f"OK: {len(py_files)} files parse cleanly"

    # ── Check 3: Git diff ──
    if check_git:
        checks_run += 1
        has_diff, diff_text = verify_git_diff(workspace)
        evidence["git_diff"] = diff_text or "(no changes)"
        if has_diff:
            checks_passed += 1
        # Not having a diff isn't always a failure (task might not need code changes)

    # ── Check 4: Tests pass ──
    if run_tests:
        checks_run += 1
        test_dir = workspace / test_path
        if test_dir.exists():
            tests_ok, test_output = verify_tests_pass(workspace, test_path)
            evidence["test_run"] = test_output
            if tests_ok:
                checks_passed += 1
            else:
                failures.append(f"TESTS_FAILED: {test_output[-200:]}")
        else:
            evidence["test_run"] = f"SKIP: {test_path} not found"

    passed = len(failures) == 0

    result = AutopsyResult(
        passed=passed,
        checks_run=checks_run,
        checks_passed=checks_passed,
        failures=failures,
        evidence=evidence,
        timestamp=time.time(),
    )

    # ── Persist autopsy log ──
    _log_autopsy(workspace, result)

    level = logging.INFO if passed else logging.ERROR
    log.log(level, f"AUTOPSY: {'PASS' if passed else 'FAIL'} ({checks_passed}/{checks_run})")
    for f in failures:
        log.error(f"  FAILURE: {f}")

    return result


def _log_autopsy(workspace: Path, result: AutopsyResult) -> None:
    """Append autopsy result to physical log file."""
    log_path = workspace / ".claw" / "autopsy_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
    except Exception:
        pass
