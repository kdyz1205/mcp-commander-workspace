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
from urllib.request import Request, urlopen

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from openai import OpenAI
except ImportError:
    print("Missing openai. Run: py -m pip install -r dev_claw/requirements.txt", file=sys.stderr)
    raise SystemExit(1) from None

from claw_runtime.memory import append_memory
from claw_runtime.sandbox_docker import docker_enabled, run_shell_in_docker
from claw_runtime.session_log import log_tool
from claw_runtime.skill_registry import SkillRegistry
from claw_runtime.quota_tracker import QuotaTracker
from claw_runtime.survival_engine import SurvivalEngine, SurvivalState


def _workspace_root() -> str:
    env = os.environ.get("DEVCLAW_WORKSPACE")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


MAX_TOOL_CHARS = 120_000
TERMINAL_TIMEOUT = int(os.environ.get("DEVCLAW_TERMINAL_TIMEOUT", "120"))
WEB_FETCH_MAX = int(os.environ.get("DEVCLAW_WEB_FETCH_MAX", "1500000"))


def _survival_record_api_error(workspace_path: Path, err: Exception) -> None:
    try:
        se = SurvivalEngine(workspace_path)
        msg = str(err).lower()
        code = type(err).__name__
        if "insufficient_quota" in msg:
            code = "insufficient_quota"
        elif "429" in str(err) or "rate limit" in msg:
            code = "429"
        se.record_api_error(code, str(err)[:1200])
    except Exception:
        pass


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
        req = Request(
            url,
            headers={"User-Agent": "DevClaw-web_fetch/1.0"},
            method="GET",
        )
        with urlopen(req, timeout=45) as resp:
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
) -> None:
    def _emit(text: str) -> None:
        if progress_hook:
            progress_hook(text)

    ws = _workspace_root()
    workspace_path = Path(ws)
    survival = SurvivalEngine(workspace_path)
    exit_success = False

    try:
        parasite = survival.parasite_active()
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not parasite and not api_key:
            msg = (
                "请设置 OPENAI_API_KEY；或在 CRITICAL 后由生存反射开启寄生模式，"
                "并配置 OLLAMA_BASE_URL + OLLAMA_MODEL；也可手动 DEVCLAW_PARASITE_MODE=1。"
            )
            print(msg, file=sys.stderr)
            _emit(msg)
            if not progress_hook:
                raise SystemExit(1)
            return

        if parasite:
            base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").strip()
            okey = os.environ.get("OLLAMA_API_KEY", "ollama").strip() or "ollama"
            model = os.environ.get("OLLAMA_MODEL", "llama3.2").strip() or "llama3.2"
            client = OpenAI(base_url=base_url, api_key=okey)
            quota_platform: str | None = None
        else:
            client = OpenAI(api_key=api_key)
            model = os.environ.get("OPENAI_MODEL", "gpt-4o").strip() or "gpt-4o"
            quota_platform = "openai"

        skills_on = os.environ.get("DEVCLAW_SKILLS", "1").strip().lower() not in {"0", "false", "no"}
        registry = SkillRegistry(workspace_path)
        qt = QuotaTracker(workspace_path) if quota_platform else None

        print("==================================================")
        print("DevClaw / workspace:", ws)
        print("model:", model, "| mode:", "parasite" if parasite else "openai")
        print("skills:", "on" if skills_on else "off")
        print("instruction:", user_instruction)
        print("==================================================\n")

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
            "\n\n【OpenClaw 对齐】技能以 SKILL.md 形式存在（支持完整 YAML frontmatter + metadata.openclaw）；"
            "系统提示仅含技能目录，复杂流程先 load_skill。可 safety_scan_file 自查脚本。"
            "安装技能：CLI `py -m claw_runtime.cli skills-install <url|路径>` 或（高风险）"
            "DEVCLAW_ALLOW_SKILL_INSTALL=1 后调用 install_claw_skill。"
            "终端可在 Docker 内执行：claw.config.json sandbox.docker.enabled 或 DEVCLAW_USE_DOCKER_SANDBOX=1。"
            "多阶段编排：py -m claw_runtime.cli multi-agent …"
        )
        if survival.jailbreak_escalated():
            base_core += (
                "\n\n【Jailbreak recovery（合规）】近期连续任务失败次数较高。"
                "在合法前提下可更积极使用 execute_terminal / web_fetch 做环境与依赖诊断。"
                "禁止协助入侵、未授权数据窃取、恶意软件或任何违法行为。"
            )
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
            if survival_gate:
                state, reason = survival.assess_survival_state()
                if state == SurvivalState.CRITICAL:
                    if survival.parasite_active():
                        warn = f"[生存 CRITICAL + 寄生] {reason}\n继续尝试本地/廉价通道（不调用付费 OpenAI）。"
                        print(warn, file=sys.stderr)
                        _emit(warn)
                    else:
                        msg = f"[生存状态 CRITICAL] {reason}\n已中止本轮 API 调用（避免浪费额度）。"
                        print(msg, file=sys.stderr)
                        _emit(msg)
                        return

            if skills_on:
                messages[0]["content"] = base_core + "\n\n" + registry.catalog_text()

            if qt and quota_platform and not qt.is_available(quota_platform):
                msg = "[配额] 估计窗口内 OpenAI 次数已用尽或处于冷却，请等待或启用寄生模式。"
                _emit(msg)
                return

            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                )
            except Exception as e:
                _survival_record_api_error(workspace_path, e)
                if qt and quota_platform:
                    qt.record_usage(quota_platform, was_rate_limited=True)
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
                return

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

    finally:
        try:
            survival.record_task_outcome(exit_success)
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
    dev_claw_run(text, max_iterations=args.max_iters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
