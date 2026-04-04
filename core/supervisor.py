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
            result["action_taken"] = "survival_scan"
            logging.info("SURVIVAL: Looking for profit opportunities...")
            # In survival, only pick PROFIT tasks
            task = self.get_next_backlog_task()
            if task:
                self._execute_task(task, result)

        elif mode == "BALANCE":
            task = self.get_next_backlog_task()
            if task:
                self._execute_task(task, result)
            else:
                result["action_taken"] = "idle_optimization"

        elif mode == "RESEARCH":
            task = self.get_next_backlog_task()
            if task:
                self._execute_task(task, result)
            else:
                result["action_taken"] = "self_optimization"
                logging.info("RESEARCH: No backlog tasks, optimizing self...")

        return result

    def _execute_task(self, task: str, result: dict) -> None:
        """
        Execute a backlog task using Claude CLI + TDD Engine.

        This is the neural splice: Supervisor → Claude CLI → TDD → Experience.
        """
        import shutil
        import subprocess
        import re

        result["action_taken"] = f"executing: {task[:60]}"
        logging.info(f"EXECUTING: {task[:60]}")

        claude_bin = shutil.which("claude") or "claude"
        ws = str(Path(self.backlog_path).parent.resolve())

        try:
            # Use Claude CLI to actually execute the task
            r = subprocess.run(
                [claude_bin, "--dangerously-skip-permissions", "-p",
                 f"你是DevClaw。在当前目录执行此任务（直接做，不要解释）：\n{task[:2000]}"],
                capture_output=True, text=True, timeout=300,
                cwd=ws, encoding="utf-8", errors="replace",
            )

            if r.returncode == 0 and r.stdout.strip():
                output = r.stdout.strip()[:2000]
                logging.info(f"TASK OUTPUT: {output[:200]}")

                # Only mark done if Claude CLI actually did something
                # (not just "I don't understand" or "task incomplete")
                _did_nothing = any(x in output.lower() for x in [
                    "不完整", "incomplete", "请告诉我", "tell me", "什么任务",
                    "不理解", "don't understand", "需要更多", "need more",
                ])
                if _did_nothing:
                    logging.warning(f"TASK NOT COMPLETED: Claude CLI didn't execute")
                    result["action_taken"] = f"not_understood: {task[:40]}"
                    result["output"] = output
                else:
                    result["action_taken"] = f"completed: {task[:40]}"
                    result["output"] = output
                    try:
                        from core.backlog_manager import mark_task_done
                        mark_task_done(self.backlog_path, task)
                        logging.info(f"BACKLOG: Marked done '{task[:40]}'")
                    except Exception:
                        pass

                # Experience consolidation
                try:
                    from claw_runtime.experience_consolidator import persist_rule
                    persist_rule(ws, {
                        "trigger_condition": f"Task type: {task[:50]}",
                        "forbidden_action": "Don't skip testing",
                        "enforced_action": f"Completed via: Claude CLI execution",
                    })
                except Exception:
                    pass
            else:
                error = (r.stderr or "")[:500]
                logging.error(f"TASK FAILED: {error[:200]}")
                result["action_taken"] = f"failed: {task[:40]}"
                result["error"] = error

        except subprocess.TimeoutExpired:
            logging.error("TASK TIMEOUT (300s)")
            result["action_taken"] = f"timeout: {task[:40]}"
        except FileNotFoundError:
            logging.error("Claude CLI not found — cannot execute tasks")
            result["action_taken"] = "no_brain"
        except Exception as e:
            logging.error(f"EXECUTION ERROR: {e}")
            result["action_taken"] = f"error: {e!s}"

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
