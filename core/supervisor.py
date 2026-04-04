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
from core.metabolic_kernel import calculate_psi, psi_gate, PsiSnapshot
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

    def get_next_backlog_task(self, *, prefer_type: str | None = None) -> str | None:
        """Read next unchecked task from backlog.

        Args:
            prefer_type: If set (e.g., "PROFIT"), return tasks of that type first.
                         Falls back to any task if no preferred type found.
        """
        import re
        try:
            lines = Path(self.backlog_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        preferred = []
        fallback = []

        for line in lines:
            s = line.strip()
            if s.startswith("- [ ]"):
                fallback.append(s[6:].strip())
            else:
                m = re.match(r"^- \[(RESEARCH|PROFIT|CRITICAL_EVOLUTION)\]\s+(.*)", s)
                if m:
                    task_type, task_text = m.group(1), m.group(2).strip()
                    if prefer_type and task_type == prefer_type:
                        preferred.append(task_text)
                    elif task_type == "CRITICAL_EVOLUTION":
                        # Pain-sensor tasks always have high priority
                        preferred.insert(0, task_text)
                    else:
                        fallback.append(task_text)

        if preferred:
            return preferred[0]
        if fallback:
            return fallback[0]
        return None

    def run_single_cycle(self) -> dict:
        """Run one heartbeat cycle. Returns status dict."""
        ws = Path(self.backlog_path).parent.resolve()

        # ── Ψ: Survival Pressure — the master control variable ──
        psi = calculate_psi(ws, balance_path=self.balance_path)
        ttl = psi.ttl_days
        mode = psi.mode  # PREDATOR / BALANCED / EXPLORER
        vitals = get_vitals_summary(self.balance_path)

        logging.info(f"{vitals} | Ψ={psi.psi:.2f} | MODE: {mode}")

        result = {
            "ttl": ttl,
            "psi": psi.psi,
            "mode": mode,
            "action_taken": None,
        }

        # ── 3-strike emergency: no profit in 3 cycles → self-diagnostic ──
        if not hasattr(self, "_no_profit_streak"):
            self._no_profit_streak = 0

        if mode == "PREDATOR":
            # Ψ > 0.8: 掠食者模式 — 只做赚钱的事
            result["action_taken"] = "predator_hunt"
            logging.info(f"PREDATOR (Ψ={psi.psi:.2f}): 停止科研，全力续命！")
            task = self.get_next_backlog_task(prefer_type="PROFIT")
            if task:
                self._execute_task(task, result)
                self._no_profit_streak = 0
            else:
                logging.info("PREDATOR: No PROFIT tasks, running profit_hunter scan...")
                self._execute_task("扫描套利机会（funding rate + DEX 价差），找到就报告", result)
                self._no_profit_streak += 1

            # 3-strike: force self-diagnostic
            if self._no_profit_streak >= 3:
                logging.warning("PREDATOR: 3 cycles without profit! Emergency self-diagnostic.")
                self._auto_survival_task(ws)
                self._no_profit_streak = 0

        elif mode == "BALANCED":
            # Ψ 0.4~0.8: 平衡模式
            task = self.get_next_backlog_task()
            if task:
                # Gate: check if task type is allowed at current Ψ
                allowed, reason, _ = psi_gate(ws, task_type="general")
                if allowed:
                    self._execute_task(task, result)
                else:
                    logging.info(f"PSI_GATE blocked: {reason}")
                    # Fall back to profit task
                    profit_task = self.get_next_backlog_task(prefer_type="PROFIT")
                    if profit_task:
                        self._execute_task(profit_task, result)
                    else:
                        result["action_taken"] = "gated_idle"
            else:
                result["action_taken"] = "idle_optimization"

        elif mode == "EXPLORER":
            # Ψ < 0.4: 探索者模式 — 可以用顶级算力
            task = self.get_next_backlog_task()
            if task:
                self._execute_task(task, result)
            else:
                result["action_taken"] = "self_optimization"
                logging.info("EXPLORER: No backlog tasks, optimizing self...")

        return result

    def _auto_survival_task(self, workspace: Path) -> None:
        """Auto-add SURVIVAL task to backlog when 3 cycles yield no profit."""
        try:
            bl = Path(self.backlog_path)
            content = bl.read_text(encoding="utf-8") if bl.is_file() else ""
            marker = "[SURVIVAL] 紧急自检"
            if marker not in content:
                task = f"- [PROFIT] {marker}：分析为什么连续3轮无盈利，重写低效模块以节省资源"
                content = content.replace(
                    "## Active Tasks\n",
                    f"## Active Tasks\n{task}\n",
                )
                bl.write_text(content, encoding="utf-8")
                logging.info(f"AUTO-SURVIVAL: Added emergency task to backlog")
        except Exception as e:
            logging.error(f"AUTO-SURVIVAL failed: {e}")

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
            # Build prompt as a temp file to avoid shell quoting issues
            prompt = (
                f"修改代码完成以下需求：{task}\n\n"
                f"直接读取相关文件、修改代码、运行测试。不要问我要做什么，直接做。"
            )
            # Use stdin piping to avoid shell quoting issues on Windows
            r = subprocess.run(
                [claude_bin, "--dangerously-skip-permissions", "-p", prompt],
                capture_output=True, text=True, timeout=300,
                cwd=ws, encoding="utf-8", errors="replace",
                input="",  # Provide empty stdin to prevent hanging
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
