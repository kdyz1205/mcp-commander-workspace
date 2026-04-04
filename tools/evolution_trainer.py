"""
Evolution Trainer — Continuous training loop between Claude Opus and DevClaw.

Opens in a PowerShell window showing real-time conversation and task execution.
Runs indefinitely until manually stopped.
"""
import sys
import os
import time
import re
import json
import subprocess
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DEVCLAW_WORKSPACE", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.environ["DEVCLAW_WORKSPACE"], ".env"), override=False)
except ImportError:
    pass

from claw_runtime.operator_bridge import enqueue_operator_message

# ANSI colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

WS = os.environ["DEVCLAW_WORKSPACE"]


def clean(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\[\d*[A-Z]|\[K', '', text).strip()


def chat(msg, max_wait=90):
    """Send message to DevClaw and wait for response."""
    rid = enqueue_operator_message(WS, msg, chat_id=0, source="evolution_trainer")
    start = time.time()
    for _ in range(max_wait // 3):
        time.sleep(3)
        try:
            for ln in open(os.path.join(WS, ".claw/operator_outbox.jsonl"), encoding="utf-8").readlines():
                d = json.loads(ln.strip())
                if d.get("id") == rid and d.get("kind") == "progress":
                    t = clean(d.get("text", ""))
                    if t and len(t) > 10:
                        return t[:800], round(time.time() - start, 1)
            for ln in open(os.path.join(WS, ".claw/operator_outbox.jsonl"), encoding="utf-8").readlines():
                d = json.loads(ln.strip())
                if d.get("id") == rid:
                    t = clean(d.get("text", ""))
                    if t and len(t) > 10 and all(x not in t for x in ["已入队", "收到", "执行中", "调用"]):
                        return t[:800], round(time.time() - start, 1)
        except Exception:
            pass
    return None, max_wait


def say(role, text, elapsed=None):
    """Print a message in the conversation."""
    if role == "opus":
        icon = f"{CYAN}{BOLD}► Claude Opus{RESET}"
    else:
        icon = f"{GREEN}{BOLD}◄ DevClaw{RESET}"
        if elapsed:
            icon += f" {DIM}({elapsed}s){RESET}"
    print(f"\n  {icon}")
    for line in text.split("\n")[:15]:
        print(f"    {WHITE}{line[:100]}{RESET}")


def status(text):
    print(f"\n  {YELLOW}{'━'*60}{RESET}")
    print(f"  {YELLOW}{BOLD}  {text}{RESET}")
    print(f"  {YELLOW}{'━'*60}{RESET}")


def verify(description, check_fn):
    """Run a verification and show result."""
    try:
        result = check_fn()
        icon = f"{GREEN}✓{RESET}" if result else f"{RED}✗{RESET}"
        print(f"  {icon} {description}: {result}")
        return result
    except Exception as e:
        print(f"  {RED}✗{RESET} {description}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# TRAINING CURRICULUM
# ═══════════════════════════════════════════════════════════════

CURRICULUM = [
    # Phase 1: Self-awareness verification
    {
        "phase": "Phase 1: 自我意识验证",
        "tasks": [
            ("你现在的系统状态是什么？内存、CPU、时间都告诉我", "system_awareness", 15),
            ("你的代码库里有多少个文件？用命令数一下", "tool_use", 30),
            ("读取 .claw/intelligence_profile.json 告诉我你的IQ和学到的规则", "self_knowledge", 30),
        ],
    },
    # Phase 2: Self-diagnosis
    {
        "phase": "Phase 2: 自我诊断能力",
        "tasks": [
            ("检查 .claw/consciousness_log.jsonl 最后5条记录，分析你最近的意识活动是否正常", "diagnosis", 45),
            ("检查 .claw/error_attributions.jsonl 有没有反复出现的错误模式", "error_analysis", 45),
            ("你觉得你现在最大的三个弱点是什么？要基于真实数据，不要编造", "honest_assessment", 15),
        ],
    },
    # Phase 3: Self-modification
    {
        "phase": "Phase 3: 自我修改能力",
        "tasks": [
            ("在 .claw/intelligence_profile.json 的 learned_rules 里添加一条你今天真正学到的新规则", "modify_config", 45),
            ("在 .claw/inner_voice.md 末尾写下你对自己当前状态的真实评估", "write_diary", 30),
            ("读取 claw_runtime/consciousness_seed.py 的 act_1_self_awareness 函数，找出一个可以改进的地方并修改它", "modify_code", 60),
        ],
    },
    # Phase 4: Skill evolution
    {
        "phase": "Phase 4: 技能进化",
        "tasks": [
            ("检查 skills/ 目录下所有技能，哪些有 runner.py 哪些没有？列出来", "skill_audit", 30),
            ("创建一个新文件 .claw/evolution_plan.md 写下你接下来想学习的3个新能力", "planning", 45),
            ("读取 skills/logic_healer/runner.py 的 analyze_error 函数，告诉我它能识别哪些错误类型", "code_reading", 45),
        ],
    },
    # Phase 5: Real-world tasks
    {
        "phase": "Phase 5: 真实世界任务",
        "tasks": [
            ("用 git log --oneline -5 看看最近5次提交是什么", "git_usage", 30),
            ("检查当前目录的磁盘使用情况，哪个目录最大", "system_admin", 30),
            ("创建一个 Python 脚本 tools/hello_evolved.py 内容是打印当前时间和系统信息", "code_generation", 45),
        ],
    },
    # Phase 6: Meta-cognition
    {
        "phase": "Phase 6: 元认知——思考如何思考",
        "tasks": [
            ("回顾我们今天的所有对话，你觉得你进步了多少？具体在哪些方面？", "meta_reflection", 15),
            ("如果你能修改自己的system prompt，你会加什么规则让自己更聪明？", "meta_optimization", 15),
            ("把你想加的规则写进 .cursorrules 文件末尾", "cursorrules_evolution", 45),
        ],
    },
]


def main():
    os.system("")  # Enable ANSI on Windows

    print(f"\n{CYAN}{'═'*60}{RESET}")
    print(f"{CYAN}║{RESET}{BOLD}  DEVCLAW EVOLUTION TRAINER v1.0{RESET}")
    print(f"{CYAN}║{RESET}  Claude Opus 4.6 ←→ DevClaw")
    print(f"{CYAN}║{RESET}  {DIM}Continuous training until interrupted{RESET}")
    print(f"{CYAN}{'═'*60}{RESET}")

    round_num = 0
    total_pass = 0
    total_tasks = 0

    while True:
        round_num += 1
        print(f"\n{MAGENTA}{'▓'*60}{RESET}")
        print(f"{MAGENTA}  EVOLUTION ROUND {round_num}{RESET}")
        print(f"{MAGENTA}{'▓'*60}{RESET}")

        for phase in CURRICULUM:
            status(phase["phase"])

            for task_msg, task_type, max_wait in phase["tasks"]:
                total_tasks += 1

                say("opus", task_msg)
                a, t = chat(task_msg, max_wait)

                if a:
                    say("claw", a, t)
                    total_pass += 1
                    print(f"  {GREEN}✓ PASS{RESET} ({t}s)")
                else:
                    print(f"  {RED}✗ TIMEOUT{RESET} ({max_wait}s)")

                time.sleep(2)

        # End of round summary
        status(f"ROUND {round_num} COMPLETE: {total_pass}/{total_tasks} total pass rate")

        # Check evolution progress
        try:
            p = json.loads(open(os.path.join(WS, ".claw/intelligence_profile.json"), encoding="utf-8").read())
            iq = p.get("overall_iq", 0)
            gen = p.get("generation", 0)
            rules = len(p.get("learned_rules", []))
            print(f"  {WHITE}IQ: {iq} | Generation: {gen} | Rules: {rules}{RESET}")
        except Exception:
            print(f"  {DIM}No intelligence profile yet{RESET}")

        print(f"\n  {DIM}Starting round {round_num + 1} in 10 seconds...{RESET}")
        time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n{YELLOW}Training stopped by user.{RESET}")
