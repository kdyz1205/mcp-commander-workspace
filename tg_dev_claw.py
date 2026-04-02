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

运行（建议在本仓库根目录）:
  py tg_dev_claw.py
  或 scripts/start-tg-bot.ps1
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from typing import Set

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import telebot
    from telebot import types
except ImportError:
    print("请安装: py -m pip install -r requirements-telegram.txt", file=sys.stderr)
    raise SystemExit(1) from None

from dev_claw.main import dev_claw_run

TG_CHUNK = 3800
_DEFAULT_ITERS = 24


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
        types.BotCommand("cancel", "取消说明（单 worker 版）"),
    ]
    try:
        bot.set_my_commands(cmds)
        print("已注册 Bot 命令菜单 (set_my_commands)")
    except Exception as e:  # noqa: BLE001
        print(f"set_my_commands 失败（可忽略）: {e}", file=sys.stderr)


def _text_looks_like_command(text: str) -> bool:
    t = (text or "").lstrip()
    return len(t) >= 2 and t.startswith("/") and (len(t) == 1 or t[1].isalpha())


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

    bot = telebot.TeleBot(token, parse_mode=None)
    task_q: queue.Queue[tuple[int, str]] = queue.Queue()
    max_iters = int(os.environ.get("TG_DEVCLAW_MAX_ITERS", str(_DEFAULT_ITERS)))
    system_append = os.environ.get("TG_DEVCLAW_SYSTEM_APPEND", "").strip() or None

    _register_bot_commands(bot)

    def worker() -> None:
        while True:
            chat_id, instruction = task_q.get()
            try:

                def hook(msg: str) -> None:
                    try:
                        _send_chunks(bot, chat_id, msg)
                    except Exception:  # noqa: BLE001
                        pass

                dev_claw_run(
                    instruction,
                    max_iterations=max_iters,
                    system_append=system_append,
                    progress_hook=hook,
                )
            except Exception as e:  # noqa: BLE001
                err = f"[DevClaw 异常]\n{e!s}\n\n{traceback.format_exc()}"[:8000]
                try:
                    _send_chunks(bot, chat_id, err)
                except Exception:
                    pass
            finally:
                task_q.task_done()

    threading.Thread(target=worker, daemon=True, name="devclaw-worker").start()

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
        bot.reply_to(
            message,
            "DevClaw 状态\n"
            f"工作区: {os.environ.get('DEVCLAW_WORKSPACE')}\n"
            f"队列中任务数: {qn}\n"
            f"最大迭代: {max_iters}\n"
            "说明: 单 worker 串行处理，上一条跑完才处理下一条。",
        )

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
            "/cancel — 取消说明\n\n"
            f"工作区: {os.environ.get('DEVCLAW_WORKSPACE')}\n"
            f"最大迭代: {max_iters}\n\n"
            "直接发普通文字（不以 / 开头）会入队交给 DevClaw 执行。",
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
        text = (message.text or "").strip()
        if not text:
            return
        bot.reply_to(message, "已入队，DevClaw 开始处理…")
        task_q.put((message.chat.id, text))

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
    bot.infinity_polling(skip_pending=True, interval=1, timeout=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
