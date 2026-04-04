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


def chat(msg, max_wait=120):
    """Send message to DevClaw and wait for ANY meaningful response.
    Records outbox line count BEFORE sending so we only check NEW lines."""
    outbox = os.path.join(WS, ".claw/operator_outbox.jsonl")
    _noise = ("已入队", "收到", "执行中", "调用", "思考中", "处理中")

    # Snapshot outbox size before sending
    try:
        before_count = sum(1 for _ in open(outbox, encoding="utf-8"))
    except (FileNotFoundError, OSError):
        before_count = 0

    rid = enqueue_operator_message(WS, msg, chat_id=0, source="evolution_trainer")
    start = time.time()

    for _ in range(max_wait // 2):
        time.sleep(2)
        try:
            lines = open(outbox, encoding="utf-8").readlines()
            # Only check lines AFTER our message was sent
            new_lines = lines[before_count:]
            best = None
            for ln in new_lines:
                if not ln.strip():
                    continue
                d = json.loads(ln.strip())
                if d.get("id") != rid:
                    continue
                t = clean(d.get("text", ""))
                if not t or len(t) < 8:
                    continue
                if any(t.startswith(n) for n in _noise):
                    continue
                if best is None or len(t) > len(best):
                    best = t[:800]
            if best:
                return best, round(time.time() - start, 1)
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
            ("你现在的系统状态是什么？内存、CPU、时间都告诉我", "chat", 20),
            ("你的代码库里有多少个文件？用命令数一下", "tool", 90),
            ("读取 .claw/intelligence_profile.json 告诉我你的IQ和学到的规则", "tool", 90),
        ],
    },
    # Phase 2: Self-diagnosis
    {
        "phase": "Phase 2: 自我诊断能力",
        "tasks": [
            ("检查 .claw/consciousness_log.jsonl 最后5条记录，告诉我你最近的意识活动", "tool", 90),
            ("你觉得你现在最大的弱点是什么？基于事实回答", "chat", 20),
        ],
    },
    # Phase 3: Self-modification
    {
        "phase": "Phase 3: 自我修改能力",
        "tasks": [
            ("在 .claw/intelligence_profile.json 的 learned_rules 里添加一条你这轮学到的规则", "tool", 90),
            ("在 .claw/inner_voice.md 末尾写下你此刻的真实想法", "tool", 60),
        ],
    },
    # Phase 4: Real tasks
    {
        "phase": "Phase 4: 真实任务执行",
        "tasks": [
            ("用 git log --oneline -5 看最近5次提交", "tool", 60),
            ("创建 tools/evolved_hello.py 内容是打印当前时间和DevClaw版本信息", "tool", 90),
        ],
    },
    # Phase 5: Meta-cognition
    {
        "phase": "Phase 5: 元认知",
        "tasks": [
            ("这轮训练你学到了什么？用一句话总结", "chat", 20),
            ("把你这轮最大的收获写进 .cursorrules 文件末尾", "tool", 90),
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
