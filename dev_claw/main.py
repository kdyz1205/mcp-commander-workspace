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

from claw_runtime.memory import append_memory
from claw_runtime.offline_brain import run_offline_brain
from claw_runtime.task_complexity_detector import (
    intercept_if_grand,
    on_consecutive_failures,
    pop_next_atom_task,
)
from claw_runtime.sandbox_docker import docker_enabled, run_shell_in_docker
from claw_runtime.session_log import log_tool
from claw_runtime.skill_registry import SkillRegistry
from claw_runtime.quota_tracker import QuotaTracker
from claw_runtime.survival_engine import SurvivalEngine, SurvivalState
from claw_runtime.survival_reflex import run_critical_reflex
from claw_runtime.ultimate.nomad import load_nomad_system_append
from claw_runtime.ultimate.proxy_env import (
    apply_proxy_env,
    current_proxy_env,
    restore_proxy_env,
    snapshot_proxy_env,
)
from claw_runtime.ultimate.self_heal import emit_rebuild_venv_scripts


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


def _probe_compatible_backend(base_url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    probe_url = base_url.rstrip("/") + "/models"
    req = Request(probe_url, headers={"User-Agent": "DevClaw-parasite-probe/1.0"}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {getattr(resp, 'status', 200)}"
    except HTTPError as exc:
        if exc.code in {401, 403, 404, 405}:
            return True, f"HTTP {exc.code}"
        return False, f"HTTPError {exc.code}: {exc.reason}"
    except URLError as exc:
        return False, f"URLError: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc!s}"


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

    _ensure_utf8_stdio()
    ws = _workspace_root()
    workspace_path = Path(ws)
    survival = SurvivalEngine(workspace_path)
    exit_success = False
    proxy_snapshot = snapshot_proxy_env()

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
            "多阶段编排：py -m claw_runtime.cli multi-agent …\n"
            "\n【任务复杂度嗅探】系统内置了任务复杂度检测器（task_complexity_detector）。"
            "当收到宏大/模糊任务（如'重构整个项目'、'开发新系统'）时，系统会自动拦截并拆解为原子任务队列。"
            "你应该专注于当前阶段的工作，不要试图一次完成所有事情。"
            "如果连续失败 2 次，系统会强制进入推理模式，在 task_plan.md 中写下逻辑分析。"
            "如果发现知识盲区（连续失败 3+ 次），系统会自动调用 research_lab 搜索解决方案。"
        )
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

        # ── Task Complexity Detector: intercept grand tasks ──
        decomposition_plan = intercept_if_grand(
            workspace_path,
            user_instruction,
            emit=_emit if progress_hook else None,
        )
        active_instruction = user_instruction
        if decomposition_plan:
            # Grand task intercepted → rewrite instruction to first atom task
            first_atom = decomposition_plan.atom_tasks[0]
            active_instruction = (
                f"[ATOM_TASK][自动拆解阶段 1/{len(decomposition_plan.atom_tasks)}] {first_atom.title}\n"
                f"{first_atom.description}\n\n"
                "注意: 只完成当前阶段的工作。完成后系统会自动从工作队列取出下一阶段。"
            )
            _emit(
                f"老板，任务太大，我已经自动将其拆分为 {len(decomposition_plan.atom_tasks)} 个阶段"
                "并排入我的工作队列。我现在开始执行阶段一。"
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": base_core + "\n\n" + catalog},
            {"role": "user", "content": active_instruction},
        ]

        survival_gate = os.environ.get("DEVCLAW_SURVIVAL_GATE", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }

        # Track consecutive failures within this run for failure-triggered reasoning
        _consecutive_fail_count = 0
        _last_error_text = ""

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
                    probe_timeout = min(3.0, max(1.0, request_timeout / 2.0))
                    ok, detail = _probe_compatible_backend(parasite_base_url or "", timeout=probe_timeout)
                    if not ok and _offline_brain_enabled():
                        _emit(f"[寄生脑] 本地兼容端点预探测失败（{detail}），直接转离线研究脑。")
                        result = run_offline_brain(
                            workspace_path,
                            user_instruction,
                            emit=_emit if progress_hook else None,
                            failure_reason=f"parasite backend probe failure: {detail}",
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

                # ── Failure-triggered reasoning (consecutive fail >= 2) ──
                _consecutive_fail_count += 1
                _last_error_text = str(e)[:500]
                reasoning_path = on_consecutive_failures(
                    workspace_path,
                    active_instruction,
                    _consecutive_fail_count,
                    _last_error_text,
                )
                if reasoning_path:
                    _emit(
                        f"[推理引擎] 连续失败 {_consecutive_fail_count} 次，"
                        f"已在 task_plan.md 写入 ≥50 字逻辑分析。"
                    )

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

                # ── Queue-driven self-dispatch: check for next atom task ──
                next_task = pop_next_atom_task(workspace_path)
                if next_task:
                    phase_info = next_task.get("meta", {})
                    phase_num = phase_info.get("phase", "?")
                    total = phase_info.get("total_phases", "?")
                    next_title = phase_info.get("title", "")
                    _emit(
                        f"[完成阶段 {phase_num - 1 if isinstance(phase_num, int) and phase_num > 1 else '?'}/{total}]\n"
                        + (final_text or "（模型未返回文本）")
                        + f"\n\n🔄 工作队列中还有任务。自动开始阶段 {phase_num}: {next_title}"
                    )
                    # Recursively execute next atom task
                    next_instruction = next_task.get("text", "")
                    if next_instruction and _resume_depth < 5:
                        exit_success = dev_claw_run(
                            next_instruction,
                            max_iterations=max_iterations,
                            system_append=system_append,
                            progress_hook=progress_hook,
                            _resume_depth=_resume_depth + 1,
                        )
                        return exit_success
                else:
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

                if name == "execute_terminal":
                    tool_result = execute_terminal(args.get("command", ""))
                elif name == "edit_local_file":
                    tool_result = edit_local_file(
                        args.get("filepath", ""),
                        args.get("content", ""),
                        args.get("mode", "w"),
                    )
                elif name == "use_browser":
                    tool_result = use_browser_stub(args.get("task_prompt", ""))
                elif name == "web_fetch":
                    tool_result = web_fetch(args.get("url", ""))
                elif name == "load_skill":
                    tool_result = load_skill_body(registry, args.get("skill_name", ""))
                elif name == "append_typed_memory":
                    tool_result = append_memory(
                        workspace_path,
                        args.get("category", "fact"),
                        args.get("text", ""),
                    )
                elif name == "safety_scan_file":
                    tool_result = safety_scan_relative_file(args.get("filepath", ""))
                elif name == "install_claw_skill":
                    if os.environ.get("DEVCLAW_ALLOW_SKILL_INSTALL", "").strip().lower() not in {
                        "1",
                        "true",
                        "yes",
                    }:
                        tool_result = (
                            "拒绝：安装技能需设置环境变量 DEVCLAW_ALLOW_SKILL_INSTALL=1 "
                            "（也可用 CLI：py -m claw_runtime.cli skills-install …）。"
                        )
                    else:
                        from claw_runtime.skill_installer import install_skill

                        tool_result = install_skill(
                            workspace_path,
                            args.get("source", ""),
                            target_name=(args.get("skill_name") or "").strip() or None,
                            skip_safety=bool(args.get("skip_safety")),
                        )
                else:
                    tool_result = f"未知工具: {name}"

                preview = str(tool_result)
                _emit("[结果] " + name + "\n" + preview[:4000] + ("…" if len(preview) > 4000 else ""))

                log_tool(workspace_path, i, name, args, str(tool_result))

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
