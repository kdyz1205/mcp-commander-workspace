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


def _send_chunks(bot: telebot.TeleBot, chat_id: int, text: str) -> None:
    t = text or ""
    if not t:
        t = "(空)"
    for i in range(0, len(t), TG_CHUNK):
        chunk = t[i : i + TG_CHUNK]
        try:
            bot.send_message(chat_id, chunk)
        except Exception as e:  # noqa: BLE001
            bot.send_message(chat_id, f"[send_message 失败] {e!s}\n{chunk[:500]}")


def main() -> int:
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    admins = _admin_ids()
    if not token or not admins:
        print(
            "缺少 TG_BOT_TOKEN 或 TG_ADMIN_CHAT_IDS。\n"
            "参考 env.example 设置环境变量。",
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

    @bot.message_handler(commands=["start", "help"])
    def cmd_help(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if cid not in admins:
            bot.reply_to(message, "无权限。")
            return
        bot.reply_to(
            message,
            "DevClaw TG 网关\n"
            f"工作区: {os.environ.get('DEVCLAW_WORKSPACE')}\n"
            f"最大迭代: {max_iters}\n\n"
            "直接发文字即执行任务。\n"
            "/help 显示本说明。",
        )

    @bot.message_handler(content_types=["text"])
    def on_text(message: telebot.types.Message) -> None:
        cid = str(message.chat.id)
        if cid not in admins:
            bot.reply_to(message, "无权限：你的 chat id 不在 TG_ADMIN_CHAT_IDS。")
            return
        text = (message.text or "").strip()
        if not text:
            return
        bot.reply_to(message, "已入队，DevClaw 开始处理…")
        task_q.put((message.chat.id, text))

    print("TG DevClaw 监听中… 工作区:", os.environ.get("DEVCLAW_WORKSPACE"))
    bot.infinity_polling(skip_pending=True, interval=1, timeout=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
