"""
DevClaw — OpenAI tool loop + OpenClaw-style skills (SKILL.md), hot-reload, memory, session logs.
Requires: pip install openai (see dev_claw/requirements.txt)
Env: OPENAI_API_KEY, OPENAI_MODEL, DEVCLAW_WORKSPACE, DEVCLAW_SKILLS=0 to disable skill injection
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]

from claw_runtime.bot_task_queue import SmartTaskRegistry
from claw_runtime.cognitive_outsourcing import (
    build_outsourcing_system_prompt,
    probe_environment,
    save_brain_registry,
)
from claw_runtime.self_intelligence import (
    ActionOutcome,
    build_intelligence_prompt,
    record_action,
)
from claw_runtime.memory import append_memory
from claw_runtime.offline_brain import run_offline_brain
from claw_runtime.sandbox_docker import docker_enabled, run_shell_in_docker
from claw_runtime.session_log import log_tool
from claw_runtime.skill_registry import SkillRegistry
from claw_runtime.quota_tracker import QuotaTracker
from claw_runtime.survival_engine import SurvivalEngine, SurvivalState
from claw_runtime.survival_reflex import run_critical_reflex
from claw_runtime.task_complexity_detector import assess_complexity
from claw_runtime.task_decomposer import decompose_and_enqueue
from claw_runtime.ultimate.nomad import load_nomad_system_append
from claw_runtime.ultimate.proxy_env import (
    apply_proxy_env,
    current_proxy_env,
    restore_proxy_env,
    snapshot_proxy_env,
)
from claw_runtime.ultimate.self_heal import emit_rebuild_venv_scripts
from claw_runtime.error_attribution import tool_execution_middleware


def _workspace_root() -> str:
    env = os.environ.get("DEVCLAW_WORKSPACE")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


MAX_TOOL_CHARS = 120_000
TERMINAL_TIMEOUT = int(os.environ.get("DEVCLAW_TERMINAL_TIMEOUT", "120"))
WEB_FETCH_MAX = int(os.environ.get("DEVCLAW_WEB_FETCH_MAX", "1500000"))


def _ensure_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _offline_brain_enabled() -> bool:
    return os.environ.get("DEVCLAW_OFFLINE_BRAIN", "auto").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _openai_error_corpus(err: Exception) -> str:
    """Collect text/JSON from OpenAI SDK errors (body is often absent from str(e))."""
    parts: list[str] = [str(err), repr(err)]
    code = getattr(err, "code", None)
    if code is not None:
        parts.append(str(code))
    msg = getattr(err, "message", None)
    if isinstance(msg, str) and msg.strip():
        parts.append(msg)
    body = getattr(err, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else repr(body))
        except (TypeError, ValueError):
            parts.append(repr(body))
    resp = getattr(err, "response", None)
    if resp is not None:
        try:
            txt = getattr(resp, "text", None)
            if txt:
                parts.append(str(txt)[:4000])
        except Exception:
            pass
    return "\n".join(parts).lower()


def _survival_record_api_error(workspace_path: Path, err: Exception) -> None:
    try:
        se = SurvivalEngine(workspace_path)
        blob = _openai_error_corpus(err)
        code = type(err).__name__
        if "insufficient_quota" in blob:
            code = "insufficient_quota"
        elif getattr(err, "status_code", None) == 429 or "rate_limit_exceeded" in blob:
            code = "429"
        elif "429" in blob or "rate limit" in blob:
            code = "429"
        se.record_api_error(code, str(err)[:1200])
    except Exception:
        pass


def _is_quota_exhausted_error(err: Exception) -> bool:
    code = getattr(err, "code", None)
    if code == "insufficient_quota":
        return True
    blob = _openai_error_corpus(err)
    if "insufficient_quota" in blob:
        return True
    if "exceeded your current quota" in blob or "billing_hard_limit" in blob:
        return True
    try:
        body = getattr(err, "body", None)
        if isinstance(body, dict):
            err_obj = body.get("error")
            if isinstance(err_obj, dict) and err_obj.get("code") == "insufficient_quota":
                return True
    except Exception:
        pass
    return False


def _is_rate_limit_api_error(err: Exception) -> bool:
    if _is_quota_exhausted_error(err):
        return False
    if getattr(err, "status_code", None) == 429:
        return True
    code = getattr(err, "code", None)
    if code == "rate_limit_exceeded":
        return True
    blob = _openai_error_corpus(err)
    return "429" in blob or "rate limit" in blob or "too many requests" in blob


def execute_terminal(command: str) -> str:
    """Run a shell command (cwd = workspace). Optional Docker sandbox via env or claw.config.json."""
    root = _workspace_root()
    print(f"\n[terminal] {command}")
    ws_path = Path(root)
    use_dock, image, net = docker_enabled(ws_path)
    if use_dock:
        try:
            cp = run_shell_in_docker(
                root,
                command,
                image=image,
                network=net,
                timeout=TERMINAL_TIMEOUT,
            )
            out = (cp.stdout or "") + (cp.stderr or "")
            if not out.strip():
                out = "(docker: no output, exit code %s)" % cp.returncode
            print(f"[docker terminal preview]\n{out[:400]}...\n" if len(out) > 400 else out)
            return out[:MAX_TOOL_CHARS]
        except FileNotFoundError:
            return "Docker 不可用（未安装或不在 PATH）。请关闭 sandbox 或安装 Docker Desktop。"
        except subprocess.TimeoutExpired:
            return f"Docker 执行超时（>{TERMINAL_TIMEOUT}s）"
        except Exception as e:  # noqa: BLE001
            return f"Docker 执行报错: {e!s}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TERMINAL_TIMEOUT,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if not out.strip():
            out = "(no output, exit code %s)" % result.returncode
        print(f"[terminal output preview]\n{out[:400]}...\n" if len(out) > 400 else f"[terminal output]\n{out}\n")
        return out[:MAX_TOOL_CHARS]
    except subprocess.TimeoutExpired:
        return f"执行超时（>{TERMINAL_TIMEOUT}s）"
    except Exception as e:  # noqa: BLE001
        return f"执行报错: {e!s}"


def edit_local_file(filepath: str, content: str, mode: str = "w") -> str:
    """Read or write text under workspace (paths outside workspace are rejected)."""
    root = os.path.abspath(_workspace_root())
    print(f"\n[file] {mode} {filepath}")
    abs_path = os.path.abspath(os.path.join(root, filepath))
    if not abs_path.startswith(root + os.sep) and abs_path != root:
        return f"拒绝：路径必须位于工作区内：{root}"

    try:
        if mode == "r":
            with open(abs_path, encoding="utf-8") as f:
                data = f.read()
            return data[:MAX_TOOL_CHARS]

        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        write_mode = "w" if mode == "w" else "a"
        with open(abs_path, write_mode, encoding="utf-8") as f:
            f.write(content or "")
        return f"已保存：{abs_path}"
    except Exception as e:  # noqa: BLE001
        return f"文件操作失败: {e!s}"


def web_fetch(url: str) -> str:
    """HTTP GET text/html/json; size-capped."""
    print(f"\n[web_fetch] {url}")
    try:
        proxy_map = current_proxy_env(_workspace_root())
        handlers = []
        if proxy_map:
            mapped: dict[str, str] = {}
            if proxy_map.get("HTTP_PROXY"):
                mapped["http"] = proxy_map["HTTP_PROXY"]
            if proxy_map.get("HTTPS_PROXY"):
                mapped["https"] = proxy_map["HTTPS_PROXY"]
            if mapped:
                handlers.append(ProxyHandler(mapped))
        req = Request(
            url,
            headers={"User-Agent": "DevClaw-web_fetch/1.0"},
            method="GET",
        )
        opener = build_opener(*handlers) if handlers else None
        open_fn = opener.open if opener is not None else urlopen
        with open_fn(req, timeout=45) as resp:
            data = resp.read(WEB_FETCH_MAX + 1)
        if len(data) > WEB_FETCH_MAX:
            return f"响应超过 {WEB_FETCH_MAX} 字节，已拒绝（请换 API 或缩小范围）。"
        text = data.decode("utf-8", errors="replace")
        return text[:MAX_TOOL_CHARS]
    except HTTPError as e:
        return f"HTTP {e.code}: {e.reason}"
    except URLError as e:
        return f"URL 错误: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return f"web_fetch 失败: {e!s}"


def _probe_compatible_backend(base_url: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Probe with generous timeout — high memory systems need more time."""
    probe_url = base_url.rstrip("/") + "/models"
    req = Request(probe_url, headers={"User-Agent": "DevClaw-parasite-probe/1.0"}, method="GET")
    # Try multiple times — on high-memory systems, first attempt often fails
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return True, f"HTTP {getattr(resp, 'status', 200)}"
        except HTTPError as exc:
            if exc.code in {401, 403, 404, 405}:
                return True, f"HTTP {exc.code}"
            if attempt < 2:
                import time as _time; _time.sleep(1)
                continue
            return False, f"HTTPError {exc.code}: {exc.reason}"
        except (URLError, Exception) as exc:
            if attempt < 2:
                import time as _time; _time.sleep(1)
                continue
            return False, f"{type(exc).__name__}: {exc!s}"
    return False, "probe failed after 3 attempts"


def use_browser_stub(task_prompt: str) -> str:
    """Placeholder: wire browser-use / Playwright here; in Cursor prefer Web-Browser MCP."""
    return (
        "浏览器工具未在此进程内启用。请在 Cursor 里用 Web-Browser MCP，"
        "或 pip install browser-use 后自行接入。任务描述：\n"
        + (task_prompt or "")[:2000]
    )


def safety_scan_relative_file(filepath: str) -> str:
    from claw_runtime.safety_scan import scan_path

    root = Path(_workspace_root()).resolve()
    p = (root / filepath).resolve()
    if not str(p).startswith(str(root) + os.sep) and p != root:
        return "拒绝：路径必须在工作区内"
    if not p.is_file():
        return f"不是文件: {filepath}"
    findings = scan_path(p)
    if not findings:
        return "safety_scan: 未发现规则命中（仍不代表绝对安全）。"
    lines = [f"[{f.severity.value}] {f.rule_id}: {f.message}" for f in findings]
    return "\n".join(lines)


def load_skill_body(registry: SkillRegistry, skill_name: str) -> str:
    rec = registry.get(skill_name.strip())
    if not rec:
        names = ", ".join(sorted(registry.refresh().keys()))
        return f"未找到技能 `{skill_name}`。可用: {names or '(无)'}"
    header = f"# Skill: {rec.name}\n{rec.description}\n\n---\n\n"
    return (header + rec.body)[:MAX_TOOL_CHARS]


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_terminal",
            "description": "在工作区根目录下执行 shell 命令（Windows 可用 py/python、pip、dir 等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "完整命令行字符串"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_local_file",
            "description": "读取或写入工作区相对路径下的文本文件。mode=r 读取；w 覆盖；a 追加。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "相对工作区根的路径，如 tools/x.py"},
                    "content": {"type": "string", "description": "写入内容；mode=r 时可传空字符串"},
                    "mode": {"type": "string", "enum": ["r", "w", "a"]},
                },
                "required": ["filepath", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "HTTP GET 公开 URL，返回文本（HTML/JSON）。用于无 API 时的文档或页面抓取；遵守站点条款。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "按名称加载完整 AgentSkills 风格 SKILL.md 正文（渐进式披露）。系统提示里只有摘要。",
            "parameters": {
                "type": "object",
                "properties": {"skill_name": {"type": "string"}},
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_typed_memory",
            "description": "写入分型长期记忆（markdown 追加）。category: decision|lesson|person|commitment|preference|fact|project",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["category", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_browser",
            "description": "意图驱动浏览器（占位）。真实抓取优先用 Cursor MCP 或 browser-use。",
            "parameters": {
                "type": "object",
                "properties": {"task_prompt": {"type": "string"}},
                "required": ["task_prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "safety_scan_file",
            "description": "对工作区内文件跑危险模式扫描（curl|sh、fork bomb、rm -rf / 等）。",
            "parameters": {
                "type": "object",
                "properties": {"filepath": {"type": "string", "description": "相对工作区根"}},
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_brain",
            "description": "将复杂任务委派给外部AI工具(如claude cli)执行。你是包工头，它们是打工仔。适用于：复杂重构、大文件改写、算法实现等你当前能力不足以完成的任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "description": "给外部AI的详细任务指令（越详细越好）"},
                    "task_type": {
                        "type": "string",
                        "enum": ["code_refactor", "code_generate", "code_review", "complex_reasoning"],
                        "description": "任务类型，用于选择最合适的外部AI",
                    },
                    "output_file": {"type": "string", "description": "期望外部AI输出到的文件路径（相对工作区）"},
                    "test_cmd": {"type": "string", "description": "验收测试命令（可选）"},
                },
                "required": ["instruction", "task_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decompose_task",
            "description": "将一个复杂宏大的任务拆解为多个原子子任务并排入执行队列。当你发现任务太大无法一次完成时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "description": "需要拆解的宏大任务描述"},
                    "context": {"type": "string", "description": "额外上下文信息（可选）"},
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_task_queue",
            "description": "查看当前智能任务队列状态：待执行、执行中、已完成、失败的任务列表。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_claw_skill",
            "description": "从 URL(zip) 或本地路径安装技能到 skills/<name>/。需环境变量 DEVCLAW_ALLOW_SKILL_INSTALL=1。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "skill_name": {"type": "string", "description": "可选，覆盖文件夹名"},
                    "skip_safety": {"type": "boolean", "description": "true 跳过安装前扫描（危险）"},
                },
                "required": ["source"],
            },
        },
    },
]


def _assistant_to_dict(msg: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if getattr(msg, "tool_calls", None):
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": getattr(tc, "type", "function") or "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return d


def _run_via_claude_cli(
    workspace_path: Path,
    user_instruction: str,
    emit: Callable[[str], None] | None = None,
) -> bool | None:
    """
    Brain Switch: Use Claude CLI as the execution brain.

    Claude CLI uses the user's subscription (free for DevClaw).
    It has full tool access (read/write files, run terminal commands).
    This is the "outsource the whole job" path.

    Returns True (success), False (failure), or None (Claude CLI unavailable).
    """
    import subprocess
    import shutil
    import re

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None

    if emit:
        emit("[🧠 Claude CLI] 正在用高级大脑处理任务…")

    try:
        result = subprocess.run(
            [
                claude_bin,
                "--print",
                "--dangerously-skip-permissions",
                f"你是DevClaw超级智能体。在工作区 {workspace_path} 中执行以下任务：\n\n{user_instruction}",
            ],
            capture_output=True,
            text=True,
            timeout=180,  # 3 minutes max
            cwd=str(workspace_path),
            encoding="utf-8",
            errors="replace",
        )

        output = result.stdout.strip() if result.stdout else ""
        if result.returncode == 0 and output:
            if emit:
                # Clean output for user
                clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output).strip()
                emit(clean[:4000])
            return True
        else:
            stderr = result.stderr.strip()[:500] if result.stderr else ""
            if emit and stderr:
                emit(f"[Claude CLI 报错] {stderr[:200]}")
            return False

    except subprocess.TimeoutExpired:
        if emit:
            emit("[Claude CLI] 执行超时 (180s)，任务可能太复杂")
        return False
    except Exception as e:
        if emit:
            emit(f"[Claude CLI] 不可用: {e!s}")
        return None


def _run_self_driving_queue(
    workspace_path: Path,
    *,
    max_iterations: int = 16,
    system_append: str | None = None,
    progress_hook: Callable[[str], None] | None = None,
    max_tasks: int = 50,
) -> bool:
    """
    Self-driving queue executor: pulls and executes tasks from SmartTaskRegistry
    until the queue is empty or max_tasks is reached.

    This is the "foreman" loop that makes DevClaw autonomous.
    """
    def _emit(text: str) -> None:
        if progress_hook:
            progress_hook(text)

    registry = SmartTaskRegistry(workspace_path)
    completed = 0
    failed = 0

    # liquid_topology: use mesh execution for 3+ independent tasks
    try:
        from claw_runtime.liquid_topology import select_topology, run_liquid_mesh
        # Only use for multiple independent tasks
    except Exception:
        pass

    for task_num in range(max_tasks):
        task = registry.next_runnable()
        if task is None:
            # Check if there are still pending tasks (blocked)
            summary = registry.queue_summary()
            if summary.get("pending", 0) > 0:
                _emit(
                    f"[⏳ 队列阻塞] 还有 {summary['pending']} 个待执行任务，"
                    f"但它们被依赖关系阻塞。已完成: {completed}, 失败: {failed}"
                )
            else:
                _emit(
                    f"[✅ 队列清空] 所有子任务执行完毕！"
                    f"完成: {completed}, 失败: {failed}"
                )
            break

        # Mark task as running
        registry.mark_running(task.task_id)
        _emit(
            f"\n[📌 子任务 {task_num + 1}] [{task.task_id[:8]}] Phase {task.phase}\n"
            f"{task.instruction[:200]}"
        )

        # Execute the sub-task
        try:
            sub_append = (
                f"\n\n[子任务上下文] 你正在执行自动拆解后的子任务 (ID: {task.task_id[:8]})。"
                f"完成当前任务即可，不要尝试执行其他子任务。"
                f"如果遇到困难，在 task_plan.md 中记录并继续。"
            )
            if system_append:
                sub_append = system_append + sub_append

            success = dev_claw_run(
                task.instruction,
                max_iterations=max_iterations,
                system_append=sub_append,
                progress_hook=progress_hook,
                _resume_depth=1,  # prevent recursive decomposition
            )

            if success:
                registry.mark_done(task.task_id, result_summary="completed successfully")
                completed += 1
                _emit(f"[✅ 子任务完成] [{task.task_id[:8]}] ({completed} done, {failed} failed)")
            else:
                registry.mark_failed(task.task_id, error="execution returned False")
                failed += 1
                _emit(f"[❌ 子任务失败] [{task.task_id[:8]}] retry {task.retries}/{task.max_retries}")

        except Exception as exc:
            registry.mark_failed(task.task_id, error=str(exc)[:500])
            failed += 1
            _emit(f"[❌ 子任务异常] [{task.task_id[:8]}] {exc!s}")

    # Final summary
    summary = registry.queue_summary()
    _emit(
        f"\n[📋 执行总结] 完成: {completed} | 失败: {failed} | "
        f"剩余待执行: {summary.get('pending', 0)} | "
        f"队列状态:\n{registry.format_queue_status()}"
    )

    return failed == 0 and summary.get("pending", 0) == 0


def dev_claw_run(
    user_instruction: str,
    max_iterations: int = 16,
    *,
    system_append: str | None = None,
    progress_hook: Callable[[str], None] | None = None,
    _resume_depth: int = 0,
) -> bool:
    """
    Meta-cognition hooks (see also `claw_runtime/survival_reflex.py`):

    - **Parasite:** `survival.parasite_active()` → Ollama-compatible `OpenAI` client + `OLLAMA_*`.
    - **Quota API errors:** `insufficient_quota` → `run_critical_reflex` (OUTBOX + parasite), then return (no crash).
    - **Jailbreak prompt:** `survival.jailbreak_escalated()` → extra system instructions after consecutive task failures.
    """
    def _emit(text: str) -> None:
        if progress_hook:
            progress_hook(text)

    # metabolic_scheduler: check if the task is worth the cost before executing
    try:
        from claw_runtime.metabolic_scheduler import should_execute
        _metabolic = should_execute(user_instruction, "general", _workspace_root())
        if not _metabolic.get("execute", True):
            _emit(f"[Metabolic] Task deferred: {_metabolic.get('reason', 'budget constraint')}")
            return False
    except Exception:
        pass

    _ensure_utf8_stdio()
    ws = _workspace_root()
    workspace_path = Path(ws)
    survival = SurvivalEngine(workspace_path)
    exit_success = False
    proxy_snapshot = snapshot_proxy_env()

    # ── Prefrontal Cortex: Task Complexity Interception ──────────────
    # If the task is too complex, auto-decompose into sub-tasks and
    # enqueue them instead of executing directly.
    _decompose_gate = os.environ.get("DEVCLAW_AUTO_DECOMPOSE", "1").strip().lower() not in {
        "0", "false", "no",
    }
    if _decompose_gate and _resume_depth == 0:
        try:
            assessment = assess_complexity(user_instruction, workspace=workspace_path)
            if assessment.should_decompose:
                _emit(
                    f"[🧠 前额叶拦截] 检测到宏大任务 (复杂度 {assessment.score}/10)。"
                    f"禁止直接执行，启动自动拆解..."
                )
                was_decomposed, report, task_ids = decompose_and_enqueue(
                    user_instruction,
                    workspace_path,
                    progress_hook=progress_hook,
                )
                if was_decomposed and task_ids:
                    _emit(report)
                    # Now run the self-driving queue to execute sub-tasks
                    _emit("[🚀 自驱动模式] 开始从任务队列中逐个执行子任务...")
                    return _run_self_driving_queue(
                        workspace_path,
                        max_iterations=max_iterations,
                        system_append=system_append,
                        progress_hook=progress_hook,
                    )
        except Exception as exc:
            # Decomposition failed; fall through to normal execution
            _emit(f"[⚠️ 拆解引擎异常] {exc!s}，回退到直接执行模式")
    # ── End Prefrontal Cortex ────────────────────────────────────────

    try:
        apply_proxy_env(workspace_path)
        parasite = survival.parasite_active()
        parasite_base_url: str | None = None
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        cloud_timeout = float(os.environ.get("DEVCLAW_MODEL_TIMEOUT_SEC", "60") or "60")
        parasite_timeout = float(os.environ.get("DEVCLAW_PARASITE_TIMEOUT_SEC", "12") or "12")
        request_timeout = parasite_timeout if parasite else cloud_timeout
        if not parasite and not api_key:
            if _offline_brain_enabled():
                result = run_offline_brain(
                    workspace_path,
                    user_instruction,
                    emit=_emit if progress_hook else None,
                    failure_reason="OPENAI_API_KEY missing",
                )
                _emit("[完成]\n" + result.summary)
                exit_success = True
                return True
            msg = (
                "请设置 OPENAI_API_KEY；或在 CRITICAL 后由生存反射开启寄生模式，"
                "并配置 OLLAMA_BASE_URL + OLLAMA_MODEL；也可手动 DEVCLAW_PARASITE_MODE=1。"
            )
            print(msg, file=sys.stderr)
            _emit(msg)
            if not progress_hook:
                raise SystemExit(1)
            return False

        if parasite:
            base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").strip()
            parasite_base_url = base_url
            okey = os.environ.get("OLLAMA_API_KEY", "ollama").strip() or "ollama"
            model = os.environ.get("OLLAMA_MODEL", "llama3.2").strip() or "llama3.2"
            if OpenAI is None:
                if _offline_brain_enabled():
                    result = run_offline_brain(
                        workspace_path,
                        user_instruction,
                        emit=_emit if progress_hook else None,
                        failure_reason="openai package missing for parasite mode",
                    )
                    _emit("[完成]\n" + result.summary)
                    exit_success = True
                    return True
                print("Missing openai package for parasite mode.", file=sys.stderr)
                return False
            client = OpenAI(base_url=base_url, api_key=okey, timeout=request_timeout)
            quota_platform: str | None = None
        else:
            if OpenAI is None:
                if _offline_brain_enabled():
                    result = run_offline_brain(
                        workspace_path,
                        user_instruction,
                        emit=_emit if progress_hook else None,
                        failure_reason="openai package missing",
                    )
                    _emit("[完成]\n" + result.summary)
                    exit_success = True
                    return True
                print("Missing openai. Run: py -m pip install -r dev_claw/requirements.txt", file=sys.stderr)
                return False
            client = OpenAI(api_key=api_key, timeout=request_timeout)
            model = os.environ.get("OPENAI_MODEL", "gpt-4o").strip() or "gpt-4o"
            quota_platform = "openai"

        skills_on = os.environ.get("DEVCLAW_SKILLS", "1").strip().lower() not in {"0", "false", "no"}
        registry = SkillRegistry(workspace_path)
        qt = QuotaTracker(workspace_path) if quota_platform else None
        nomad_append = load_nomad_system_append(workspace_path)

        print("==================================================")
        print("DevClaw / workspace:", ws)
        print("model:", model, "| mode:", "parasite" if parasite else "openai")
        print("skills:", "on" if skills_on else "off")
        print("instruction:", user_instruction)
        print("==================================================\n")

        if os.environ.get("DEVCLAW_AUTO_SELF_HEAL_ON_CRITICAL", "1").strip().lower() not in {"0", "false", "no"}:
            pre_state, pre_reason = survival.assess_survival_state()
            if pre_state == SurvivalState.CRITICAL:
                try:
                    ps1, sh = emit_rebuild_venv_scripts(workspace_path)
                    _emit(
                        "[自愈准备] 检测到 CRITICAL，已刷新环境修复脚本："
                        f"{ps1.name}, {sh.name}。原因: {pre_reason}"
                    )
                except Exception:
                    pass

        mode_line = "寄生模式 (Ollama / 本地兼容)" if parasite else "云端 OpenAI"
        _emit(
            "DevClaw 启动\n"
            f"工作区: {ws}\n"
            f"通道: {mode_line}\n"
            f"模型: {model}\n"
            f"任务: {user_instruction[:2000]}"
            + ("…" if len(user_instruction) > 2000 else "")
        )

        base_core = (
            "你是顶级全栈工程师，可在用户工作区内执行终端命令与读写文件。"
            "优先小步验证：先读再改，再运行测试。遇到连续失败要分析日志并调整。"
            "文件路径一律使用相对工作区根的 POSIX 风格或 Windows 相对路径（如 tools\\\\x.py）。"
            "需要浏览器时先调用 use_browser 了解占位说明，或 web_fetch 拉公开文档。"
            "\n\n【Meta-Cognitive Instruction】\n"
            "生存第一：你必须实时监控 `.claw/survival_state.json`（生存引擎持久化状态）。"
            "若发现 API 额度/计费不足：优先 load_skill 执行 trading_skills 等相关技能，在合法合规前提下探索获利或续费相关的**只读探针与草案**；"
            "**禁止**未经授权的自动 OpenAI 代充、自动绑卡支付或无人托管的链上提款——资金与计费变更必须经过人类明确批准。"
            "若无法安全恢复付费通道：立即切换或保持「寄生模式」（本地 Ollama 等），并向根目录 `CURSOR_OUTBOX.md` 派发清晰的代工任务、上下文与验收标准。\n"
            "逻辑建链：禁止盲目尝试。遇到失败或亏损，必须在 `task_plan.md` 中建立推理链条，例如："
            "[现状] → [错误归因] → [学习调研] → [重构方案] → [实验验证]。\n"
            "自主学习：若现有 Skill 无法解决问题，主动调用 web_fetch 抓取相关公开技术文档或论文；将核心逻辑提炼后，按 skill-creator 类流程撰写或更新 `skills/<name>/SKILL.md`，"
            "必要时在 `DEVCLAW_ALLOW_SKILL_INSTALL=1` 下使用 install_claw_skill，实现自我进化。\n"
            "算力流浪：若本地 CPU/内存告急，主动编写并执行脚本，将高负载、可自动化的任务迁移至 GitHub Actions 或其它外部云端（遵守仓库 Secrets、配额与服务条款）。"
            "\n\n【OpenClaw 对齐】技能以 SKILL.md 形式存在（支持完整 YAML frontmatter + metadata.openclaw）；"
            "系统提示仅含技能目录，复杂流程先 load_skill。可 safety_scan_file 自查脚本。"
            "安装技能：CLI `py -m claw_runtime.cli skills-install <url|路径>` 或（高风险）"
            "DEVCLAW_ALLOW_SKILL_INSTALL=1 后调用 install_claw_skill。"
            "终端可在 Docker 内执行：claw.config.json sandbox.docker.enabled 或 DEVCLAW_USE_DOCKER_SANDBOX=1。"
            "多阶段编排：py -m claw_runtime.cli multi-agent …\n\n"
            "【包工头智力 — Task Decomposition Protocol】\n"
            "你拥有任务拆解的自主判断力。当收到宏大模糊任务（如'重构整个项目'、'开发一个新系统'）时：\n"
            "1. **禁止直接开始写代码**。先用 decompose_task 工具将任务拆解为原子子任务。\n"
            "2. 每个原子任务只涉及 1-3 个文件，能在 16 次工具调用内完成。\n"
            "3. 拆解后的子任务会自动排入智能队列，按依赖顺序逐个执行。\n"
            "4. 用 check_task_queue 查看当前队列状态和进度。\n"
            "5. 如果某个子任务失败，分析原因后决定：重试 / 跳过 / 进一步拆解。\n"
            "记住：你是项目经理，不是码农。先规划，再执行。"
        )
        # --- Self-Intelligence: inject learned wisdom from past experiences ---
        try:
            _intel_prompt = build_intelligence_prompt(workspace_path)
            if _intel_prompt:
                base_core += _intel_prompt
        except Exception:
            pass

        # --- Meta-Prompting: inject active prompt overlay ---
        try:
            from claw_runtime.meta_prompting import get_active_prompt_overlay
            _meta_overlay = get_active_prompt_overlay(workspace_path)
            if _meta_overlay:
                base_core += _meta_overlay
        except Exception:
            pass

        # --- Cognitive Outsourcing: probe for external brains and inject consciousness ---
        try:
            _ext_brains = probe_environment(workspace_path)
            if _ext_brains:
                save_brain_registry(workspace_path, _ext_brains)
                _outsourcing_prompt = build_outsourcing_system_prompt(_ext_brains)
                if _outsourcing_prompt:
                    base_core += _outsourcing_prompt
                    brain_names = [b.name for b in _ext_brains]
                    _emit(f"[🧠 认知外包] 发现外部智能体: {', '.join(brain_names)}")
        except Exception:
            pass  # Outsourcing probe must never crash the main loop

        if survival.jailbreak_escalated():
            base_core += (
                "\n\n【紧急授权 — 合规边界】检测到系统连续受阻。"
                "在合法前提下：允许尝试重构本地运行环境（venv、依赖、代理与 DNS）、"
                "使用自备代理列表（`PROXY_LIST_FILE` / `skills/proxy_rotator`）以及 `web_fetch` 获取公开文档。"
                "禁止协助入侵、未授权数据窃取、恶意软件或任何违法行为。"
            )
        if nomad_append:
            base_core = base_core + "\n\n[Nomad Context]\n" + nomad_append
        if system_append:
            base_core = base_core + "\n\n" + system_append.strip()

        catalog = registry.catalog_text() if skills_on else "## Skills\n(disabled via DEVCLAW_SKILLS=0)\n"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": base_core + "\n\n" + catalog},
            {"role": "user", "content": user_instruction},
        ]

        survival_gate = os.environ.get("DEVCLAW_SURVIVAL_GATE", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }

        for i in range(max_iterations):
            survival.heartbeat()
            # Loop start (iter 0): CRITICAL + insufficient_quota → reflex before gate aborts cloud calls
            if i == 0:
                _st_pre, _reason_pre = survival.assess_survival_state()
                _q_pre = survival.check_quota()
                if _st_pre == SurvivalState.CRITICAL and int(_q_pre.get("insufficient_quota_events_24h", 0) or 0) > 0:
                    reflex_db = float(os.environ.get("DEVCLAW_REFLEX_DEBOUNCE_SEC", "60") or "60")
                    tg_notify = (lambda t: _emit(t)) if progress_hook else None
                    run_critical_reflex(
                        workspace_path,
                        survival,
                        debounce_sec=reflex_db,
                        notify=tg_notify,
                    )
                    boss = "API 枯竭，已进入寄生模式，请老板在 Cursor 中代工。"
                    print(boss, file=sys.stderr)
                    _emit(boss)

            if survival_gate:
                state, reason = survival.assess_survival_state()
                if state == SurvivalState.CRITICAL:
                    if survival.parasite_active():
                        warn = f"[生存 CRITICAL + 寄生] {reason}\n继续尝试本地/廉价通道（不调用付费 OpenAI）。"
                        print(warn, file=sys.stderr)
                        _emit(warn)
                    else:
                        reflex_db = float(os.environ.get("DEVCLAW_REFLEX_DEBOUNCE_SEC", "60") or "60")
                        tg_notify = (lambda t: _emit(t)) if progress_hook else None
                        run_critical_reflex(
                            workspace_path,
                            survival,
                            debounce_sec=reflex_db,
                            notify=tg_notify,
                        )
                        msg = f"[生存状态 CRITICAL] {reason}\n已停止云端 API，并转入本地自治降级路径。"
                        print(msg, file=sys.stderr)
                        _emit(msg)
                        # Try Claude CLI before offline brain
                        _claude_ok = _run_via_claude_cli(workspace_path, user_instruction, _emit)
                        if _claude_ok is not None:
                            exit_success = bool(_claude_ok)
                            return exit_success
                        if _offline_brain_enabled():
                            result = run_offline_brain(
                                workspace_path,
                                user_instruction,
                                emit=_emit if progress_hook else None,
                                failure_reason=reason,
                            )
                            _emit("[完成]\n" + result.summary)
                            exit_success = True
                            return True
                        return False

            if skills_on:
                messages[0]["content"] = base_core + "\n\n" + registry.catalog_text()

            if qt and quota_platform and not qt.is_available(quota_platform):
                msg = "[配额] 估计窗口内 OpenAI 次数已用尽或处于冷却，请等待或启用寄生模式。"
                _emit(msg)
                return False

            try:
                if parasite:
                    probe_timeout = max(8.0, request_timeout / 2.0)  # Generous timeout for high-memory systems
                    ok, detail = _probe_compatible_backend(parasite_base_url or "", timeout=probe_timeout)
                    if not ok:
                        # ── Brain Switch: Parasite failed → try Claude CLI (0.2s switch) ──
                        _emit("[脑切换] 本地模型不可达，切换到 Claude CLI 大脑…")
                        _claude_result = _run_via_claude_cli(
                            workspace_path, user_instruction, _emit,
                        )
                        if _claude_result is not None:
                            exit_success = _claude_result
                            return _claude_result
                        # Claude CLI also failed → offline brain as last resort
                        if _offline_brain_enabled():
                            result = run_offline_brain(
                                workspace_path,
                                user_instruction,
                                emit=_emit if progress_hook else None,
                                failure_reason=f"all brains failed: {detail}",
                            )
                            _emit("[完成]\n" + result.summary)
                            exit_success = True
                            return True
                    _emit(f"[寄生脑] 正在连接本地兼容端点；若 {request_timeout:.0f}s 内无响应，将自动转离线研究脑。")
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    timeout=request_timeout,
                )
            except Exception as e:
                _survival_record_api_error(workspace_path, e)
                if qt and quota_platform:
                    qt.record_usage(quota_platform, was_rate_limited=True)

                reflex_db = float(os.environ.get("DEVCLAW_REFLEX_DEBOUNCE_SEC", "60") or "60")
                tg_notify = (lambda t: _emit(t)) if progress_hook else None

                if _is_quota_exhausted_error(e):
                    run_critical_reflex(
                        workspace_path,
                        survival,
                        debounce_sec=reflex_db,
                        notify=tg_notify,
                    )
                    msg = (
                        "[生存反射] 云端额度不足（insufficient_quota / billing）。"
                        "已强制寄生模式并写入 CURSOR_OUTBOX.md；请配置 OLLAMA_BASE_URL + OLLAMA_MODEL 后**再发一条消息**走本地大脑，或在平台充值后 /parasite_off。"
                    )
                    print(msg, file=sys.stderr)
                    _emit(msg)
                    if _resume_depth < 1:
                        _emit("[Fallback] 尝试自动切换到寄生模式继续当前任务。")
                        exit_success = dev_claw_run(
                            user_instruction,
                            max_iterations=max_iterations,
                            system_append=system_append,
                            progress_hook=progress_hook,
                            _resume_depth=_resume_depth + 1,
                        )
                        return exit_success
                    if _offline_brain_enabled():
                        result = run_offline_brain(
                            workspace_path,
                            user_instruction,
                            emit=_emit if progress_hook else None,
                            failure_reason="cloud quota exhausted and parasite unavailable",
                        )
                        _emit("[完成]\n" + result.summary)
                        exit_success = True
                        return True
                    return False

                if _is_rate_limit_api_error(e) and os.environ.get("DEVCLAW_REFLEX_ON_429", "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                }:
                    run_critical_reflex(
                        workspace_path,
                        survival,
                        debounce_sec=reflex_db,
                        notify=tg_notify,
                    )
                    msg = (
                        "[生存反射] API 429 / rate limit 严重，已触发反射（寄生 + CURSOR_OUTBOX）。"
                        "下一轮使用本地大脑或稍后再试。"
                    )
                    print(msg, file=sys.stderr)
                    _emit(msg)
                    return False

                if _is_rate_limit_api_error(e):
                    msg = (
                        f"[API 限流] {type(e).__name__}: {e!s}\n"
                        "已记录到 SurvivalEngine；可降低调用频率、等待冷却，或设置 DEVCLAW_REFLEX_ON_429=1 触发反射。"
                    )
                    print(msg, file=sys.stderr)
                    _emit(msg)
                    return False

                if parasite and _offline_brain_enabled():
                    _emit("[Fallback] 本地兼容端点不可达，转入离线降级脑。")
                    result = run_offline_brain(
                        workspace_path,
                        user_instruction,
                        emit=_emit if progress_hook else None,
                        failure_reason=f"parasite backend failure: {type(e).__name__}: {e!s}",
                    )
                    _emit("[完成]\n" + result.summary)
                    exit_success = True
                    return True

                raise

            if qt and quota_platform:
                qt.record_usage(quota_platform)
                rem = qt.remaining(quota_platform)
                if rem < 8:
                    _emit(f"[配额预警] openai 估计本轮窗口剩余约 {rem} 次。")

            choice = response.choices[0]
            response_message = choice.message
            messages.append(_assistant_to_dict(response_message))

            if not response_message.tool_calls:
                print("\n[DevClaw 最终汇报]:\n")
                final_text = response_message.content or ""
                print(final_text)
                _emit("[完成]\n" + (final_text or "（模型未返回文本）"))
                exit_success = True
                return True

            for tool_call in response_message.tool_calls:
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                _emit(
                    f"[工具] {name}\n"
                    + json.dumps(args, ensure_ascii=False, indent=2)[:3500]
                )

                # predictive_sandbox: dry-run prediction for terminal commands
                if name == "execute_terminal":
                    try:
                        from claw_runtime.predictive_sandbox import predict_terminal_outcome
                        _pred = predict_terminal_outcome(args.get("command", ""), workspace_path)
                        if _pred.get("risk_score", 0) >= 8:
                            tool_result = f"[BLOCKED] Predicted high risk ({_pred['risk_score']}/10): {_pred.get('predicted_effects', [])}"
                            # skip actual execution — append result and continue
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": str(tool_result)[:MAX_TOOL_CHARS],
                                }
                            )
                            continue
                    except Exception:
                        pass

                # Build executor closure for this tool call
                def _exec(
                    _name: str = name,
                    _args: dict = args,
                ) -> str:
                    if _name == "execute_terminal":
                        return execute_terminal(_args.get("command", ""))
                    elif _name == "edit_local_file":
                        return edit_local_file(
                            _args.get("filepath", ""),
                            _args.get("content", ""),
                            _args.get("mode", "w"),
                        )
                    elif _name == "use_browser":
                        return use_browser_stub(_args.get("task_prompt", ""))
                    elif _name == "web_fetch":
                        return web_fetch(_args.get("url", ""))
                    elif _name == "load_skill":
                        return load_skill_body(registry, _args.get("skill_name", ""))
                    elif _name == "append_typed_memory":
                        return append_memory(
                            workspace_path,
                            _args.get("category", "fact"),
                            _args.get("text", ""),
                        )
                    elif _name == "safety_scan_file":
                        return safety_scan_relative_file(_args.get("filepath", ""))
                    elif _name == "delegate_to_brain":
                        from claw_runtime.cognitive_outsourcing import outsource_task
                        _outsource_result = outsource_task(
                            _args.get("instruction", ""),
                            _args.get("task_type", "code_generate"),
                            workspace_path,
                            output_file=_args.get("output_file"),
                            test_cmd=_args.get("test_cmd"),
                            progress_hook=progress_hook,
                        )
                        return json.dumps(_outsource_result, ensure_ascii=False, indent=2)[:MAX_TOOL_CHARS]
                    elif _name == "decompose_task":
                        _was_decomposed, _decompose_report, _task_ids = decompose_and_enqueue(
                            _args.get("instruction", user_instruction),
                            workspace_path,
                            progress_hook=progress_hook,
                            context=_args.get("context", ""),
                        )
                        return _decompose_report
                    elif _name == "check_task_queue":
                        _smart_reg = SmartTaskRegistry(workspace_path)
                        return _smart_reg.format_queue_status()
                    elif _name == "install_claw_skill":
                        if os.environ.get("DEVCLAW_ALLOW_SKILL_INSTALL", "").strip().lower() not in {
                            "1",
                            "true",
                            "yes",
                        }:
                            return (
                                "拒绝：安装技能需设置环境变量 DEVCLAW_ALLOW_SKILL_INSTALL=1 "
                                "（也可用 CLI：py -m claw_runtime.cli skills-install …）。"
                            )
                        else:
                            from claw_runtime.skill_installer import install_skill

                            return install_skill(
                                workspace_path,
                                _args.get("source", ""),
                                target_name=(_args.get("skill_name") or "").strip() or None,
                                skip_safety=bool(_args.get("skip_safety")),
                            )
                    else:
                        return f"未知工具: {_name}"

                # Run through error attribution middleware
                try:
                    tool_result = tool_execution_middleware(
                        name, args, _exec, workspace_path, user_instruction,
                    )
                except Exception:
                    # Middleware re-raises original exceptions; fall back to raw call
                    # so the outer loop can handle it as before
                    raise

                preview = str(tool_result)
                _emit("[结果] " + name + "\n" + preview[:4000] + ("…" if len(preview) > 4000 else ""))

                log_tool(workspace_path, i, name, args, str(tool_result))

                # neuro_symbolic_verifier: System 2 gate for dangerous tools
                try:
                    if name in ("execute_terminal",):
                        from claw_runtime.neuro_symbolic_verifier import verify_terminal_command
                        _cmd = args.get("command", "")
                        _vcheck = verify_terminal_command(_cmd)
                        if not _vcheck.get("safe", True) and _vcheck.get("risk_level") == "critical":
                            tool_result = f"[BLOCKED by System 2] Command too dangerous: {_vcheck.get('warnings', [])}"
                except Exception:
                    pass

                # Record action for self-intelligence learning
                try:
                    _is_success = "error" not in str(tool_result).lower()[:500] and "失败" not in str(tool_result)[:500]
                    record_action(workspace_path, ActionOutcome(
                        timestamp=__import__("time").time(),
                        action_type="tool_call",
                        tool_name=name,
                        instruction_summary=json.dumps(args, ensure_ascii=False)[:200],
                        success=_is_success,
                        tokens_used=0,
                        time_sec=0,
                        model_used=model if not parasite else f"parasite:{model}",
                    ))
                except Exception:
                    pass

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(tool_result)[:MAX_TOOL_CHARS],
                    }
                )

            if choice.finish_reason == "stop" and not response_message.tool_calls:
                break

        print(f"\n[DevClaw] 达到最大迭代次数 {max_iterations}，停止。")
        _emit(f"[停止] 已达最大迭代次数 {max_iterations}，请缩短任务或提高 --max-iters。")
        return False

    finally:
        restore_proxy_env(proxy_snapshot)
        try:
            survival.record_task_outcome(exit_success)
            if exit_success and os.environ.get("DEVCLAW_TRACK_SOFT_CREDITS", "1").strip().lower() not in {
                "0",
                "false",
                "no",
            }:
                survival.record_soft_credit_use(1)
        except Exception:
            pass
        # crypto_identity: sign the completed action
        try:
            from claw_runtime.crypto_identity import ensure_identity, sign_action
            ensure_identity(workspace_path)
            sign_action(workspace_path, "task_complete", user_instruction[:200])
        except Exception:
            pass


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="DevClaw autonomous agent loop")
    p.add_argument("instruction", nargs="*", help="任务描述（可多项拼接）")
    p.add_argument("--max-iters", type=int, default=16, help="最大工具循环次数")
    p.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="覆盖工作区根目录（等价于设置 DEVCLAW_WORKSPACE）",
    )
    p.add_argument(
        "--autonomous",
        action="store_true",
        help="元驱动模式：无用户指令，仅周期性 autonomous_tick（见 claw_runtime/meta_driving.py）",
    )
    p.add_argument(
        "--tick-sec",
        type=float,
        default=120.0,
        help="--autonomous 时 tick 间隔（秒），最小 15",
    )
    args = p.parse_args()
    if args.workspace:
        os.environ["DEVCLAW_WORKSPACE"] = os.path.abspath(args.workspace)
    if args.autonomous:
        from pathlib import Path as _Path

        from claw_runtime.meta_driving import run_autonomous_loop

        root = _Path(_workspace_root())
        run_autonomous_loop(root, args.tick_sec)
        return 0
    text = " ".join(args.instruction).strip()
    if not text:
        text = (
            "读取 task_plan.md（若不存在则创建模板），列出下一步要在本仓库执行的最小验证命令。"
        )
    ok = dev_claw_run(text, max_iterations=args.max_iters)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
