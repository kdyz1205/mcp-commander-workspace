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
from claw_runtime.session_log import log_tool
from claw_runtime.skill_registry import SkillRegistry


def _workspace_root() -> str:
    env = os.environ.get("DEVCLAW_WORKSPACE")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


MAX_TOOL_CHARS = 120_000
TERMINAL_TIMEOUT = int(os.environ.get("DEVCLAW_TERMINAL_TIMEOUT", "120"))
WEB_FETCH_MAX = int(os.environ.get("DEVCLAW_WEB_FETCH_MAX", "1500000"))


def execute_terminal(command: str) -> str:
    """Run a shell command (cwd = workspace). High privilege: review commands carefully."""
    root = _workspace_root()
    print(f"\n[terminal] {command}")
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

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        msg = "请设置环境变量 OPENAI_API_KEY"
        print(msg, file=sys.stderr)
        _emit(msg)
        if not progress_hook:
            raise SystemExit(1)
        return

    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    client = OpenAI(api_key=api_key)
    ws = _workspace_root()
    workspace_path = Path(ws)
    skills_on = os.environ.get("DEVCLAW_SKILLS", "1").strip().lower() not in {"0", "false", "no"}
    registry = SkillRegistry(workspace_path)

    print("==================================================")
    print("DevClaw / workspace:", ws)
    print("model:", model)
    print("skills:", "on" if skills_on else "off")
    print("instruction:", user_instruction)
    print("==================================================\n")

    _emit(
        "DevClaw 启动\n"
        f"工作区: {ws}\n"
        f"模型: {model}\n"
        f"任务: {user_instruction[:2000]}"
        + ("…" if len(user_instruction) > 2000 else "")
    )

    base_core = (
        "你是顶级全栈工程师，可在用户工作区内执行终端命令与读写文件。"
        "优先小步验证：先读再改，再运行测试。遇到连续失败要分析日志并调整。"
        "文件路径一律使用相对工作区根的 POSIX 风格或 Windows 相对路径（如 tools\\\\x.py）。"
        "需要浏览器时先调用 use_browser 了解占位说明，或 web_fetch 拉公开文档。"
        "\n\n【OpenClaw 对齐】技能以 SKILL.md 形式存在；系统提示仅含技能目录，"
        "执行前对复杂流程请 load_skill 读取全文。新建技能放入 ./skills/<name>/SKILL.md 会在下一轮自动生效（热重载）。"
    )
    if system_append:
        base_core = base_core + "\n\n" + system_append.strip()

    catalog = registry.catalog_text() if skills_on else "## Skills\n(disabled via DEVCLAW_SKILLS=0)\n"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": base_core + "\n\n" + catalog},
        {"role": "user", "content": user_instruction},
    ]

    for i in range(max_iterations):
        if skills_on:
            messages[0]["content"] = base_core + "\n\n" + registry.catalog_text()

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        choice = response.choices[0]
        response_message = choice.message
        messages.append(_assistant_to_dict(response_message))

        if not response_message.tool_calls:
            print("\n[DevClaw 最终汇报]:\n")
            final_text = response_message.content or ""
            print(final_text)
            _emit("[完成]\n" + (final_text or "（模型未返回文本）"))
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
    args = p.parse_args()
    if args.workspace:
        os.environ["DEVCLAW_WORKSPACE"] = os.path.abspath(args.workspace)
    text = " ".join(args.instruction).strip()
    if not text:
        text = (
            "读取 task_plan.md（若不存在则创建模板），列出下一步要在本仓库执行的最小验证命令。"
        )
    dev_claw_run(text, max_iterations=args.max_iters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
