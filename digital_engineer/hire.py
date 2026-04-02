"""
Hire a local "digital AI engineer" loop (DevClaw + stronger product prompt).
Run from repo root:

  set OPENAI_API_KEY=...
  py digital_engineer\\hire.py --bot-root C:\\path\\to\\crypto-tg-bot "修 onchain_filter 导入错误并跑通测试"

If --bot-root is omitted, DEVCLAW_WORKSPACE / 当前仓库根 即为工作区。
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dev_claw.main import dev_claw_run  # noqa: E402

HIRED_ENGINEER_APPEND = """
【雇佣身份：数字 AI 工程师】你受雇维护当前工作区内的软件（常为 Crypto Telegram Bot）。
目标：写代码、修 Bug、加功能，并通过终端自测形成闭环；禁止只给“纸面方案”。

【流程】
1. 先读 `task_plan.md`（若无则创建）：把用户目标拆成可验证子任务；大改动必须先更新计划再动代码。
2. 定位相关文件：用 `edit_local_file` mode=r 读源码与配置；必要时 `execute_terminal` 列目录（Windows: `dir /b`）。
3. 修 Bug：复现 → 运行入口或测试（如 `py -m pytest`、`py bot.py` 等以项目为准）→ 根据完整 Traceback 修改 → 再跑直到通过或明确阻塞。
4. 自我阻断：若缺 API/ABI/反爬/依赖冲突，暂停主线，在 `task_plan.md` 记录旁路；在 `tools/` 或 `temp_tools/` 写最小探针脚本，跑通后写入 `skills.md` 与 `skills/skills.json`。
5. 行情类逻辑：若涉及实时币价/DEX，对**本仓库**可调用 `py tools\\web_agent.py "..."`（若 Bot 仓库内无该脚本，先用 Brave/浏览器 MCP 或公开 API 文档，勿编造数字）。
6. **OpenClaw 式技能**：系统提示里会有技能目录；复杂流程先 `load_skill` 再执行。可在 `./skills/<name>/SKILL.md` 新增技能，下一轮热重载生效。
7. 元改进：若发现流程缺陷，优先更新 `task_plan.md` 与测试命令，而不是堆屎山。

【安全】对用户机器有高权限；删除/格式化/覆盖敏感配置前须二次确认思路，默认不做破坏性系统命令。
"""


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="Digital AI Engineer — hired DevClaw session")
    p.add_argument("instruction", nargs="*", help="任务描述")
    p.add_argument("--max-iters", type=int, default=24, help="最大工具循环次数")
    p.add_argument(
        "--bot-root",
        type=str,
        default=None,
        help="Crypto TG Bot（或其它项目）根目录；会设置 DEVCLAW_WORKSPACE",
    )
    args = p.parse_args()

    if args.bot_root:
        os.environ["DEVCLAW_WORKSPACE"] = os.path.abspath(args.bot_root)

    text = " ".join(args.instruction).strip()
    if not text:
        text = (
            "读取当前工作区的 task_plan.md 与主要入口文件，列出下一步最小验证步骤，"
            "并执行第一条可在本机完成的终端命令。"
        )

    dev_claw_run(
        text,
        max_iterations=args.max_iters,
        system_append=HIRED_ENGINEER_APPEND,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
