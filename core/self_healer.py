"""
SelfHealer — Pain-Driven Self-Healing Engine.

When a skill/tool throws an Exception, this is NOT a "program crash".
This is a SURVIVAL THREAT — something is blocking DevClaw from eating.

Flow:
  1. Skill fails with Exception
  2. SelfHealer wraps the error into a repair task
  3. Claude CLI attempts the fix (reading error + source code)
  4. Autopsy verifies the fix is real (not hallucinated)
  5. Retry the original skill

If the fix succeeds → experience is crystallized (future L1 muscle memory).
If the fix fails 3 times → escalate to human (CRITICAL pain signal).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

from core.autopsy import run_autopsy, verify_python_syntax

log = logging.getLogger("self_healer")

MAX_HEAL_ATTEMPTS = 3
HEAL_TIMEOUT = 180  # 3 min per attempt


@dataclass
class HealAttempt:
    attempt: int
    error_type: str
    error_msg: str
    source_file: str | None
    fix_applied: bool
    autopsy_passed: bool
    timestamp: float


@dataclass
class HealResult:
    healed: bool
    attempts: list[HealAttempt]
    total_attempts: int
    original_error: str
    fix_description: str | None


def extract_source_file(tb_text: str) -> str | None:
    """Extract the most relevant source file from a traceback."""
    import re
    # Find files in the workspace (not stdlib/site-packages)
    matches = re.findall(r'File "([^"]*(?:skills|core|tools|claw_runtime)[^"]*)"', tb_text)
    return matches[-1] if matches else None


def heal(
    workspace: Path,
    error: Exception,
    *,
    context: str = "",
    source_file: str | None = None,
) -> HealResult:
    """
    Attempt to self-heal from an Exception.

    1. Analyze the error + source code
    2. Ask Claude CLI to fix it
    3. Verify with autopsy
    4. Repeat up to MAX_HEAL_ATTEMPTS times
    """
    ws = workspace.resolve()
    tb_text = traceback.format_exception(type(error), error, error.__traceback__)
    tb_str = "".join(tb_text)[-2000:]
    error_type = type(error).__name__
    error_msg = str(error)[:500]

    # Auto-detect source file from traceback
    if not source_file:
        source_file = extract_source_file(tb_str)

    log.warning(
        f"PAIN SIGNAL: {error_type}: {error_msg[:100]} "
        f"| source: {source_file or 'unknown'}"
    )

    attempts: list[HealAttempt] = []
    claude_bin = shutil.which("claude") or "claude"

    for attempt_num in range(1, MAX_HEAL_ATTEMPTS + 1):
        log.info(f"HEAL ATTEMPT {attempt_num}/{MAX_HEAL_ATTEMPTS}")

        # ── Build repair prompt ──
        repair_prompt = (
            f"以下代码抛出了异常，这正在威胁系统的生存。立即修复。\n\n"
            f"【错误类型】{error_type}\n"
            f"【错误信息】{error_msg}\n"
            f"【Traceback】\n{tb_str[-1000:]}\n"
        )
        if source_file:
            repair_prompt += f"\n【出错文件】{source_file}\n"
            repair_prompt += "请读取该文件，找到 Bug，直接修改并保存。\n"
        if context:
            repair_prompt += f"\n【上下文】{context}\n"
        repair_prompt += (
            "\n【规则】\n"
            "1. 直接修改文件，不要只解释\n"
            "2. 修改后运行 python -c 'import ast; ast.parse(open(\"FILE\").read())' 验证语法\n"
            "3. 如果有相关测试，运行测试确认修复\n"
        )

        # ── Run Claude CLI ──
        fix_applied = False
        try:
            r = subprocess.run(
                [claude_bin, "--dangerously-skip-permissions", "-p", repair_prompt],
                capture_output=True, text=True, timeout=HEAL_TIMEOUT,
                cwd=str(ws), encoding="utf-8", errors="replace",
            )
            fix_applied = r.returncode == 0 and bool(r.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            log.error(f"HEAL CLI FAILED: {e}")

        # ── Autopsy: verify the fix is real ──
        autopsy_passed = False
        if fix_applied and source_file:
            # Check the file was actually modified and is valid
            rel_path = source_file
            if ws.as_posix() in source_file or str(ws) in source_file:
                try:
                    rel_path = str(Path(source_file).relative_to(ws))
                except ValueError:
                    pass

            syntax_failures = verify_python_syntax(ws, [rel_path])
            autopsy_passed = len(syntax_failures) == 0

        attempt = HealAttempt(
            attempt=attempt_num,
            error_type=error_type,
            error_msg=error_msg[:200],
            source_file=source_file,
            fix_applied=fix_applied,
            autopsy_passed=autopsy_passed,
            timestamp=time.time(),
        )
        attempts.append(attempt)

        if autopsy_passed:
            log.info(f"HEALED on attempt {attempt_num}!")
            _log_heal(ws, attempts, healed=True)
            return HealResult(
                healed=True,
                attempts=attempts,
                total_attempts=attempt_num,
                original_error=f"{error_type}: {error_msg}",
                fix_description=f"Auto-healed {source_file} on attempt {attempt_num}",
            )

        log.warning(f"HEAL ATTEMPT {attempt_num} FAILED (autopsy: {autopsy_passed})")

    # All attempts exhausted
    log.error(f"HEAL EXHAUSTED after {MAX_HEAL_ATTEMPTS} attempts: {error_type}")
    _log_heal(ws, attempts, healed=False)
    return HealResult(
        healed=False,
        attempts=attempts,
        total_attempts=MAX_HEAL_ATTEMPTS,
        original_error=f"{error_type}: {error_msg}",
        fix_description=None,
    )


def self_healing_wrapper(func):
    """
    Decorator: wrap any function with auto-heal on failure.

    Usage:
        @self_healing_wrapper
        def run_trading_scan(workspace):
            ...

    If run_trading_scan raises, SelfHealer tries to fix the source code
    and retries automatically.
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        workspace = None
        # Try to extract workspace from args
        for a in args:
            if isinstance(a, Path):
                workspace = a
                break
        if workspace is None:
            workspace = kwargs.get("workspace", Path("."))

        try:
            return func(*args, **kwargs)
        except Exception as e:
            result = heal(workspace, e, context=f"Function: {func.__name__}")
            if result.healed:
                # Retry after successful heal
                log.info(f"Retrying {func.__name__} after successful heal")
                return func(*args, **kwargs)
            else:
                # Re-raise with heal context
                raise RuntimeError(
                    f"Self-heal failed for {func.__name__} after "
                    f"{result.total_attempts} attempts: {result.original_error}"
                ) from e

    return wrapper


def _log_heal(workspace: Path, attempts: list[HealAttempt], *, healed: bool) -> None:
    """Persist heal attempts to disk for learning."""
    log_path = workspace / ".claw" / "heal_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "healed": healed,
        "attempts": [asdict(a) for a in attempts],
        "ts": time.time(),
    }
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
