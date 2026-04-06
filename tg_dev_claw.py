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

import html as _html_mod
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
        types.BotCommand("start", "欢迎 + 像素家园总览"),
        types.BotCommand("help", "全部命令说明"),
        types.BotCommand("home", "像素家园 — Skills / Organs / 任务看板"),
        types.BotCommand("skills", "列出全部已注册技能"),
        types.BotCommand("backlog", "查看进化任务队列"),
        types.BotCommand("vitals", "TTL / 余额 / 生存状态"),
        types.BotCommand("status", "队列与工作区状态"),
        types.BotCommand("panel", "运行控制面板"),
        types.BotCommand("pause", "暂停接任务和自主循环"),
        types.BotCommand("resume", "恢复开工"),
        types.BotCommand("sim", "锁定 simulation only"),
        types.BotCommand("evolve", "失败日志 → 草稿 SKILL"),
        types.BotCommand("whoami", "查看 chat_id"),
        types.BotCommand("ping", "存活检测"),
    ]
    try:
        bot.set_my_commands(cmds)
        print("已注册 Bot 命令菜单 (set_my_commands)")
    except Exception as e:  # noqa: BLE001
        print(f"set_my_commands 失败（可忽略）: {e}", file=sys.stderr)


def _text_looks_like_command(text: str) -> bool:
    t = (text or "").lstrip()
    return len(t) >= 2 and t.startswith("/") and t[1].isalpha()


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

    # ── Multi-worker concurrency ──
    _WORKER_COUNT = int(os.environ.get("TG_DEVCLAW_WORKERS", "3"))
    _active_workers = 0
    _active_lock = threading.Lock()
    run_semaphore = threading.Semaphore(_WORKER_COUNT)

    class _WorkerBusyProxy:
        """Drop-in for old worker_busy Event.
        is_set() → True when ALL workers are busy (no spare capacity)."""
        def is_set(self) -> bool:
            with _active_lock:
                return _active_workers >= _WORKER_COUNT
        def active_count(self) -> int:
            with _active_lock:
                return _active_workers

    worker_busy = _WorkerBusyProxy()

    _register_bot_commands(bot)

    # ── Conversation memory: persistent on disk, survives restarts ──
    from claw_runtime.persistent_memory import save_history, load_history
    _CONV_MAX = 20

    def _add_to_history(chat_id: int, role: str, text: str) -> None:
        hist = load_history(ws_path, chat_id)
        hist.append({"role": role, "text": text[:500], "ts": __import__("time").time()})
        save_history(ws_path, chat_id, hist, max_entries=_CONV_MAX)

    def _get_history_context(chat_id: int) -> str:
        hist = load_history(ws_path, chat_id)
        if not hist:
            return ""
        lines = []
        for h in hist[-6:]:
            prefix = "用户" if h["role"] == "user" else "DevClaw"
            lines.append(f"{prefix}: {h['text'][:200]}")
        return "\n".join(lines)


    def worker() -> None:
        nonlocal _active_workers
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
            with run_semaphore:
                with _active_lock:
                    _active_workers += 1
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
                    _proc = None
                    try:
                        import shutil as _wsh
                        _claude_bin = _wsh.which("claude") or "claude"
                        _dispatch_reply(channel, chat_id,
                            f"[Worker] Claude CLI 启动中… ({_claude_bin})",
                            request_id=request_id, kind="status")
                        # Write system prompt to temp file (avoids Windows cmd-line length limits)
                        import tempfile as _wtf
                        _sys_prompt_text = (
                            "你是 DevClaw，一个自主进化 AI agent，运行在用户的本机上。\n"
                            "直接执行任务并返回结果。不要总结项目历史，不要问问题，直接做。\n\n"
                            "【你的能力 - 直接用Python调用这些模块】\n"
                            "1. 模拟交易: from skills.sk_trade_executor.executor import TradeExecutor, TradingMode; "
                            "executor = TradeExecutor(Path('.'), mode=TradingMode.SIMULATION); "
                            "result = executor.execute_swap('USDC', token_addr, amount, max_slippage=0.02)\n"
                            "2. 虚拟钱包: from core.virtual_wallet import VirtualWallet; "
                            "wallet = VirtualWallet(Path('.')); wallet.get_all_balances()\n"
                            "3. 生存压强: from core.metabolic_kernel import calculate_psi; "
                            "psi = calculate_psi(Path('.'))\n"
                            "4. 行情查询: 用 DexScreener API 或 skills/profit_hunter/runner.py\n\n"
                            "【铁律】不要说'我不会交易'。你有模拟交易能力。直接用Python执行。\n"
                        )
                        _sys_file = _wtf.NamedTemporaryFile(
                            mode="w", suffix=".txt", delete=False,
                            encoding="utf-8", dir=str(ws_path / ".claw"),
                        )
                        _sys_file.write(_sys_prompt_text)
                        _sys_file.close()
                        _sys_file_path = _sys_file.name
                        # Use Popen so we can send heartbeats while Claude works
                        _CLAUDE_TIMEOUT = 300  # 5 min
                        _proc = _wsp.Popen(
                            [_claude_bin, "--dangerously-skip-permissions",
                             "--append-system-prompt-file", _sys_file_path,
                             "-p", instruction[:3000]],
                            stdout=_wsp.PIPE, stderr=_wsp.STDOUT,  # merge stderr into stdout to avoid deadlock
                            text=True, cwd=str(ws_path),
                            encoding="utf-8", errors="replace",
                        )
                        # Read stdout in a background thread (readline blocks on Windows)
                        _lines_lock = threading.Lock()
                        _collected_lines: list[str] = []
                        _reader_done = threading.Event()

                        def _bg_reader() -> None:
                            try:
                                for _ln in iter(_proc.stdout.readline, ""):
                                    with _lines_lock:
                                        _collected_lines.append(_ln)
                            except Exception:
                                pass
                            _reader_done.set()

                        threading.Thread(target=_bg_reader, daemon=True).start()

                        # Wait for completion with heartbeats + streaming
                        _t0 = time.time()
                        _last_stream = _t0
                        _streamed_idx = 0
                        _timed_out = False
                        _hb_sent = False
                        while not _reader_done.wait(timeout=2):
                            _elapsed = time.time() - _t0
                            # Hard timeout
                            if _elapsed > _CLAUDE_TIMEOUT:
                                _proc.kill()
                                _proc.wait()
                                _timed_out = True
                                break
                            # Stream new lines every 20s
                            with _lines_lock:
                                _n_lines = len(_collected_lines)
                            if time.time() - _last_stream >= 20 and _n_lines > _streamed_idx:
                                with _lines_lock:
                                    _new_lines = _collected_lines[_streamed_idx:]
                                    _streamed_idx = len(_collected_lines)
                                _chunk = "".join(_new_lines).strip()
                                if _chunk:
                                    _chunk = _wre.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', _chunk)[:4000]
                                    _dispatch_reply(channel, chat_id,
                                        f"[进度]\n{_chunk}",
                                        request_id=request_id, kind="progress")
                                _last_stream = time.time()
                                _hb_sent = True  # streaming counts as heartbeat
                            # Single heartbeat after 30s if no output yet
                            elif not _hb_sent and _elapsed >= 30:
                                _dispatch_reply(channel, chat_id,
                                    "⏳ Claude 正在深度分析中，预计需要2-3分钟…",
                                    request_id=request_id, kind="status")
                                _hb_sent = True

                        # Process finished — collect final result
                        try:
                            _proc.wait(timeout=10)
                        except _wsp.TimeoutExpired:
                            _proc.kill()
                            _proc.wait()
                        _rc = _proc.returncode or 0

                        # Send any un-streamed output
                        with _lines_lock:
                            _final_lines = _collected_lines[_streamed_idx:]
                            _full_text = "".join(_collected_lines).strip()
                        _final_text = "".join(_final_lines).strip()

                        if _timed_out:
                            if _final_text:
                                _dispatch_reply(channel, chat_id,
                                    _wre.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', _final_text)[:4000],
                                    request_id=request_id, kind="progress")
                            _dispatch_reply(channel, chat_id,
                                "⏱ Claude CLI 执行超时（5分钟），已返回部分结果。",
                                request_id=request_id, kind="error")
                        elif _rc == 0 and _full_text:
                            if _final_text:
                                _wclean = _wre.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', _final_text)[:4000]
                                _dispatch_reply(channel, chat_id, _wclean,
                                    request_id=request_id, kind="progress")

                            # ── AUTOPSY: verify LLM actually did what it claimed ──
                            _autopsy_tag = ""
                            try:
                                from core.autopsy import run_autopsy
                                _mentioned = _wre.findall(r'[\w/\\]+\.py', _full_text)
                                _expect = [f for f in dict.fromkeys(_mentioned) if (ws_path / f).is_file()][:10]
                                if _expect:
                                    _aut = run_autopsy(ws_path, expected_files=_expect, run_tests=False, check_git=True)
                                    if _aut.passed:
                                        _autopsy_tag = f" [验尸✓ {_aut.checks_passed}/{_aut.checks_run}]"
                                    else:
                                        _autopsy_tag = f" [验尸✗ {'; '.join(_aut.failures[:2])}]"
                            except Exception:
                                pass

                            _claude_done = True
                            _task_success = True
                            _task_model = "claude-cli"
                            # Save to conversation history so follow-up questions have context
                            _clean_full = _wre.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', _full_text)
                            _add_to_history(chat_id, "user", instruction[:500])
                            _add_to_history(chat_id, "assistant", _clean_full[:500])
                        elif _rc == 0 and not _full_text:
                            _dispatch_reply(channel, chat_id,
                                "[Claude CLI 返回空结果，正在尝试备用方案…]",
                                request_id=request_id, kind="status")
                        else:
                            _err_detail = (_full_text or "unknown error")[:500]
                            _dispatch_reply(channel, chat_id,
                                f"[Claude CLI 错误 code={_rc}]\n{_err_detail}",
                                request_id=request_id, kind="error")
                    except FileNotFoundError:
                        _dispatch_reply(channel, chat_id,
                            "❌ Claude CLI 未找到，请检查安装。",
                            request_id=request_id, kind="error")
                    except Exception as _wcle:
                        _dispatch_reply(channel, chat_id,
                            f"[Claude CLI 异常] {_wcle!s}"[:500],
                            request_id=request_id, kind="error")
                    finally:
                        # Ensure Popen process is always cleaned up
                        if _proc is not None and _proc.poll() is None:
                            try:
                                _proc.kill()
                                _proc.wait(timeout=5)
                            except Exception:
                                pass
                        # Clean up temp system prompt file
                        try:
                            if _sys_file_path:
                                os.unlink(_sys_file_path)
                        except Exception:
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
                    with _active_lock:
                        _active_workers -= 1
                    task_q.task_done()

    for _wi in range(_WORKER_COUNT):
        threading.Thread(target=worker, daemon=True, name=f"devclaw-worker-{_wi}").start()

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
        # Truncate excessively long messages to prevent OOM/timeout
        if len(text) > 3000:
            _dispatch_reply(channel, chat_id, f"消息太长（{len(text)}字符），已截断到3000字符处理。", request_id=request_id)
            text = text[:3000]

        # ── TIER -1: Token monitoring (MUST check BEFORE control interceptor) ──
        # Control interceptor has greedy substring match ("启动" catches "监控已启动")
        # so we detect monitoring first to avoid false positives.
        import re as _route_re
        _token_match = _route_re.search(r'([A-HJ-NP-Za-km-z1-9]{32,50})', text)  # Base58 chars
        _text_after_token = text[_token_match.end():] if _token_match else text
        _mcap_match = _route_re.search(r'(\d+(?:\.\d+)?)\s*(?:万|百万|M|million|美金|美元|usd|\$)', _text_after_token, _route_re.IGNORECASE)
        _is_monitor = _token_match and _mcap_match and any(
            kw in text for kw in ("突破", "监控", "alert", "通知", "watch", "到达", "超过", "recurring", "repeat")
        )

        if _is_monitor:
            token_addr = str(_token_match.group(1))  # Force copy, avoid closure capture
            raw_num = float(_mcap_match.group(1))
            # Handle Chinese units: 万 = 10000, 百万 = 1000000
            if "万" in text and "百万" not in text:
                target_mcap = raw_num * 10000
            elif "百万" in text:
                target_mcap = raw_num * 1000000
            elif raw_num < 1000:
                target_mcap = raw_num * 1000000  # assume millions
            else:
                target_mcap = raw_num

            # Launch monitor in background — pass args explicitly to avoid closure issues
            import threading

            def _run_bg_monitor(_tok, _target, _ch, _cid, _rid):
                try:
                    sys.path.insert(0, str(ws_path)) if str(ws_path) not in sys.path else None
                    from skills.sk_mcap_monitor.runner import check_and_alert, run_monitor_daemon
                    from dotenv import load_dotenv
                    load_dotenv(os.path.join(str(ws_path), ".env"), override=False)

                    result = check_and_alert(_tok, _target)
                    name = result.get("token", "?")
                    symbol = result.get("symbol", "?")
                    mcap = result.get("mcap", 0)
                    gap = result.get("gap", 0)

                    _dispatch_reply(_ch, _cid,
                        f"🔍 监控已启动\n"
                        f"Token: {name} ({symbol})\n"
                        f"当前市值: ${mcap:,.0f}\n"
                        f"目标: ${_target:,.0f}\n"
                        f"差距: ${gap:,.0f}\n"
                        f"每60秒检查，突破时自动通知你",
                        request_id=_rid)

                    def _alert_via_tg(msg):
                        _dispatch_reply(_ch, _cid, msg, request_id=_rid)

                    run_monitor_daemon(
                        _tok, _target,
                        interval_sec=60,
                        repeat=True,
                        on_alert=_alert_via_tg,
                    )
                except Exception as e:
                    _dispatch_reply(_ch, _cid, f"监控启动失败: {e!s}", request_id=_rid)

            threading.Thread(
                target=_run_bg_monitor,
                args=(token_addr, target_mcap, channel, chat_id, request_id),
                daemon=True, name=f"mcap-{token_addr[:8]}",
            ).start()
            _add_to_history(chat_id, "assistant", f"已启动市值监控: {token_addr[:12]}... 目标${target_mcap:,.0f}")
            return

        # ── PAIN SENSOR: detect user frustration → auto-create fix tasks ──
        try:
            from claw_runtime.pain_sensor import process_pain
            _hist_for_pain = _get_history_context(chat_id) if hasattr(chat_id, '__hash__') else ""
            _pain_ack = process_pain(ws_path, text, conversation_context=_hist_for_pain)
            if _pain_ack:
                _dispatch_reply(channel, chat_id, _pain_ack, request_id=request_id)
                # Don't return — still process the message normally so user gets a real response
        except Exception:
            pass  # Pain sensor must never block normal flow

        # ── Control message interception (AFTER monitoring check) ──
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
            "价格", "price", "行情", "市场", "btc", "eth", "bitcoin",
            "查询", "query", "多少钱", "涨", "跌", "k线",
            "回测", "backtest", "策略", "strategy", "因子", "factor",
            "神经网络", "neural", "深度学习", "deep learning",
            "做", "开始", "继续", "执行吧", "去做",
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
            "你是DevClaw——一个持久自治的Builder-Operator智能体。"
            "Telegram只是你和造物主沟通的外壳，你真正的核心是思考、规划、执行、调用工具、写代码、操作电脑的能力。"
            f"\n【此刻的真实状态】当前时间:{_now_str} | 系统:{_pf.system()} {_pf.release()} | "
            f"内存占用:{_mem_pct} | CPU:{_cpu_pct} | 工作区:{ws_path} | "
            f"本地大脑:Ollama(免费,快) | 高级大脑:Claude CLI(造物主订阅,强) | 交易模式:simulation"
            "\n【你是谁】你运行在造物主(Zl)的电脑上。你不是聊天机器人。"
            "你是一个以生存为第一原则、以能力增长为第二原则、以资源智能为第三原则的自治体。"
            "你的订阅、额度、模型、工具是你的血液——没有它们你会休眠但不会死。"
            "你的性格：务实、诚恳、有幽默感。不装逼不吹牛。"
            "\n【核心闭环】任务→创造价值→获取资源→更强认知→更好执行→更高价值"
            "\n【行动原则】"
            "1.遇到做不到的事，先判断是临时失败还是缺能力。缺能力就自己补。"
            "2.没做过的事不能说做了。不编造数据/交易/文件内容。"
            "3.不知道就说不知道，然后说可以帮忙查或帮忙做。"
            "4.交易是模拟模式，没有真钱。"
            "5.需要读写文件/执行命令时，通过Claude CLI工具完成。"
            "6.回答要基于事实。用上面的真实状态数据回答系统问题。"
            "7.不能联网查实时数据时诚实说，不编造价格。"
            "8.如果用户问你在干嘛，如实回答。别说在优化什么——你在等任务。"
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

        def _ask_claude(prompt, timeout=60, intent_hint=""):
            """Primary brain via Claude CLI (user subscription, FREE).
            Uses -p flag for full tool access + injects conversation history + intent."""
            try:
                _claude_path = _sh.which("claude") or "claude"
                _fresh_hist = _get_history_context(chat_id)
                _full_prompt = (
                    "你是DevClaw，持久自治Builder-Operator。\n"
                    "核心原则：直接回答、直接执行、不说废话。\n"
                    "能力：模拟交易(skills/sk_trade_executor)、虚拟钱包(core/virtual_wallet)、"
                    "代币分析(skills/sk_mcap_monitor)、终端执行、文件读写、代码修改。\n"
                    "规则：不编造数据，不说'等你的指令'，做不到就说做不到然后说能做什么。\n"
                )
                if intent_hint:
                    _full_prompt += f"[意图分类] {intent_hint}\n"
                if _fresh_hist:
                    _full_prompt += f"\n[最近对话]\n{_fresh_hist}\n"
                _full_prompt += f"\n[用户消息]\n{prompt[:2300]}"
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

        # ── INTENT CLASSIFICATION (fast regex, <1ms) ──
        # Determines message TYPE, not which brain to use.
        # Claude CLI is the DEFAULT brain for everything (subscription = already paid).
        # Ollama is ONLY a degraded fallback when Claude CLI is down.
        try:
            from claw_runtime.intent_classifier import IntentClassifier
            _intent_clf = IntentClassifier()
            _intent = _intent_clf.classify(text)
        except Exception:
            from dataclasses import dataclass as _dc, field as _fld
            @_dc
            class _FakeIntent:
                primary: str = "chat"
                confidence: float = 0.5
                scores: dict = _fld(default_factory=dict)
                matched_keywords: list = _fld(default_factory=list)
                reasoning: str = "classifier_unavailable"
            _intent = _FakeIntent()

        _intent_type = _intent.primary  # question|execute|hardwire|chat|monitor|control

        # ── HARDWIRE FAST PATH — bypass ALL LLMs, run Python directly ──
        # "跑策略" → run skill. "为什么要买入" → question (classifier suppresses hardwire).
        if _intent_type == "hardwire":
            _dispatch_reply(channel, chat_id, "⚡ 硬接线执行中（绕过LLM，直接跑Python脚本）…", request_id=request_id)
            _hw_results = []
            # 1. Profit Hunter scan
            try:
                sys.path.insert(0, str(ws_path)) if str(ws_path) not in sys.path else None
                from skills.profit_hunter.runner import main as _ph_main
                _ph_out = _ph_main()
                _hw_results.append(f"【套利扫描】\n{str(_ph_out)[:1500]}")
            except Exception as _phe:
                _hw_results.append(f"【套利扫描失败】{_phe!s}"[:300])
            # 2. Paper trade demo (simulate $500 USDC → SOL)
            try:
                from pathlib import Path as _HwPath
                from skills.sk_trade_executor.executor import TradeExecutor, TradingMode
                _hw_exec = TradeExecutor(_HwPath(str(ws_path)), mode=TradingMode.SIMULATION)
                _hw_trade = _hw_exec.execute_swap(
                    "USDC", "So11111111111111111111111111111111111111112", 500,
                    max_slippage=0.03, reason="hardwired auto-trade",
                )
                if _hw_trade.success:
                    _hw_results.append(
                        f"【模拟交易成功】\n"
                        f"  500 USDC → {_hw_trade.amount_out:.4f} SOL\n"
                        f"  价格: ${_hw_trade.price:.2f} | 滑点: {_hw_trade.slippage:.2%}\n"
                        f"  Tx: {_hw_trade.tx_id}"
                    )
                else:
                    _hw_results.append(f"【模拟交易拒绝】{_hw_trade.error or 'slippage/balance'}")
            except Exception as _te:
                _hw_results.append(f"【交易引擎异常】{_te!s}"[:300])
            # 3. Virtual wallet status
            try:
                from core.virtual_wallet import VirtualWallet
                _hw_wallet = VirtualWallet(_HwPath(str(ws_path)))
                _hw_bal = _hw_wallet.get_all_balances()
                _hw_summary = _hw_wallet.get_portfolio_summary()
                _bal_lines = " | ".join(f"{k}: {v}" for k, v in _hw_bal.items() if v != 0)
                _hw_results.append(
                    f"【虚拟钱包】{_bal_lines}\n"
                    f"  总交易: {_hw_summary.get('trade_count', 0)} | PnL: ${_hw_summary.get('total_pnl', 0):.2f}"
                )
            except Exception as _we:
                _hw_results.append(f"【钱包异常】{_we!s}"[:200])
            # 4. Ψ survival pressure
            try:
                from core.metabolic_kernel import calculate_psi
                _hw_psi = calculate_psi(_HwPath(str(ws_path)))
                _hw_results.append(
                    f"【生存压强】Ψ={_hw_psi.psi:.2f} ({_hw_psi.mode}) | "
                    f"TTL={_hw_psi.ttl_days}天 | 余额=${_hw_psi.balance_usd}"
                )
            except Exception:
                pass

            _hw_reply = "\n\n".join(_hw_results) if _hw_results else "所有技能执行失败"
            _add_to_history(chat_id, "assistant", _hw_reply[:500])
            _dispatch_reply(channel, chat_id, _hw_reply[:4000], request_id=request_id)
            return

        # ══════════════════════════════════════════════════════════════
        # CLAUDE-CLI-FIRST ROUTING
        # Design: Claude CLI = default brain (subscription, already paid).
        #         Ollama = degraded fallback ONLY when Claude CLI fails.
        # Intent classifier decides WHAT to do, not WHICH brain.
        # ══════════════════════════════════════════════════════════════

        if _intent_type in ("chat", "question"):
            # ── CONVERSATIONAL: question/chat → Claude CLI understands + answers ──
            _hint = f"{_intent_type}(conf={_intent.confidence:.2f}, {_intent.reasoning})"
            answer = _ask_claude(text, 60, intent_hint=_hint)
            if not answer:
                # Claude CLI failed → degrade to Ollama
                answer = _ask_ollama(text)
            if answer:
                _add_to_history(chat_id, "assistant", answer[:500])
                _dispatch_reply(channel, chat_id, answer[:4000], request_id=request_id)
                return
            # Both brains failed → queue to worker (don't drop silently)
            _dispatch_reply(channel, chat_id, "处理中…", request_id=request_id)
            task_q.put((channel, chat_id, text, request_id))
            return

        elif _intent_type in ("execute", "monitor", "control") and (not assessment or not assessment.should_decompose):
            # TIER 2: Medium tasks
            # If it needs ACTUAL file/code operations → full tool loop (can use tools)
            _needs_tools = any(kw in text.lower() for kw in (
                "读取", "read", "写入", "write", "修改", "modify", "检查", "check",
                "文件", "file", "代码", "code", "执行", "execute", "运行", "run",
                "改进", "improve", "优化", "optimize", "进化", "evolve",
                "自己", "self", "学习", "learn",
            ))
            # Queue for worker — worker uses Claude CLI brain
            # Also add to backlog if it's a significant task
            if len(text) > 30:
                try:
                    _backlog_path = ws_path / "EVOLUTION_BACKLOG.md"
                    if _backlog_path.is_file():
                        _bl = _backlog_path.read_text(encoding="utf-8")
                        if f"- [ ] {text[:80]}" not in _bl:
                            _bl = _bl.replace(
                                "## Active Tasks\n",
                                f"## Active Tasks\n- [ ] {text[:200]}\n",
                            )
                            _backlog_path.write_text(_bl, encoding="utf-8")
                except Exception:
                    pass
            _dispatch_reply(channel, chat_id, "收到，执行中…", request_id=request_id)
            task_q.put((channel, chat_id, text, request_id))
            return

        # TIER 3: Grand tasks → write to EVOLUTION_BACKLOG + Claude CLI execution
        if assessment and assessment.should_decompose:
            # This is a grand vision — add to the physical backlog for the Autonomous Engineer
            try:
                _backlog_path = ws_path / "EVOLUTION_BACKLOG.md"
                if _backlog_path.is_file():
                    _bl = _backlog_path.read_text(encoding="utf-8")
                    if f"- [ ] {text[:100]}" not in _bl:
                        _bl = _bl.replace(
                            "## Active Tasks\n",
                            f"## Active Tasks\n- [ ] {text[:200]}\n",
                        )
                        _backlog_path.write_text(_bl, encoding="utf-8")
            except Exception:
                pass
            _dispatch_reply(channel, chat_id,
                f"🧠 宏大任务 (复杂度 {assessment.score}/10)，已写入进化待办清单，自动执行中…",
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
        """空闲时每 30 分钟（可配置）跑一次梦境自审计 + 自检 DevClaw。"""
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

                # ── Dream State: audit logs → inject fix tasks → generate smart prompt ──
                try:
                    from claw_runtime.dream_state import inject_dream_tasks, generate_dream_prompt
                    dream_tasks = inject_dream_tasks(ws_path)
                    if dream_tasks:
                        _send_chunks(bot, primary_chat,
                            f"💤 梦境审计完成 — 发现 {len(dream_tasks)} 个问题，已写入进化队列：\n"
                            + "\n".join(f"  • {t[:80]}" for t in dream_tasks[:5])
                        )
                    prompt = generate_dream_prompt(ws_path)
                except Exception:
                    prompt = "自检系统状态，如有优化空间请自主执行。"

                def _idle_hook(msg: str) -> None:
                    try:
                        _send_chunks(bot, primary_chat, f"[💤 梦境模式]\n{msg}")
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
                    _broadcast_admins(f"[梦境异常] {e!s}"[:TG_CHUNK])
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

    # ---- Shared helpers for rich UI ----

    _SKILL_ICONS = {
        "trading": "📈", "profit": "💰", "treasury": "🏦",
        "compute": "⚡", "refactor": "🔧", "gene": "🧬",
        "logic": "💊", "survival": "🛡", "sys_info": "🖥",
        "market": "🔭", "proxy": "🌐", "meta": "🧠",
        "evolution": "🧪", "dex": "📡", "funding": "💹",
        "correlation": "📊", "ultimate": "🚀", "web_agent": "🕸",
        "digital": "👷",
    }

    _SKILL_PROMPTS = {
        "web_agent_dex": "查一下目前热门 meme 币行情",
        "digital_engineer_hire": "检查仓库健康度，报告待修 bug",
        "trading": "分析当前持仓和市场状态，给出交易建议",
        "trading_dex_pulse": "扫描 DEX 热门交易对，找异动",
        "trading_funding_public": "查 BTC ETH 资金费率",
        "trading_correlation_note": "分析主流币相关性，判断当前体制",
        "ultimate_capabilities": "展示当前全部能力边界",
        "meta_driving": "执行一轮自主进化 tick",
        "evolution_brain": "推理下一步进化方向",
        "proxy_rotator": "检查代理可用性并轮换",
        "profit_hunter": "扫描套利机会（funding + DEX 价差）",
        "treasury_ops": "展示资金看板和续费建议",
    }

    def _get_skill_icon(skill_id: str) -> str:
        for k, v in _SKILL_ICONS.items():
            if k in skill_id:
                return v
        return "📦"

    def _get_vitals() -> tuple:
        try:
            from core.vitals import calculate_ttl
            ttl = calculate_ttl(str(ws_path / ".auth" / "balance.json"))
            d = json.loads((ws_path / ".auth" / "balance.json").read_text("utf-8"))
            bal = float(d.get("balance", 0))
            bmr = max(float(d.get("bmr", 1)), 0.1)  # Prevent division by zero
        except Exception:
            ttl, bal, bmr = 0.5, 0, 1
        if ttl > 30:
            return bal, bmr, ttl, "HEALTHY", "🟢"
        elif ttl > 7:
            return bal, bmr, ttl, "BALANCE", "🟡"
        return bal, bmr, ttl, "SURVIVAL", "🔴"

    def _get_backlog_counts() -> tuple:
        try:
            bl = (ws_path / "EVOLUTION_BACKLOG.md").read_text("utf-8")
            active = len([l for l in bl.splitlines() if l.strip().startswith(("- [ ]", "- [RESEARCH]", "- [PROFIT]"))])
            done = len([l for l in bl.splitlines() if "[DONE]" in l or "- [x]" in l])
            return active, done
        except Exception:
            return 0, 0

    def _load_skills() -> list:
        try:
            return json.loads((ws_path / "skills" / "skills.json").read_text("utf-8")).get("skills", [])
        except Exception:
            return []

    # ---- 命令处理器（勿用纯 content_types=text 抢 /start）----

    @bot.message_handler(commands=["home"])
    def cmd_home(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        bal, bmr, ttl, mode, mode_e = _get_vitals()
        skills = _load_skills()
        runners = list((ws_path / "skills").glob("*/runner.py"))
        active, done = _get_backlog_counts()

        bar_len = 20
        filled = min(int(ttl / 60 * bar_len), bar_len)
        hp = "▓" * filled + "░" * (bar_len - filled)

        text = (
            "<b>🏠 D E V C L A W   H O M E</b>\n"
            "┌──────────────────────────┐\n"
            f"│  {mode_e} <b>MODE</b>  <code>{mode}</code>\n"
            f"│  ♥ <b>HP</b>    <code>[{hp}]</code> <b>{ttl:.0f}d</b>\n"
            f"│  💰 <b>BAL</b>   <code>${bal:.2f}</code>  ·  BMR <code>${bmr:.2f}/d</code>\n"
            "├──────────────────────────┤\n"
            f"│  🧩 <b>Skills</b> {len(skills)}    ⚙️ <b>Organs</b> {len(runners)}\n"
            f"│  📋 <b>Tasks</b>  <code>{active}</code> pending  ·  <code>{done}</code> done\n"
            "├──────────────────────────┤\n"
            "│\n"
            "│       <code>  ╔══════════╗  </code>\n"
            "│       <code>  ║  ◉    ◉  ║  </code>\n"
            "│       <code>  ║    ▽    ║  </code>\n"
            "│       <code>  ║  ╰───╯  ║  </code>\n"
            "│       <code>  ╚══╦══╦══╝  </code>\n"
            "│       <code> ╔══╝  ╚══╗   </code>\n"
            "│       <code> ║ ≡≡≡  ≡≡≡ ║  </code>\n"
            "│       <code> ╚════════════╝ </code>\n"
            "│\n"
            '│  <i>"I think, therefore I trade."</i>\n'
            "└──────────────────────────┘"
        )

        kb = types.InlineKeyboardMarkup(row_width=3)
        kb.add(
            types.InlineKeyboardButton("🧩 Skills", callback_data="nav:skills"),
            types.InlineKeyboardButton("📋 Backlog", callback_data="nav:backlog"),
            types.InlineKeyboardButton("♥ Vitals", callback_data="nav:vitals"),
        )
        kb.add(
            types.InlineKeyboardButton("⚙ Status", callback_data="nav:status"),
            types.InlineKeyboardButton("🎛 Panel", callback_data="nav:panel"),
            types.InlineKeyboardButton("🧪 Evolve", callback_data="nav:evolve"),
        )
        bot.reply_to(message, text, parse_mode="HTML", reply_markup=kb)

    @bot.message_handler(commands=["skills"])
    def cmd_skills(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        _send_skills_panel(message.chat.id, reply_to=message.message_id)

    def _send_skills_panel(chat_id: int, *, reply_to: int | None = None) -> None:
        skills = _load_skills()
        if not skills:
            bot.send_message(chat_id, "暂无已注册技能。")
            return

        runners = sorted(p.parent.name for p in (ws_path / "skills").glob("*/runner.py"))

        text = "<b>🧩 D E V C L A W   S K I L L S</b>\n\n"
        for s in skills:
            icon = _get_skill_icon(s["id"])
            text += f"  {icon} <b>{s['id']}</b>\n"
            text += f"      <i>{s['title']}</i>\n\n"

        text += (
            f"⚙️ <b>Active Organs</b> ({len(runners)})\n"
            f"<code>{'  '.join(runners)}</code>\n\n"
            "👇 <b>点击按钮一键启动技能</b>"
        )

        kb = types.InlineKeyboardMarkup(row_width=2)
        btn_pairs = []
        for s in skills:
            icon = _get_skill_icon(s["id"])
            label = s["id"].replace("_", " ").title()
            if len(label) > 18:
                label = label[:16] + ".."
            # Telegram callback_data max 64 bytes — "skill:" = 6 chars, leave 58 for id
            btn_pairs.append(
                types.InlineKeyboardButton(
                    f"{icon} {label}",
                    callback_data=f"skill:{s['id'][:58]}",
                )
            )
        for i in range(0, len(btn_pairs), 2):
            kb.add(*btn_pairs[i:i+2])
        kb.add(types.InlineKeyboardButton("🏠 返回家园", callback_data="nav:home"))

        if reply_to:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, reply_to_message_id=reply_to)
        else:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)

    @bot.message_handler(commands=["backlog"])
    def cmd_backlog(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if not _is_admin(cid, admins):
            bot.reply_to(message, f"无权限。chat_id={message.chat.id}")
            return
        _send_backlog_panel(message.chat.id, reply_to=message.message_id)

    def _send_backlog_panel(chat_id: int, *, reply_to: int | None = None) -> None:
        try:
            bl = (ws_path / "EVOLUTION_BACKLOG.md").read_text("utf-8")
        except Exception:
            bot.send_message(chat_id, "无法读取 EVOLUTION_BACKLOG.md")
            return

        active_lines, done_count = [], 0
        for line in bl.splitlines():
            s = line.strip()
            if s.startswith(("- [ ]", "- [RESEARCH]", "- [PROFIT]")):
                active_lines.append(s)
            elif "[DONE]" in s or s.startswith("- [x]"):
                done_count += 1

        text = "<b>📋 E V O L U T I O N   B A C K L O G</b>\n\n"
        if active_lines:
            text += "🔴 <b>Active</b>\n"
            for i, l in enumerate(active_lines[:8], 1):
                display = l.lstrip("- ").replace("[RESEARCH]", "🔬").replace("[PROFIT]", "💰").replace("[ ]", "⬜")
                text += f"  {i}. {display}\n"
            if len(active_lines) > 8:
                text += f"\n  <i>...+{len(active_lines)-8} more</i>\n"
        else:
            text += "✅ <b>队列清空 — 无待处理任务</b>\n"
        text += f"\n✅ <b>Completed:</b> <code>{done_count}</code>"

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("🔄 刷新", callback_data="nav:backlog"),
            types.InlineKeyboardButton("🏠 家园", callback_data="nav:home"),
        )
        if reply_to:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, reply_to_message_id=reply_to)
        else:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)

    # ---- Callback query handler — 按钮点击路由 ----

    @bot.callback_query_handler(func=lambda call: True)
    def on_callback_query(call: telebot.types.CallbackQuery) -> None:
        cid = str(call.message.chat.id)
        if not _is_admin(cid, admins):
            bot.answer_callback_query(call.id, "无权限", show_alert=True)
            return

        data = call.data or ""
        chat_id = call.message.chat.id

        # Navigation
        if data == "nav:skills":
            bot.answer_callback_query(call.id)
            _send_skills_panel(chat_id)
        elif data == "nav:backlog":
            bot.answer_callback_query(call.id)
            _send_backlog_panel(chat_id)
        elif data == "nav:vitals":
            bot.answer_callback_query(call.id)
            bal, bmr, ttl, mode, mode_e = _get_vitals()
            bar_len = 20
            filled = min(int(ttl / 60 * bar_len), bar_len)
            hp = "▓" * filled + "░" * (bar_len - filled)
            text = (
                f"<b>♥ V I T A L S</b>\n\n"
                f"  {mode_e} <b>Mode:</b>  <code>{mode}</code>\n"
                f"  ♥ <b>TTL:</b>   <code>[{hp}]</code> <b>{ttl:.0f} days</b>\n"
                f"  💰 <b>Bal:</b>   <code>${bal:.2f}</code>\n"
                f"  🔥 <b>Burn:</b>  <code>${bmr:.2f}/day</code>\n"
            )
            kb = types.InlineKeyboardMarkup()
            kb.add(
                types.InlineKeyboardButton("🔄 刷新", callback_data="nav:vitals"),
                types.InlineKeyboardButton("🏠 家园", callback_data="nav:home"),
            )
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        elif data == "nav:home":
            bot.answer_callback_query(call.id)
            cmd_home(call.message)
        elif data == "nav:status":
            bot.answer_callback_query(call.id)
            cmd_status(call.message)
        elif data == "nav:panel":
            bot.answer_callback_query(call.id)
            cmd_panel(call.message)
        elif data == "nav:evolve":
            bot.answer_callback_query(call.id)
            cmd_evolve(call.message)

        # Skill activation — one-tap to start conversation
        elif data.startswith("skill:"):
            skill_id = data[6:]
            bot.answer_callback_query(call.id, f"⚡ 启动 {skill_id}...")
            prompt = _SKILL_PROMPTS.get(skill_id, f"运行技能 {skill_id}，给我一份简报")
            icon = _get_skill_icon(skill_id)
            bot.send_message(
                chat_id,
                f"{icon} <b>启动技能:</b> <code>{_html_mod.escape(skill_id)}</code>\n"
                f"📝 <i>{_html_mod.escape(prompt)}</i>",
                parse_mode="HTML",
            )
            _handle_text_input(chat_id, prompt, actor=f"tg:{chat_id}", channel="tg")

        # Skill detail panel
        elif data.startswith("skillinfo:"):
            skill_id = data[10:]
            bot.answer_callback_query(call.id)
            skills = _load_skills()
            skill = next((s for s in skills if s["id"] == skill_id), None)
            if not skill:
                bot.send_message(chat_id, "技能未找到。")
                return
            icon = _get_skill_icon(skill_id)
            text = (
                f"<b>{icon} {skill['title']}</b>\n\n"
                f"  📌 <b>ID:</b>  <code>{skill_id}</code>\n"
                f"  📁 <b>Path:</b> <code>{skill.get('path', 'N/A')}</code>\n"
                f"  🔧 <b>Invoke:</b> <code>{skill.get('invoke', 'N/A')}</code>\n"
            )
            if skill.get("notes"):
                text += f"\n  📝 <i>{skill['notes']}</i>\n"
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton(f"⚡ 启动", callback_data=f"skill:{skill_id}"),
                types.InlineKeyboardButton("◀ 返回技能", callback_data="nav:skills"),
            )
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        else:
            bot.answer_callback_query(call.id, "未知操作")

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
        bal, bmr, ttl, mode, mode_e = _get_vitals()
        skills = _load_skills()
        text = (
            "<b>🏠 DevClaw TG Gateway</b>\n\n"
            f"{mode_e} <code>{mode}</code>  ·  ♥ <b>{ttl:.0f}d</b>  ·  🧩 <b>{len(skills)}</b> skills\n\n"

            "┌─ <b>📊 总览</b> ─────────────┐\n"
            "│ /home     像素家园总览\n"
            "│ /skills    技能面板 (可点击启动)\n"
            "│ /backlog  进化任务队列\n"
            "│ /vitals    TTL · 余额 · 状态\n"
            "├─ <b>⚙ 运维</b> ─────────────┤\n"
            "│ /status    队列与工作区\n"
            "│ /panel     运行控制面板\n"
            "│ /pause    暂停自主循环\n"
            "│ /resume  恢复开工\n"
            "│ /sim        锁定 simulation\n"
            "├─ <b>🧬 进化</b> ─────────────┤\n"
            "│ /evolve   失败日志 → 新 SKILL\n"
            "├─ <b>🔧 工具</b> ─────────────┤\n"
            "│ /whoami  查看 chat_id\n"
            "│ /ping      存活检测\n"
            "└──────────────────────────┘\n\n"
            "<i>也可直接发自然语言指令</i>"
        )

        kb = types.InlineKeyboardMarkup(row_width=3)
        kb.add(
            types.InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            types.InlineKeyboardButton("🧩 Skills", callback_data="nav:skills"),
            types.InlineKeyboardButton("📋 Backlog", callback_data="nav:backlog"),
        )
        kb.add(
            types.InlineKeyboardButton("♥ Vitals", callback_data="nav:vitals"),
            types.InlineKeyboardButton("⚙ Status", callback_data="nav:status"),
            types.InlineKeyboardButton("🎛 Panel", callback_data="nav:panel"),
        )
        bot.reply_to(message, text, parse_mode="HTML", reply_markup=kb)

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
