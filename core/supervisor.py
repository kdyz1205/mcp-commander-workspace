"""
DevClaw Supervisor — The metabolic heart.

An infinite loop daemon that:
1. Checks TTL (life signs)
2. Decides strategy: RESEARCH / BALANCE / SURVIVAL
3. Dispatches tasks accordingly
4. Sleeps, repeats

This is the "life force" that makes DevClaw autonomous.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from core.vitals import calculate_ttl, get_vitals_summary
from core.mcp_server import ToolRegistry

logging.basicConfig(level=logging.INFO, format='%(asctime)s [HEARTBEAT] %(message)s')


class DevClawSupervisor:
    """
    The metabolic scheduler. Balances survival vs research.

    Strategy matrix:
    - TTL > 30: RESEARCH (refactor, optimize, learn)
    - 7 < TTL <= 30: BALANCE (earn + iterate)
    - TTL <= 7: SURVIVAL (stop everything, earn money, recharge)
    """

    def __init__(
        self,
        balance_path: str = ".auth/balance.json",
        backlog_path: str = "EVOLUTION_BACKLOG.md",
        sleep_interval: int = 600,
    ):
        self.balance_path = balance_path
        self.backlog_path = backlog_path
        self.sleep_interval = sleep_interval
        self.registry = ToolRegistry()

    def decide_strategy(self, ttl: float) -> tuple[str, str]:
        """
        Core intelligence: TTL → strategy.

        Returns (mode, directive_text).
        """
        if ttl > 30:
            return "RESEARCH", (
                "生命力充沛。启动底层重构与算法优化。"
                "读取 EVOLUTION_BACKLOG.md 选取非紧急任务执行。"
            )
        elif ttl > 7:
            return "BALANCE", (
                "资源稳健。一边盈利一边迭代。"
                "优先执行有收益的任务，同时进行小规模代码优化。"
            )
        else:
            return "SURVIVAL", (
                "致命危险！停止一切科研，全力续命。"
                "调用盈利脚本赚取资金，执行 recharge 为系统充值。"
            )

    def get_next_backlog_task(self) -> str | None:
        """Read first unchecked task from backlog."""
        try:
            for line in Path(self.backlog_path).read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("- [ ]"):
                    return line.strip()[6:].strip()
        except OSError:
            pass
        return None

    def run_single_cycle(self) -> dict:
        """Run one heartbeat cycle. Returns status dict."""
        ttl = calculate_ttl(self.balance_path)
        mode, directive = self.decide_strategy(ttl)
        vitals = get_vitals_summary(self.balance_path)

        logging.info(f"{vitals} | MODE: {mode}")

        result = {
            "ttl": ttl,
            "mode": mode,
            "directive": directive,
            "action_taken": None,
        }

        if mode == "SURVIVAL":
            # In survival mode, try to earn money
            result["action_taken"] = "survival_scan"
            logging.info("SURVIVAL: Looking for profit opportunities...")

        elif mode == "BALANCE":
            # Balance: try backlog + earnings
            task = self.get_next_backlog_task()
            if task:
                result["action_taken"] = f"backlog: {task[:60]}"
                logging.info(f"BALANCE: Working on '{task[:60]}'")
            else:
                result["action_taken"] = "idle_optimization"

        elif mode == "RESEARCH":
            # Research: pure learning and optimization
            task = self.get_next_backlog_task()
            if task:
                result["action_taken"] = f"research: {task[:60]}"
                logging.info(f"RESEARCH: Deep work on '{task[:60]}'")
            else:
                result["action_taken"] = "self_optimization"
                logging.info("RESEARCH: No backlog tasks, optimizing self...")

        return result

    def run_daemon(self):
        """
        Infinite life loop. The heartbeat that never stops.

        This is the daemon that makes DevClaw alive.
        """
        logging.info("DevClaw Supervisor starting. Entering life loop.")

        while True:
            try:
                self.run_single_cycle()
            except Exception as e:
                logging.error(f"Heartbeat anomaly: {e}")

            time.sleep(self.sleep_interval)


if __name__ == "__main__":
    import sys
    # Initialize balance if missing
    if not Path(".auth/balance.json").is_file():
        Path(".auth").mkdir(exist_ok=True)
        Path(".auth/balance.json").write_text(
            '{"balance": 50, "bmr": 1.0}', encoding="utf-8"
        )
        print("Initialized balance: $50, BMR: $1/day")

    sup = DevClawSupervisor(sleep_interval=10)  # 10s for testing
    # Run just one cycle for testing, then exit
    if "--once" in sys.argv:
        result = sup.run_single_cycle()
        print(f"Result: {result}")
    else:
        sup.run_daemon()
