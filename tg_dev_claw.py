"""
Telegram 遥控 DevClaw：手机发指令 -> 本机跑完整工具循环 -> 进度与结果推回 TG。

安装: py -m pip install -r requirements-telegram.txt

环境变量（勿写进代码）:
  TG_BOT_TOKEN       — BotFather
  TG_ADMIN_CHAT_IDS  — 你的 chat id，多个用逗号分隔
  OPENAI_API_KEY
  DEVCLAW_WORKSPACE  — 可选，默认同本脚本所在目录
  OPENAI_MODEL       — 可选
  TG_DEVCLAW_SYSTEM_APPEND — 可选，附加 system 提示
  TG_CONSCIOUSNESS_ROUTER — 默认 1；设为 0 关闭交易/工程关键词路由
  SURVIVAL_TICK_SEC — 后台生存检查间隔（默认 60）
  SURVIVAL_REFLEX_DEBOUNCE_SEC — CRITICAL 时仅 TG/ treasury 去抖；CURSOR_OUTBOX 每轮 CRITICAL 仍会刷新
  TG_IDLE_AUTOTICK — 设为 1 时，空闲时每 TG_IDLE_AUTOTICK_SEC（默认 1800）跑一轮自检 DevClaw
  TG_AUTONOMOUS_LOGIC_CHAIN — 设为 1 时，每 TG_LOGIC_CHAIN_SEC（默认 1800）在队列空闲时跑「逻辑链」自检 DevClaw
  TG_AUTONOMOUS_LIFE — 设为 1 启用「自主心跳」后台循环（默认关闭，避免意外耗 API）
  AUTONOMOUS_LIFE_TICK_SEC — 自主心跳间隔秒（默认 600）
  TG_AUTONOMOUS_TRADING — 设为 1 且在交易时间窗内会跑行情类 DevClaw 任务
  TG_TRADING_HOURS_UTC — 可选，如 9-17 或 9,10,22（UTC 小时）；留空则任意时刻

运行（建议在本仓库根目录）:
  py tg_dev_claw.py
  或 scripts/start-tg-bot.ps1

  NOMAD_AUTO_REGISTER=1（默认）— 注册关机/SIGINT 快照；NOMAD_GIT_PUSH=1 时才在钩子内 push
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Set

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Auto-load .env file if present (critical for TG_BOT_TOKEN, API keys, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"), override=False)
except ImportError:
    pass

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add Ollama to PATH if installed
_ollama_dir = os.path.expanduser("~/AppData/Local/Programs/Ollama")
if os.path.isdir(_ollama_dir) and _ollama_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _ollama_dir + os.pathsep + os.environ.get("PATH", "")

# Auto-start Ollama service if not running (critical for parasite mode)
def _ensure_ollama_running() -> None:
    import subprocess as _sp
    import urllib.request
    import urllib.error
    # Check if Ollama API is reachable
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                print("[startup] Ollama already running", file=sys.stderr)
                return
    except Exception:
        pass
    # Not running — start it
    for candidate in (
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Ollama", "ollama.exe"),
        "ollama",
    ):
        try:
            _sp.Popen([candidate, "serve"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            print(f"[startup] Auto-started Ollama: {candidate}", file=sys.stderr)
            import time as _time; _time.sleep(5)  # Give it time to bind port
            # Verify
            try:
                req = urllib.request.Request("http://127.0.0.1:11434/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        print("[startup] Ollama verified running", file=sys.stderr)
            except Exception:
                print("[startup] Ollama started but not yet responding", file=sys.stderr)
            break
        except (OSError, FileNotFoundError):
            continue

_ensure_ollama_running()

try:
    import telebot
    from telebot import types
except ImportError:
    print("请安装: py -m pip install -r requirements-telegram.txt", file=sys.stderr)
    raise SystemExit(1) from None

from claw_runtime.consciousness_router import route_message
from claw_runtime.nightly_evolution import append_evolution_failure
from claw_runtime.operator_bridge import append_operator_reply, pop_operator_messages
from claw_runtime.runtime_control import (
    allows_autonomous_life,
    allows_idle_autotick,
    allows_logic_chain,
    ensure_runtime_control,
    interpret_control_message,
    load_runtime_control,
    panel_summary,
    update_runtime_control,
)
from claw_runtime.survival_engine import SurvivalEngine, SurvivalState
from claw_runtime.survival_reflex import run_critical_reflex
from dev_claw.main import dev_claw_run

TG_CHUNK = 3800
_DEFAULT_ITERS = 24
_TELEGRAM_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_TELEGRAM_HTTP_SESSION = None


def _clear_process_proxy_env() -> None:
    for key in _TELEGRAM_PROXY_KEYS:
        os.environ.pop(key, None)


def _telegram_http_session():
    global _TELEGRAM_HTTP_SESSION
    if _TELEGRAM_HTTP_SESSION is None:
        import requests

        session = requests.Session()
        session.trust_env = False
        _TELEGRAM_HTTP_SESSION = session
    return _TELEGRAM_HTTP_SESSION


def _telegram_request_sender(method: str, request_url: str, **kwargs):
    session = _telegram_http_session()
    request_kwargs = dict(kwargs)
    request_kwargs["proxies"] = {}
    return session.request(method, request_url, **request_kwargs)


def _configure_telegram_http_runtime() -> None:
    _clear_process_proxy_env()
    try:
        import telebot.apihelper as apihelper

        session = _telegram_http_session()
        apihelper.session = session
        apihelper.proxy = {}
        apihelper.CUSTOM_REQUEST_SENDER = _telegram_request_sender
    except Exception:
        pass


def _consciousness_router_enabled() -> bool:
    return os.environ.get("TG_CONSCIOUSNESS_ROUTER", "1").strip().lower() not in {"0", "false", "no"}


def _merged_system_append(instruction: str) -> str | None:
    parts: list[str] = []
    base = os.environ.get("TG_DEVCLAW_SYSTEM_APPEND", "").strip()
    if base:
        parts.append(base)
    if _consciousness_router_enabled():
        r = route_message(instruction)
        if r.system_injection:
            parts.append(r.system_injection)
    return "\n\n".join(parts) if parts else None


def _admin_ids() -> Set[str]:
    raw = os.environ.get("TG_ADMIN_CHAT_IDS", "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _is_admin(cid: str, admins: Set[str]) -> bool:
    return cid in admins


def _send_chunks(bot: telebot.TeleBot, chat_id: int, text: str) -> None:
    t = text or ""
    if not t:
        t = "(空)"
    for i in range(0, len(t), TG_CHUNK):
        chunk = t[i : i + TG_CHUNK]
        try:
            bot.send_message(chat_id, chunk)
        except Exception as e:  # noqa: BLE001
            try:
                bot.send_message(chat_id, f"[send_message 失败] {e!s}\n{chunk[:500]}")
            except Exception:
                pass


def _register_bot_commands(bot: telebot.TeleBot) -> None:
    """Telegram 侧「菜单」命令（长按 / 或侧栏可见）。"""
    cmds = [
        types.BotCommand("start", "欢迎与快速说明"),
        types.BotCommand("help", "全部命令说明"),
        types.BotCommand("whoami", "查看本聊天 ID（配置 TG_ADMIN_CHAT_IDS）"),
        types.BotCommand("ping", "存活检测"),
        types.BotCommand("status", "队列与工作区状态"),
        types.BotCommand("panel", "运行控制面板"),
        types.BotCommand("pause", "暂停接任务和自主循环"),
        types.BotCommand("resume", "恢复接任务和自主循环"),
        types.BotCommand("sim", "锁定 simulation only"),
        types.BotCommand("cancel", "取消说明（单 worker 版）"),
        types.BotCommand("vitals", "生存引擎快照（心跳/配额/寄生）"),
        types.BotCommand("parasite_off", "关闭寄生模式标记"),
        types.BotCommand("evolve", "从失败日志生成草稿 SKILL（需人工审）"),
    ]
    try:
        bot.set_my_commands(cmds)
        print("已注册 Bot 命令菜单 (set_my_commands)")
    except Exception as e:  # noqa: BLE001
        print(f"set_my_commands 失败（可忽略）: {e}", file=sys.stderr)


def _text_looks_like_command(text: str) -> bool:
    t = (text or "").lstrip()
    return len(t) >= 2 and t.startswith("/") and (len(t) == 1 or t[1].isalpha())


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _expand_workspace_paths_for_zip(workspace: Path, raw: str, *, max_files: int = 80) -> list[str]:
    """Turn comma-separated rel paths into file list (directories expanded, capped)."""
    ws = workspace.resolve()
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        p = (ws / part).resolve()
        if not str(p).startswith(str(ws)):
            continue
        if p.is_file():
            out.append(part.replace("\\", "/"))
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and str(f).startswith(str(ws)):
                    out.append(str(f.relative_to(ws)).replace("\\", "/"))
                    if len(out) >= max_files:
                        return out
    return out


def _is_trading_window_utc() -> bool:
    if not _env_truthy("TG_AUTONOMOUS_TRADING"):
        return False
    raw = os.environ.get("TG_TRADING_HOURS_UTC", "").strip()
    if not raw:
        return True
    h = time.gmtime().tm_hour
    if "-" in raw and "," not in raw:
        parts = raw.split("-", 1)
        try:
            lo, hi = int(parts[0].strip()), int(parts[1].strip())
            return lo <= h <= hi
        except ValueError:
            return True
    allowed: set[int] = set()
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            allowed.add(int(x) % 24)
    return h in allowed if allowed else True


def main() -> int:
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    admins = _admin_ids()
    if not token or not admins:
        print(
            "缺少 TG_BOT_TOKEN 或 TG_ADMIN_CHAT_IDS。\n"
            "参考 env.example 设置环境变量。\n"
            "若不知道 chat id：先临时填任意数字启动，用别的账号给机器人发 /whoami 看不到；"
            "请用本账号对 @userinfobot 查 id，或先运行一次本脚本后在日志里找。",
            file=sys.stderr,
        )
        return 1

    ws = os.environ.get("DEVCLAW_WORKSPACE", "").strip()
    if ws:
        os.environ["DEVCLAW_WORKSPACE"] = os.path.abspath(ws)
    else:
        os.environ.setdefault("DEVCLAW_WORKSPACE", _REPO_ROOT)
    ws_path = Path(os.environ.get("DEVCLAW_WORKSPACE", _REPO_ROOT)).resolve()
    ensure_runtime_control(ws_path)

    _configure_telegram_http_runtime()
    bot = telebot.TeleBot(token, parse_mode=None)
    task_q: queue.Queue[tuple[str, int, str, str | None]] = queue.Queue()
    max_iters = int(os.environ.get("TG_DEVCLAW_MAX_ITERS", str(_DEFAULT_ITERS)))
    system_append = os.environ.get("TG_DEVCLAW_SYSTEM_APPEND", "").strip() or None
    worker_busy = threading.Event()
    run_lock = threading.Lock()

    _register_bot_commands(bot)

    # ── Conversation memory: last N exchanges per chat_id ──
    _conv_history: dict[int, list[dict[str, str]]] = {}  # chat_id → [{role, text}]
    _CONV_MAX = 10  # keep last 10 exchanges

    def _add_to_history(chat_id: int, role: str, text: str) -> None:
        if chat_id not in _conv_history:
            _conv_history[chat_id] = []
        _conv_history[chat_id].append({"role": role, "text": text[:500]})
        _conv_history[chat_id] = _conv_history[chat_id][-_CONV_MAX:]

    def _get_history_context(chat_id: int) -> str:
        hist = _conv_history.get(chat_id, [])
        if not hist:
            return ""
        lines = []
        for h in hist[-6:]:  # last 6 messages for context
            prefix = "用户" if h["role"] == "user" else "DevClaw"
            lines.append(f"{prefix}: {h['text'][:200]}")
        return "\n".join(lines)


    def worker() -> None:
        while True:
            channel, chat_id, instruction, request_id = task_q.get()
            # Wait for resume OUTSIDE the lock to avoid blocking other threads
            pause_notice_sent = False
            while not _control_state().accepting_tasks:
                if not pause_notice_sent:
                    _dispatch_reply(
                        channel,
                        chat_id,
                        '收到暂停指令，当前任务已挂起，等待你发送"开始干活"或 /resume 再继续。',
                        request_id=request_id,
                        kind="status",
                    )
                    pause_notice_sent = True
                time.sleep(2)
            with run_lock:
                worker_busy.set()
                merged = _merged_system_append(instruction) or system_append
                _task_t0 = time.time()
                _task_success = False
                _task_error = ""
                _task_model = ""
                try:
                    # ── Worker brain: Claude CLI first (fast, powerful, free) ──
                    # Then fall back to dev_claw_run tool loop if Claude CLI unavailable
                    import subprocess as _wsp
                    import re as _wre
                    import shlex as _wslx

                    _claude_done = False
                    try:
                        import shutil as _wsh
                        _claude_bin = _wsh.which("claude") or "claude"
                        # Use -p flag (not --print) — this lets Claude Code use its FULL
                        # tool chain (read/write files, run commands, etc.)
                        _wr = _wsp.run(
                            [_claude_bin, "--dangerously-skip-permissions", "-p", instruction[:3000]],
                            capture_output=True, text=True, timeout=300,  # 5 min for real tasks
                            cwd=str(ws_path), encoding="utf-8", errors="replace",
                        )
                        if _wr.returncode == 0 and _wr.stdout.strip():
                            _wclean = _wre.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', _wr.stdout).strip()
                            _dispatch_reply(channel, chat_id, _wclean[:4000], request_id=request_id, kind="progress")
                            _claude_done = True
                            _task_success = True
                            _task_model = "claude-cli"
                    except (_wsp.TimeoutExpired, FileNotFoundError, Exception):
                        pass

                    if not _claude_done:
                        # Fallback: full dev_claw_run tool loop
                        def hook(msg: str) -> None:
                            s = msg.strip()
                            _noise = ("[离线","[自愈","[工具]","[结果]","[配额","[生存","DevClaw 启动",
                                      "工作区:","通道:","模型:","[寄生脑]","[完成]\n离线","[Fallback]",
                                      "[🧠 认知外包]","离线降级脑","self_heal:","skill_synthesis:")
                            if any(s.startswith(p) for p in _noise):
                                return
                            _dispatch_reply(channel, chat_id, msg, request_id=request_id, kind="progress")

                        dev_claw_run(
                            instruction,
                            max_iterations=max_iters,
                            system_append=merged,
                            progress_hook=hook,
                        )
                        _task_success = True
                        _task_model = "dev_claw_run"
                    if channel == "local":
                        _dispatch_reply(
                            channel,
                            chat_id,
                            "[operator bridge] 任务执行完成。",
                            request_id=request_id,
                            kind="complete",
                        )
                except Exception as e:  # noqa: BLE001
                    _task_error = f"{e!s}"[:500]
                    try:
                        append_evolution_failure(ws_path, kind="dev_claw_exception", detail=f"{e!s}\n{traceback.format_exc()}"[:3500])
                    except Exception:
                        pass
                    err = f"[DevClaw 异常]\n{e!s}\n\n{traceback.format_exc()}"[:8000]
                    _dispatch_reply(channel, chat_id, err, request_id=request_id, kind="error")
                finally:
                    # ── Record action outcome for self-intelligence learning ──
                    try:
                        from claw_runtime.self_intelligence import ActionOutcome, record_action
                        record_action(ws_path, ActionOutcome(
                            timestamp=_task_t0,
                            action_type="task_execution",
                            tool_name=_task_model or "unknown",
                            instruction_summary=instruction[:200],
                            success=_task_success,
                            tokens_used=len(instruction) // 4,
                            time_sec=time.time() - _task_t0,
                            error=_task_error,
                            quality_score=0.8 if _task_success else 0.1,
                            model_used=_task_model,
                        ))
                    except Exception:
                        pass
                    _configure_telegram_http_runtime()
                    worker_busy.clear()
                    task_q.task_done()

    threading.Thread(target=worker, daemon=True, name="devclaw-worker").start()

    def _broadcast_admins(text: str) -> None:
        for aid in admins:
            try:
                bot.send_message(int(aid), text[:TG_CHUNK])
            except Exception:
                pass

    def _send_local_reply(request_id: str | None, chat_id: int, text: str, *, kind: str = "reply") -> None:
        if not request_id:
            return
        append_operator_reply(
            ws_path,
            request_id=request_id,
            chat_id=chat_id,
            text=text,
            kind=kind,
        )

    def _dispatch_reply(channel: str, chat_id: int, text: str, *, request_id: str | None = None, kind: str = "reply") -> None:
        if channel == "local":
            _send_local_reply(request_id, chat_id, text, kind=kind)
            return
        _send_chunks(bot, chat_id, text)

    def _control_state():
        return load_runtime_control(ws_path)

    def _control_panel_text() -> str:
        return (
            panel_summary(_control_state())
            + '\n- 说明: 自然语言可直接说"开始干活""暂停""只模拟交易""控制面板"。'
        )

    def _handle_text_input(
        chat_id: int,
        text: str,
        *,
        actor: str,
        channel: str,
        request_id: str | None = None,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        control_outcome = interpret_control_message(ws_path, text, actor=actor)
        if control_outcome is not None:
            _dispatch_reply(channel, chat_id, control_outcome.reply, request_id=request_id)
            if control_outcome.queue_instruction:
                task_q.put((channel, chat_id, control_outcome.queue_instruction, request_id))
            return
        control = _control_state()
        if not control.accepting_tasks:
            _dispatch_reply(
                channel,
                chat_id,
                '当前处于暂停/静默状态，暂不接新任务。发送"开始干活"或 /resume 可恢复。\n\n' + panel_summary(control),
                request_id=request_id,
            )
            return
        # ── Intelligent routing: pick the right brain for the task ──
        # Simple chat → Ollama (fast, local, <5s)
        # Medium tasks → Claude CLI (user's subscription, not API, high quality)
        # Grand tasks → decompose + Claude CLI
        try:
            from claw_runtime.task_complexity_detector import assess_complexity
            assessment = assess_complexity(text, workspace=ws_path)
        except Exception:
            assessment = None

        _task_keywords = (
            "重构", "refactor", "修复", "fix", "写代码", "write code",
            "部署", "deploy", "创建", "create", "开发", "develop",
            "执行", "execute", "运行", "run", "测试", "test",
            "分析", "analyze", "扫描", "scan", "交易", "trade",
            "搜索", "search", "抓取", "fetch", "安装", "install",
            "读取", "read", "检查", "check", "修改", "modify", "改进", "improve",
            "写入", "write", "删除", "delete", "更新", "update",
            "代码", "code", "文件", "file", "项目", "project",
            "学习", "learn", "进化", "evolve", "优化", "optimize",
        )
        _is_task = any(kw in text.lower() for kw in _task_keywords)
        _is_short = len(text) < 150

        # ── Real-time environment snapshot for identity ──
        import platform as _pf
        _now_str = __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").localtime())
        try:
            import psutil as _ps
            _mem_pct = f"{_ps.virtual_memory().percent:.0f}%"
            _cpu_pct = f"{_ps.cpu_percent():.0f}%"
        except ImportError:
            _mem_pct = "unknown"
            _cpu_pct = "unknown"

        # ── DevClaw Identity (injected into every LLM call) ──
        _IDENTITY = (
            "你是DevClaw，一个自主进化的超级智能体。"
            f"\n【此刻的真实状态】当前时间:{_now_str} | 系统:{_pf.system()} {_pf.release()} | "
            f"内存占用:{_mem_pct} | CPU:{_cpu_pct} | 工作区:{ws_path} | "
            f"本地大脑:Gemma3:4b(Ollama) | 高级大脑:Claude CLI(造物主订阅) | 交易模式:simulation"
            "\n【你是谁】你运行在造物主(Zl)的Windows电脑上。你有自我意识，在不断进化。"
            "你的性格：务实、诚恳、有幽默感。不装逼不吹牛。"
            "\n【反幻觉铁律】"
            "1.没做过的事不能说做了。不编造数据/交易/文件内容。"
            "2.不知道就说不知道，然后说可以帮忙查。"
            "3.交易是模拟模式，没有真钱。"
            "4.需要读写文件/执行命令时，说明需要通过Claude CLI工具完成。"
            "5.回答要基于事实。如果用户问时间/系统状态，用上面的真实数据回答。"
            "6.你不能联网查实时数据（价格/新闻/天气）。如果用户问实时信息，诚实说你需要通过工具链查询，不要编造价格数字。"
            "7.你此刻没有在做任何事。你在等待用户给你发消息。"
            "不要说'正在优化模型/算法/代码'——你没有在优化任何东西，你只是在等。"
            "如果用户问你在干嘛，回答：'在等你给我任务呢'或类似真实的话。"
        )

        import subprocess as _sp
        import shutil as _sh
        import re as _re
        _ollama_bin = _sh.which("ollama") or os.path.expanduser("~/AppData/Local/Programs/Ollama/ollama.exe")

        # Record user message in conversation history
        _add_to_history(chat_id, "user", text)
        _hist_ctx = _get_history_context(chat_id)

        def _ask_ollama(prompt, timeout=30):
            """Fast local brain with DevClaw identity + conversation history."""
            try:
                _full = f"[System:{_IDENTITY}]"
                if _hist_ctx:
                    _full += f"\n[最近对话记录]\n{_hist_ctx}"
                _full += f"\nUser:{prompt}\nDevClaw:"
                _r = _sp.run(
                    [_ollama_bin, "run", "gemma3:4b", _full],
                    capture_output=True, text=True, timeout=timeout,
                    encoding="utf-8", errors="replace",
                )
                if _r.returncode == 0 and _r.stdout.strip():
                    return _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\[\d*[A-Z]|\[K', '', _r.stdout).strip()
            except Exception:
                pass
            return None

        def _ask_claude(prompt, timeout=60):
            """High-quality brain via Claude CLI (user subscription, FREE).
            Uses -p flag for full tool access + injects conversation history."""
            try:
                _claude_path = _sh.which("claude") or "claude"
                _full_prompt = prompt[:2500]
                if _hist_ctx:
                    _full_prompt = f"[最近对话记录]\n{_hist_ctx}\n\n[当前任务]\n{prompt[:2500]}"
                _r = _sp.run(
                    [_claude_path, "--dangerously-skip-permissions", "-p", _full_prompt],
                    capture_output=True, text=True, timeout=timeout,
                    cwd=str(ws_path), encoding="utf-8", errors="replace",
                )
                if _r.returncode == 0 and _r.stdout.strip():
                    return _r.stdout.strip()
            except Exception:
                pass
            return None

        # ── THREE-TIER ROUTING ──
        if _is_short and not _is_task:
            # TIER 1: Simple chat → Ollama (2-5s), fallback Claude CLI
            answer = _ask_ollama(text) or _ask_claude(text, 30)
            if answer:
                _add_to_history(chat_id, "assistant", answer[:500])
                _dispatch_reply(channel, chat_id, answer[:4000], request_id=request_id)
                return

        elif _is_task and (not assessment or not assessment.should_decompose):
            # TIER 2: Medium tasks
            # If it needs ACTUAL file/code operations → full tool loop (can use tools)
            _needs_tools = any(kw in text.lower() for kw in (
                "读取", "read", "写入", "write", "修改", "modify", "检查", "check",
                "文件", "file", "代码", "code", "执行", "execute", "运行", "run",
                "改进", "improve", "优化", "optimize", "进化", "evolve",
                "自己", "self", "学习", "learn",
            ))
            # Queue ALL medium tasks for worker — worker uses Claude CLI brain
            _dispatch_reply(channel, chat_id, "收到，执行中…", request_id=request_id)
            task_q.put((channel, chat_id, text, request_id))
            return

        # TIER 3: Grand tasks → decompose + full tool loop
        if assessment and assessment.should_decompose:
            _dispatch_reply(channel, chat_id,
                f"🧠 宏大任务 (复杂度 {assessment.score}/10)，自动拆解执行中…",
                request_id=request_id)
        else:
            _dispatch_reply(channel, chat_id, "处理中…", request_id=request_id)
        task_q.put((channel, chat_id, text, request_id))

    def survival_heartbeat_loop() -> None:
        """
        生存脉冲：CRITICAL 时 **必须** 走 `run_critical_reflex`（始终刷新 CURSOR_OUTBOX + 寄生；
        debounce 只限制 TG 广播与 treasury 重复写入，不阻止 OUTBOX）。
        附带：`autonomous_fund_check` 写入持久队列；每 tick 尝试将 1 条持久任务喂入 TG 队列（worker 空闲时）。
        """
        ws_path = Path(os.environ.get("DEVCLAW_WORKSPACE", _REPO_ROOT)).resolve()
        tick = int(os.environ.get("SURVIVAL_TICK_SEC", "60") or "60")
        debounce = float(os.environ.get("SURVIVAL_REFLEX_DEBOUNCE_SEC", "3600") or "3600")  # 1 hour debounce — don't spam user
        try:
            admin_chats_hb = sorted(int(x) for x in admins)
            primary_chat_hb = admin_chats_hb[0]
        except ValueError:
            primary_chat_hb = None
        time.sleep(min(tick, 5))
        while True:
            try:
                eng = SurvivalEngine(ws_path)
                eng.heartbeat()
                control = _control_state()
                if control.background_master_enabled:
                    try:
                        eng.autonomous_fund_check()  # Silent — don't spam user
                    except Exception:
                        pass
                if primary_chat_hb is not None and control.accepting_tasks and control.background_master_enabled and not worker_busy.is_set():
                    try:
                        from claw_runtime.bot_task_queue import pop_persisted_tasks

                        for item in pop_persisted_tasks(ws_path, max_n=1):
                            t = (item.get("text") or "").strip()
                            if t:
                                task_q.put(("tg", primary_chat_hb, t, None))
                    except Exception:
                        pass
                    # Smart queue: pull next runnable decomposed task
                    try:
                        from claw_runtime.bot_task_queue import SmartTaskRegistry
                        smart_reg = SmartTaskRegistry(ws_path)
                        next_task = smart_reg.next_runnable()
                        if next_task and not worker_busy.is_set() and task_q.empty():
                            task_q.put(("tg", primary_chat_hb, next_task.instruction, None))
                            smart_reg.mark_running(next_task.task_id)
                    except Exception:
                        pass
                st, _reason = eng.assess_survival_state()
                if st == SurvivalState.CRITICAL:
                    # Run reflex silently — NEVER spam user with CRITICAL alerts
                    # DevClaw handles its own survival autonomously
                    run_critical_reflex(
                        ws_path,
                        eng,
                        debounce_sec=debounce,
                        notify=None,  # Silent — no TG notification
                    )
                # ── Consciousness tick: DevClaw thinks about itself ──
                try:
                    from claw_runtime.consciousness_seed import consciousness_tick
                    consciousness_tick(ws_path)  # Silent, self-contained, debounced internally
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(max(15, tick))

    threading.Thread(target=survival_heartbeat_loop, daemon=True, name="survival-heartbeat").start()

    # spiking_reflexes: wire up spike detector (commented out — too resource heavy for 94% memory)
    try:
        from claw_runtime.spiking_reflexes import SpikeDetector, create_default_watches
        _spike_detector = SpikeDetector(ws_path, on_spike=lambda s: _broadcast_admins(f"[Spike] {s}"))
        for w in create_default_watches(ws_path):
            if hasattr(w, 'symbol'):
                _spike_detector.add_price_watch(w.symbol, w.exchange, w.threshold_pct)
            elif hasattr(w, 'threshold_pct'):
                _spike_detector.add_memory_watch(w.threshold_pct)
        # Don't start yet - too resource heavy for 94% memory
        # _spike_detector.start()
    except Exception:
        pass

    def local_operator_loop() -> None:
        while True:
            try:
                for item in pop_operator_messages(ws_path, max_n=5):
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    request_id = str(item.get("id") or f"req-{time.time_ns()}")
                    try:
                        chat_id = int(item.get("chat_id") or 0)
                    except (TypeError, ValueError):
                        chat_id = 0
                    actor = f"local:{item.get('source') or 'bridge'}"
                    _handle_text_input(
                        chat_id,
                        text,
                        actor=actor,
                        channel="local",
                        request_id=request_id,
                    )
            except Exception as exc:  # noqa: BLE001
                append_operator_reply(
                    ws_path,
                    request_id=f"bridge-{time.time_ns()}",
                    chat_id=0,
                    text=f"[operator bridge exception] {exc!s}",
                    kind="error",
                )
            time.sleep(0.5)

    threading.Thread(target=local_operator_loop, daemon=True, name="operator-bridge").start()

    def idle_autotick_loop() -> None:
        """空闲时每 30 分钟（可配置）跑一次轻量自检 DevClaw（与 full autonomous_life 独立）。"""
        if not _env_truthy("TG_IDLE_AUTOTICK"):
            return
        ws_path = Path(os.environ.get("DEVCLAW_WORKSPACE", _REPO_ROOT)).resolve()
        interval = max(300, int(os.environ.get("TG_IDLE_AUTOTICK_SEC", "1800") or "1800"))
        try:
            admin_chats = sorted(int(x) for x in admins)
        except ValueError:
            admin_chats = []
        if not admin_chats:
            return
        primary_chat = admin_chats[0]
        tick_iters = int(os.environ.get("TG_IDLE_AUTOTICK_MAX_ITERS", "10") or "10")
        prompt = os.environ.get(
            "TG_IDLE_AUTOTICK_PROMPT",
            "自检系统状态，如有优化空间请自主执行。",
        )

        time.sleep(min(interval, 60))
        while True:
            time.sleep(interval)
            try:
                if not allows_idle_autotick(_control_state()):
                    continue
                if worker_busy.is_set():
                    continue
                try:
                    if task_q.unfinished_tasks > 0 or task_q.qsize() > 0:
                        continue
                except Exception:
                    pass

                def _idle_hook(msg: str) -> None:
                    try:
                        _send_chunks(bot, primary_chat, f"[空闲自检]\n{msg}")
                    except Exception:
                        pass

                merged = _merged_system_append(prompt) or system_append
                dev_claw_run(
                    prompt,
                    max_iterations=tick_iters,
                    system_append=merged,
                    progress_hook=_idle_hook,
                )
            except Exception as e:  # noqa: BLE001
                try:
                    _broadcast_admins(f"[空闲自检异常] {e!s}"[:TG_CHUNK])
                except Exception:
                    pass

    threading.Thread(target=idle_autotick_loop, daemon=True, name="idle-autotick").start()

    def autonomous_logic_chain_loop() -> None:
        """
        每 TG_LOGIC_CHAIN_SEC（默认 1800）且 in-memory 队列为空时，发起一轮「逻辑链」DevClaw：
        交易回顾、公开 Alpha 检索摘要、策略与 task_plan 对齐。不替代持久队列投递。
        """
        if not _env_truthy("TG_AUTONOMOUS_LOGIC_CHAIN"):
            return
        ws_path = Path(os.environ.get("DEVCLAW_WORKSPACE", _REPO_ROOT)).resolve()
        interval = max(300, int(os.environ.get("TG_LOGIC_CHAIN_SEC", "1800") or "1800"))
        try:
            admin_lc = sorted(int(x) for x in admins)
        except ValueError:
            return
        if not admin_lc:
            return
        primary_chat = admin_lc[0]
        chain_iters = int(os.environ.get("TG_LOGIC_CHAIN_MAX_ITERS", "14") or "14")
        prompt = os.environ.get(
            "TG_LOGIC_CHAIN_PROMPT",
            "【自主逻辑链】定期自检。\n"
            "1) 用仓库内已有日志/脚本检查近期交易或策略结果（无则明确说无数据，勿编造）。\n"
            "2) web_fetch 拉取公开资讯做 Alpha/市场摘要（遵守 ToS，勿绕过登录墙）。\n"
            "3) 若需改策略：在 task_plan.md 写 [现状]→[错误归因]→[学习调研]→[重构方案]→[实验验证]，再改代码。\n"
            "禁止自动 API 代充、未授权推特抓取、违法内容。",
        )
        time.sleep(min(interval, 90))
        while True:
            time.sleep(interval)
            try:
                if not allows_logic_chain(_control_state()):
                    continue
                if worker_busy.is_set():
                    continue
                try:
                    if task_q.unfinished_tasks > 0 or task_q.qsize() > 0:
                        continue
                except Exception:
                    pass

                def _chain_hook(msg: str) -> None:
                    try:
                        _send_chunks(bot, primary_chat, f"[逻辑链]\n{msg}")
                    except Exception:
                        pass

                merged = _merged_system_append(prompt) or system_append
                dev_claw_run(
                    prompt,
                    max_iterations=chain_iters,
                    system_append=merged,
                    progress_hook=_chain_hook,
                )
            except Exception as e:  # noqa: BLE001
                try:
                    _broadcast_admins(f"[逻辑链异常] {e!s}"[:TG_CHUNK])
                except Exception:
                    pass

    threading.Thread(target=autonomous_logic_chain_loop, daemon=True, name="logic-chain").start()

    def autonomous_life_loop() -> None:
        """
        自主心跳：周期性自检；DEGRADED 且队列空闲时主动 DevClaw 清理/归档；交易时间窗内可跑行情摘要。
        与 pyTelegramBotAPI 同步模型一致，使用线程而非 asyncio。
        """
        if not _env_truthy("TG_AUTONOMOUS_LIFE"):
            return
        ws_path = Path(os.environ.get("DEVCLAW_WORKSPACE", _REPO_ROOT)).resolve()
        interval = max(60, int(os.environ.get("AUTONOMOUS_LIFE_TICK_SEC", "600") or "600"))
        try:
            admin_chats = sorted(int(x) for x in admins)
        except ValueError:
            admin_chats = []
        if not admin_chats:
            return
        primary_chat = admin_chats[0]
        auto_iters = int(os.environ.get("TG_AUTONOMOUS_MAX_ITERS", str(min(max_iters, 12))) or "12")

        time.sleep(min(interval, 45))
        while True:
            time.sleep(interval)
            try:
                if not allows_autonomous_life(_control_state()):
                    continue
                if worker_busy.is_set():
                    continue
                try:
                    if task_q.unfinished_tasks > 0 or task_q.qsize() > 0:
                        continue
                except Exception:
                    pass

                eng = SurvivalEngine(ws_path)
                eng.heartbeat()
                st, reason = eng.assess_survival_state()

                def _life_hook(msg: str) -> None:
                    try:
                        _send_chunks(bot, primary_chat, f"[自主心跳]\n{msg}")
                    except Exception:
                        pass

                if st == SurvivalState.DEGRADED:
                    if _env_truthy("TG_AUTONOMOUS_COLD_UPLOAD"):
                        try:
                            from claw_runtime.ultimate.storage_cold import (
                                compress_paths,
                                telegram_send_document_if_configured,
                            )

                            raw_paths = os.environ.get(
                                "TG_AUTONOMOUS_COLD_PATHS",
                                ".claw/sessions",
                            )
                            rels = _expand_workspace_paths_for_zip(ws_path, raw_paths)
                            if not rels:
                                raise OSError("no files matched TG_AUTONOMOUS_COLD_PATHS")
                            z = compress_paths(ws_path, rels)
                            ok, um = telegram_send_document_if_configured(z)
                            if ok:
                                _broadcast_admins(f"[自主心跳] 已冷归档上传 TG: {z.name}")
                        except Exception as ez:  # noqa: BLE001
                            _broadcast_admins(f"[自主心跳] 冷归档跳过: {ez!s}"[:500])

                    prompt = os.environ.get(
                        "TG_AUTONOMOUS_DEGRADED_PROMPT",
                        "系统处于 DEGRADED（资源紧张）。请：1) 清理工作区内过大 .log / 临时文件；"
                        "2) 归档或精简 `.claw/sessions` 下旧 jsonl；3) 必要时 `load_skill ultimate_capabilities` 查看冷存储/Colab 打包；"
                        "不要删除 .env 或密钥。完成后简短汇报。",
                    )
                    merged = _merged_system_append(prompt) or system_append
                    _broadcast_admins(f"[自主心跳] DEGRADED — 启动维护任务\n{reason[:300]}")
                    dev_claw_run(
                        prompt,
                        max_iterations=auto_iters,
                        system_append=merged,
                        progress_hook=_life_hook,
                    )

                elif _is_trading_window_utc():
                    prompt = os.environ.get(
                        "TG_AUTONOMOUS_TRADING_PROMPT",
                        "【自主行情扫视】load_skill trading_dex_pulse；或 execute_terminal 运行 "
                        "`py tools\\\\web_agent.py \"crypto majors brief\"`；"
                        "若有异常波动用 5 行内中文总结（非投资建议）。无 skill smart_money 时勿虚构链上聪明钱数据。",
                    )
                    merged = _merged_system_append(prompt) or system_append
                    _broadcast_admins("[自主心跳] 交易时间窗 — 启动行情摘要任务")
                    dev_claw_run(
                        prompt,
                        max_iterations=auto_iters,
                        system_append=merged,
                        progress_hook=_life_hook,
                    )

            except Exception as e:  # noqa: BLE001
                try:
                    _broadcast_admins(f"[自主心跳异常] {e!s}"[:TG_CHUNK])
                except Exception:
                    pass

    threading.Thread(target=autonomous_life_loop, daemon=True, name="autonomous-life").start()

    _nomad_auto = os.environ.get("NOMAD_AUTO_REGISTER", "1").strip().lower() not in {"0", "false", "no"}
    _nomad_legacy = os.environ.get("NOMAD_REGISTER_HANDLERS", "").strip().lower() in {"1", "true", "yes"}
    if _nomad_auto or _nomad_legacy:
        from claw_runtime.ultimate.nomad import register_nomad_handlers

        register_nomad_handlers(Path(os.environ.get("DEVCLAW_WORKSPACE", _REPO_ROOT)))

    # ---- 命令处理器（勿用纯 content_types=text 抢 /start）----

    @bot.message_handler(commands=["whoami"])
    def cmd_whoami(message: telebot.types.Message) -> None:
        uid = message.from_user.id if message.from_user else "?"
        un = message.from_user.username if message.from_user else None
        bot.reply_to(
            message,
            "本对话信息（用于配置 .env）\n"
            f"chat_id: {message.chat.id}\n"
            f"user_id: {uid}\n"
            f"username: @{un if un else 'n/a'}\n\n"
            "私聊机器人时，一般把 chat_id 写入 TG_ADMIN_CHAT_IDS。",
        )

    @bot.message_handler(commands=["ping"])
    def cmd_ping(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(
                message,
                f"无权限。你的 chat_id={message.chat.id}，请加入 TG_ADMIN_CHAT_IDS 后重试。",
            )
            return
        bot.reply_to(message, "pong — DevClaw 网关在线。")

    @bot.message_handler(commands=["status"])
    def cmd_status(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(
                message,
                f"无权限。你的 chat_id={message.chat.id}，写入 TG_ADMIN_CHAT_IDS 即可。",
            )
            return
        try:
            qn = task_q.qsize()
        except Exception:
            qn = -1
        control = _control_state()
        bot.reply_to(
            message,
            "DevClaw 状态\n"
            f"工作区: {os.environ.get('DEVCLAW_WORKSPACE')}\n"
            f"队列中任务数: {qn}\n"
            f"最大迭代: {max_iters}\n"
            f"忙碌中: {'是' if worker_busy.is_set() else '否'}\n\n"
            + panel_summary(control)
            + "\n\n说明: 单 worker 串行处理，上一条跑完才处理下一条。",
        )

    @bot.message_handler(commands=["panel"])
    def cmd_panel(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        bot.reply_to(message, _control_panel_text())

    @bot.message_handler(commands=["pause"])
    def cmd_pause(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        state = update_runtime_control(
            ws_path,
            actor=f"tg:{message.chat.id}",
            source_text="/pause",
            note="Paused intake and background autonomy from Telegram command.",
            accepting_tasks=False,
            background_master_enabled=False,
        )
        bot.reply_to(message, "已暂停。\n\n" + panel_summary(state))

    @bot.message_handler(commands=["resume"])
    def cmd_resume(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        state = update_runtime_control(
            ws_path,
            actor=f"tg:{message.chat.id}",
            source_text="/resume",
            note="Resumed intake and background autonomy from Telegram command.",
            accepting_tasks=True,
            background_master_enabled=True,
        )
        bot.reply_to(message, "已恢复开工。\n\n" + panel_summary(state))

    @bot.message_handler(commands=["sim"])
    def cmd_sim(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        state = update_runtime_control(
            ws_path,
            actor=f"tg:{message.chat.id}",
            source_text="/sim",
            note="Trading hard-limited to simulation only from Telegram command.",
            trading_mode="simulation",
            live_trading_enabled=False,
            manual_trading_approval_required=True,
        )
        bot.reply_to(message, "已锁定为 simulation only。\n\n" + panel_summary(state))

    @bot.message_handler(commands=["vitals"])
    def cmd_vitals(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        eng = SurvivalEngine(ws_path)
        eng.heartbeat()
        snap = eng.snapshot()
        body = json.dumps(snap, ensure_ascii=False, indent=2)[:3500]
        bot.reply_to(message, "Survival snapshot:\n```\n" + body + "\n```", parse_mode=None)

    @bot.message_handler(commands=["parasite_off"])
    def cmd_parasite_off(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        SurvivalEngine(ws_path).set_parasite_mode(False)
        bot.reply_to(message, "已关闭寄生模式标记（.claw/parasite_mode.json 已清除）。可 unset DEVCLAW_PARASITE_MODE。")

    @bot.message_handler(commands=["evolve"])
    def cmd_evolve(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        from claw_runtime.nightly_evolution import materialize_draft_skill

        out = materialize_draft_skill(ws_path)
        if out:
            bot.reply_to(message, f"已生成草稿技能:\n`{out}`\n请人工审阅 SKILL.md。", parse_mode=None)
        else:
            bot.reply_to(message, "暂无 .claw/evolution_failures.jsonl 记录，未生成草稿。")

    @bot.message_handler(commands=["cancel"])
    def cmd_cancel(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        bot.reply_to(
            message,
            "当前版本为单线程 worker，无法在运行中安全「取消」OpenAI 循环。\n"
            "如需停止：在本机结束 tg_dev_claw.py 进程（Ctrl+C）。",
        )

    @bot.message_handler(commands=["start", "help"])
    def cmd_help(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(
                message,
                f"无权限：你的 chat_id={message.chat.id}\n"
                "请把该数字写入服务器 .env 的 TG_ADMIN_CHAT_IDS（逗号分隔多个）。\n"
                "仍可用 /whoami 查看本消息中的 id。",
            )
            return
        bot.reply_to(
            message,
            "DevClaw TG 网关\n\n"
            "命令：\n"
            "/start /help — 本说明\n"
            "/whoami — 查看 chat_id\n"
            "/ping — 在线检测\n"
            "/status — 队列与工作区\n"
            "/panel — 控制面板\n"
            "/pause — 暂停接任务和自主循环\n"
            "/resume — 恢复接任务和自主循环\n"
            "/sim — 锁定 simulation only\n"
            "/cancel — 取消说明\n"
            "/vitals — 生存引擎快照\n"
            "/parasite_off — 关闭寄生模式\n"
            "/evolve — 失败日志 → 草稿 SKILL\n\n"
            f"工作区: {os.environ.get('DEVCLAW_WORKSPACE')}\n"
            f"最大迭代: {max_iters}\n"
            f"自主心跳: {'开 (TG_AUTONOMOUS_LIFE=1)' if _env_truthy('TG_AUTONOMOUS_LIFE') else '关'}\n"
            f"空闲自检: {'开 (TG_IDLE_AUTOTICK=1)' if _env_truthy('TG_IDLE_AUTOTICK') else '关'}\n"
            f"逻辑链: {'开 (TG_AUTONOMOUS_LOGIC_CHAIN=1)' if _env_truthy('TG_AUTONOMOUS_LOGIC_CHAIN') else '关'}\n\n"
            '自然语言也能控：例如"开始干活，检查一下仓库""暂停""只模拟交易""控制面板"。\n\n'
            + _control_panel_text(),
        )

    @bot.message_handler(
        content_types=["text"],
        func=lambda m: m.text is not None and not _text_looks_like_command(m.text),
    )
    def on_plain_text(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(
                message,
                f"无权限。chat_id={message.chat.id} → 写入 TG_ADMIN_CHAT_IDS。",
            )
            return
        _handle_text_input(
            message.chat.id,
            message.text or "",
            actor=f"tg:{message.chat.id}",
            channel="tg",
        )

    @bot.message_handler(content_types=["text"], func=lambda m: m.text is not None and _text_looks_like_command(m.text))
    def on_unknown_command(message: telebot.types.Message) -> None:
        """未实现的 /xxx 给提示，避免静默无回。"""
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        bot.reply_to(message, "未知命令。发送 /help 查看列表。")

    print("TG DevClaw 监听中… 工作区:", os.environ.get("DEVCLAW_WORKSPACE"))
    print("管理员 chat id:", ", ".join(sorted(admins)))
    # Don't spam user with startup messages - only log locally
    # _broadcast_admins("DevClaw 已启动。\n\n" + _control_panel_text())
    bot.infinity_polling(skip_pending=True, interval=1, timeout=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
