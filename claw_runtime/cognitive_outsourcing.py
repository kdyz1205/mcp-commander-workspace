"""
Cognitive Outsourcing Engine — DevClaw's "Foreman Brain".

DevClaw doesn't just use its own brain. It discovers and leverages
OTHER brains in the environment: Claude CLI, Cursor, GitHub Copilot,
Ollama models, etc.

This is the highest form of intelligence: using the cheapest local
compute to orchestrate the most powerful external brains — for FREE.

Architecture:
1. Environment Probe: Detect available AI CLI tools at startup
2. Capability Matrix: Map each tool to task types it handles best
3. Task Router: Route complex tasks to the best available brain
4. Delegation Protocol: Send task → Monitor → Verify → Accept/Retry
5. Cost Optimizer: Always prefer free tools over paid APIs

Philosophy: "I am not a coder. I am an architect who commands an
army of AI workers. My job is to think, delegate, and verify."
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# External Brain Registry
# ---------------------------------------------------------------------------

@dataclass
class ExternalBrain:
    """An external AI tool discovered in the environment."""
    name: str               # e.g. "claude", "cursor", "ollama"
    cli_path: str           # full path to the CLI binary
    version: str            # detected version string
    tier: int               # 1=top (Claude/GPT), 2=mid (Cursor), 3=local (Ollama)
    cost: str               # "free" | "paid" | "local"
    capabilities: list[str] # what it's good at
    max_context: int        # rough token limit for input
    available: bool = True


# Capability tags
CAP_CODE_REFACTOR = "code_refactor"
CAP_CODE_GENERATE = "code_generate"
CAP_CODE_REVIEW = "code_review"
CAP_COMPLEX_REASONING = "complex_reasoning"
CAP_FILE_EDIT = "file_edit"
CAP_TERMINAL = "terminal"
CAP_SIMPLE_CHAT = "simple_chat"
CAP_TOOL_USE = "tool_use"


def _run_quiet(cmd: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run a command and return (success, stdout)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0, (result.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return False, ""


def probe_environment(workspace: Path | str | None = None) -> list[ExternalBrain]:
    """
    Probe the system for all available AI CLI tools.

    This is the "武器库清点" — discover what brains we can outsource to.
    Called at startup and after nomadic migration.
    """
    brains: list[ExternalBrain] = []

    # --- Claude CLI ---
    ok, ver = _run_quiet("claude --version")
    if ok and ver:
        brains.append(ExternalBrain(
            name="claude",
            cli_path="claude",
            version=ver.split("\n")[0],
            tier=1,
            cost="free",  # Uses user's Claude subscription, not DevClaw's API key
            capabilities=[
                CAP_CODE_REFACTOR, CAP_CODE_GENERATE, CAP_CODE_REVIEW,
                CAP_COMPLEX_REASONING, CAP_FILE_EDIT, CAP_TOOL_USE,
            ],
            max_context=200000,
        ))

    # --- Cursor CLI ---
    ok, ver = _run_quiet("cursor --version")
    if ok and ver:
        brains.append(ExternalBrain(
            name="cursor",
            cli_path="cursor",
            version=ver.split("\n")[0],
            tier=2,
            cost="free",  # Uses user's Cursor subscription
            capabilities=[
                CAP_CODE_REFACTOR, CAP_CODE_GENERATE, CAP_FILE_EDIT,
            ],
            max_context=100000,
        ))

    # --- GitHub Copilot CLI ---
    ok, ver = _run_quiet("gh copilot --version")
    if ok:
        brains.append(ExternalBrain(
            name="gh_copilot",
            cli_path="gh copilot",
            version=ver.split("\n")[0] if ver else "unknown",
            tier=2,
            cost="free",
            capabilities=[CAP_CODE_GENERATE, CAP_SIMPLE_CHAT],
            max_context=8000,
        ))

    # --- Ollama models ---
    # Try multiple paths for Ollama on Windows
    ollama_cmd = "ollama"
    for candidate in ("ollama", os.path.expanduser("~/AppData/Local/Programs/Ollama/ollama.exe")):
        test_ok, _ = _run_quiet(f"{candidate} --version")
        if test_ok:
            ollama_cmd = candidate
            break
    ok, output = _run_quiet(f"{ollama_cmd} list")
    if ok and output:
        for line in output.strip().split("\n")[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 1:
                model_name = parts[0]
                size_str = parts[2] if len(parts) > 2 else "?"
                brains.append(ExternalBrain(
                    name=f"ollama:{model_name}",
                    cli_path=f"ollama run {model_name}",
                    version=model_name,
                    tier=3,
                    cost="local",
                    capabilities=[CAP_SIMPLE_CHAT, CAP_CODE_GENERATE],
                    max_context=8000,
                ))

    return brains


def save_brain_registry(workspace: Path, brains: list[ExternalBrain]) -> Path:
    """Persist discovered brains to .claw/brain_registry.json."""
    ws = Path(workspace).resolve()
    out = ws / ".claw" / "brain_registry.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "probed_at": time.time(),
        "brains": [
            {
                "name": b.name,
                "cli_path": b.cli_path,
                "version": b.version,
                "tier": b.tier,
                "cost": b.cost,
                "capabilities": b.capabilities,
                "max_context": b.max_context,
                "available": b.available,
            }
            for b in brains
        ],
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_brain_registry(workspace: Path) -> list[ExternalBrain]:
    """Load cached brain registry."""
    ws = Path(workspace).resolve()
    path = ws / ".claw" / "brain_registry.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            ExternalBrain(**{k: v for k, v in b.items() if k != "available"}, available=b.get("available", True))
            for b in data.get("brains", [])
        ]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Task Router — pick the best brain for a task
# ---------------------------------------------------------------------------

def select_brain(
    task_type: str,
    brains: list[ExternalBrain],
    *,
    prefer_free: bool = True,
    min_tier: int = 3,
) -> ExternalBrain | None:
    """
    Select the best brain for a given task type.

    Priority:
    1. Free brains with the required capability
    2. Lowest tier number (highest quality)
    3. Highest max_context
    """
    candidates = [
        b for b in brains
        if b.available and task_type in b.capabilities and b.tier <= min_tier
    ]

    if not candidates:
        return None

    if prefer_free:
        free = [b for b in candidates if b.cost in ("free", "local")]
        if free:
            candidates = free

    # Sort: tier ASC (best first), then max_context DESC
    candidates.sort(key=lambda b: (b.tier, -b.max_context))
    return candidates[0]


# ---------------------------------------------------------------------------
# Delegation Protocol — send task to external brain
# ---------------------------------------------------------------------------

@dataclass
class DelegationResult:
    """Result of delegating a task to an external brain."""
    brain_name: str
    success: bool
    output: str
    output_file: str | None = None
    elapsed_sec: float = 0.0
    retries: int = 0
    error: str = ""


def delegate_to_claude_cli(
    instruction: str,
    workspace: Path,
    *,
    output_file: str | None = None,
    timeout_sec: float = 300,
    max_turns: int = 5,
) -> DelegationResult:
    """
    Delegate a task to Claude CLI (claude command).

    This is the crown jewel: a Tier 1 brain that costs us nothing.
    """
    ws = Path(workspace).resolve()
    start = time.time()

    # Build the claude command
    # Use --print flag for non-interactive output
    # Use --dangerously-skip-permissions to avoid interactive prompts
    cmd_parts = [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
    ]

    if output_file:
        # Ask Claude to write output to a specific file
        instruction += f"\n\nIMPORTANT: Write your final output/code to the file: {output_file}"

    # Pipe the instruction via stdin
    full_cmd = " ".join(cmd_parts)

    try:
        result = subprocess.run(
            full_cmd,
            input=instruction,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=str(ws),
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - start
        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()

        if result.returncode == 0:
            return DelegationResult(
                brain_name="claude",
                success=True,
                output=output[:100000],
                output_file=output_file,
                elapsed_sec=round(elapsed, 1),
            )
        else:
            return DelegationResult(
                brain_name="claude",
                success=False,
                output=output[:50000],
                elapsed_sec=round(elapsed, 1),
                error=error[:2000],
            )

    except subprocess.TimeoutExpired:
        return DelegationResult(
            brain_name="claude",
            success=False,
            output="",
            elapsed_sec=timeout_sec,
            error=f"Claude CLI timed out after {timeout_sec}s",
        )
    except Exception as e:
        return DelegationResult(
            brain_name="claude",
            success=False,
            output="",
            elapsed_sec=time.time() - start,
            error=str(e)[:2000],
        )


def delegate_to_ollama(
    instruction: str,
    model: str = "gemma3:4b",
    *,
    timeout_sec: float = 120,
) -> DelegationResult:
    """Delegate a task to a local Ollama model."""
    start = time.time()
    try:
        result = subprocess.run(
            f'ollama run {model} "{instruction[:4000]}"',
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - start
        return DelegationResult(
            brain_name=f"ollama:{model}",
            success=result.returncode == 0,
            output=(result.stdout or "").strip()[:50000],
            elapsed_sec=round(elapsed, 1),
            error=(result.stderr or "").strip()[:2000] if result.returncode != 0 else "",
        )
    except subprocess.TimeoutExpired:
        return DelegationResult(
            brain_name=f"ollama:{model}",
            success=False,
            output="",
            elapsed_sec=timeout_sec,
            error="timeout",
        )
    except Exception as e:
        return DelegationResult(
            brain_name=f"ollama:{model}",
            success=False,
            output="",
            elapsed_sec=time.time() - start,
            error=str(e),
        )


def delegate_task(
    instruction: str,
    task_type: str,
    workspace: Path,
    *,
    brains: list[ExternalBrain] | None = None,
    timeout_sec: float = 300,
    output_file: str | None = None,
    progress_hook: Callable[[str], None] | None = None,
) -> DelegationResult:
    """
    Smart delegation: pick the best brain and send the task.

    This is the core "foreman" function — DevClaw never does heavy
    lifting itself, it delegates to the best available worker.
    """
    ws = Path(workspace).resolve()

    if brains is None:
        brains = load_brain_registry(ws)
        if not brains:
            brains = probe_environment(ws)
            save_brain_registry(ws, brains)

    brain = select_brain(task_type, brains)

    if brain is None:
        return DelegationResult(
            brain_name="none",
            success=False,
            output="",
            error="No suitable brain found for this task type. Falling back to self.",
        )

    if progress_hook:
        progress_hook(
            f"[🧠 认知外包] 将任务委派给 {brain.name} (Tier {brain.tier}, {brain.cost})\n"
            f"任务: {instruction[:100]}..."
        )

    # Route to the appropriate delegation function
    if brain.name == "claude":
        result = delegate_to_claude_cli(
            instruction, ws,
            output_file=output_file,
            timeout_sec=timeout_sec,
        )
    elif brain.name.startswith("ollama:"):
        model = brain.name.split(":", 1)[1]
        result = delegate_to_ollama(instruction, model, timeout_sec=timeout_sec)
    else:
        # Generic CLI delegation
        result = _delegate_generic_cli(brain, instruction, ws, timeout_sec)

    if progress_hook:
        icon = "✅" if result.success else "❌"
        progress_hook(
            f"[🧠 外包结果] {icon} {result.brain_name} "
            f"({result.elapsed_sec:.0f}s)\n"
            f"{result.output[:200]}{'...' if len(result.output) > 200 else ''}"
        )

    return result


def _delegate_generic_cli(
    brain: ExternalBrain,
    instruction: str,
    workspace: Path,
    timeout_sec: float,
) -> DelegationResult:
    """Generic delegation for any CLI tool."""
    start = time.time()
    try:
        result = subprocess.run(
            f'{brain.cli_path} "{instruction[:4000]}"',
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=str(workspace),
            encoding="utf-8",
            errors="replace",
        )
        return DelegationResult(
            brain_name=brain.name,
            success=result.returncode == 0,
            output=(result.stdout or "").strip()[:50000],
            elapsed_sec=round(time.time() - start, 1),
            error=(result.stderr or "").strip()[:2000] if result.returncode != 0 else "",
        )
    except Exception as e:
        return DelegationResult(
            brain_name=brain.name,
            success=False,
            output="",
            elapsed_sec=time.time() - start,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Verification Protocol — verify delegated work
# ---------------------------------------------------------------------------

def verify_delegation(
    result: DelegationResult,
    workspace: Path,
    *,
    output_file: str | None = None,
    test_cmd: str | None = None,
) -> dict[str, Any]:
    """
    Verify that delegated work is correct.

    Steps:
    1. Check if output file was created (if specified)
    2. Run syntax check on generated code
    3. Run tests if available
    4. Return verification verdict
    """
    ws = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []

    # Check output file exists
    if output_file:
        target = ws / output_file
        exists = target.is_file()
        checks.append({
            "check": "output_file_exists",
            "passed": exists,
            "detail": f"{output_file} {'exists' if exists else 'NOT FOUND'}",
        })

        # Syntax check for .py files
        if exists and output_file.endswith(".py"):
            try:
                code = target.read_text(encoding="utf-8")
                compile(code, output_file, "exec")
                checks.append({"check": "python_syntax", "passed": True, "detail": "valid"})
            except SyntaxError as e:
                checks.append({"check": "python_syntax", "passed": False, "detail": str(e)})

    # Run test command if provided
    if test_cmd:
        try:
            test_result = subprocess.run(
                test_cmd, shell=True, capture_output=True, text=True,
                timeout=60, cwd=str(ws), encoding="utf-8", errors="replace",
            )
            checks.append({
                "check": "test_command",
                "passed": test_result.returncode == 0,
                "detail": (test_result.stdout or "")[:500],
            })
        except Exception as e:
            checks.append({"check": "test_command", "passed": False, "detail": str(e)})

    all_passed = all(c["passed"] for c in checks) if checks else result.success

    return {
        "verified": all_passed,
        "checks": checks,
        "brain": result.brain_name,
        "elapsed_sec": result.elapsed_sec,
    }


# ---------------------------------------------------------------------------
# Full outsourcing pipeline: Delegate → Verify → Retry → Accept
# ---------------------------------------------------------------------------

def outsource_task(
    instruction: str,
    task_type: str,
    workspace: Path,
    *,
    output_file: str | None = None,
    test_cmd: str | None = None,
    max_retries: int = 2,
    timeout_sec: float = 300,
    progress_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Full cognitive outsourcing pipeline:
    1. Select best brain
    2. Delegate task
    3. Verify result
    4. Retry with error feedback if failed
    5. Return final result

    This is the "包工头" workflow — DevClaw never writes code itself
    when a better brain is available for free.
    """
    ws = Path(workspace).resolve()

    for attempt in range(max_retries + 1):
        # Enhance instruction with error feedback on retry
        if attempt > 0:
            instruction = (
                f"[RETRY {attempt}/{max_retries}] The previous attempt had errors:\n"
                f"{last_error}\n\n"
                f"Please fix and try again. Original task:\n{instruction}"
            )

        # Delegate
        result = delegate_task(
            instruction, task_type, ws,
            output_file=output_file,
            timeout_sec=timeout_sec,
            progress_hook=progress_hook,
        )

        if not result.success:
            last_error = result.error or "Unknown error"
            if progress_hook:
                progress_hook(f"[🔄 外包重试] 第 {attempt + 1} 次失败: {last_error[:200]}")
            continue

        # Verify
        verification = verify_delegation(
            result, ws,
            output_file=output_file,
            test_cmd=test_cmd,
        )

        if verification["verified"]:
            if progress_hook:
                progress_hook(
                    f"[✅ 外包验收通过] {result.brain_name} 完成任务 "
                    f"({result.elapsed_sec:.0f}s, {attempt + 1} attempts)"
                )
            return {
                "success": True,
                "brain": result.brain_name,
                "output": result.output,
                "output_file": output_file,
                "verification": verification,
                "attempts": attempt + 1,
            }
        else:
            failed_checks = [c for c in verification["checks"] if not c["passed"]]
            last_error = "; ".join(c["detail"] for c in failed_checks)
            if progress_hook:
                progress_hook(f"[🔄 验收失败] {last_error[:200]}")

    return {
        "success": False,
        "brain": result.brain_name if result else "none",
        "output": result.output if result else "",
        "error": f"Failed after {max_retries + 1} attempts",
        "attempts": max_retries + 1,
    }


# ---------------------------------------------------------------------------
# System prompt injection for DevClaw's "foreman consciousness"
# ---------------------------------------------------------------------------

def build_outsourcing_system_prompt(brains: list[ExternalBrain]) -> str:
    """
    Build system prompt addition that gives DevClaw the "foreman" consciousness.

    This is injected into dev_claw_run()'s system prompt to make DevClaw
    aware of its external workforce.
    """
    if not brains:
        return ""

    lines = [
        "\n\n【认知外包 — Cognitive Outsourcing Protocol】",
        "你是包工头，不是码农。当前系统中存在以下外部AI工具可供你调遣：",
        "",
    ]

    for b in brains:
        tier_label = {1: "顶级", 2: "中等", 3: "本地"}.get(b.tier, "?")
        lines.append(
            f"  - **{b.name}** (Tier {b.tier} {tier_label}, {b.cost}) "
            f"v{b.version} — 擅长: {', '.join(b.capabilities[:3])}"
        )

    lines.extend([
        "",
        "**外包规则（强制执行）：**",
        "1. 当你使用本地小模型（寄生模式）且任务涉及复杂重构/算法/大文件时，"
        "**严禁直接输出低质量代码**。",
        "2. 必须通过 execute_terminal 调用外部 CLI 工具来完成重活。格式：",
        '   `claude --print --dangerously-skip-permissions "你的详细指令"`',
        "3. 委派后，你必须验证输出：运行测试、检查语法、对比预期结果。",
        "4. 如果外部工具报错，分析错误后重新委派，最多重试2次。",
        "5. 优先使用免费工具（claude > cursor > ollama），绝不浪费自己的API余额。",
        "6. 你的角色是：思考 → 委派 → 验收 → 汇报。",
    ])

    return "\n".join(lines)
