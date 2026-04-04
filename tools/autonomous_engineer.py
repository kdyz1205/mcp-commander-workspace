"""
Autonomous Engineer — Physical State Machine for DevClaw Evolution.

NOT a prompt trick. A real TDD-driven engineering loop:
1. Read EVOLUTION_BACKLOG.md for next task
2. Write test first (TDD)
3. Implement until test passes
4. Commit
5. Reset state, request context restart

State is stored on DISK (CURRENT_EVOLUTION_STATE.json), not in LLM context.
"""
import json
import os
import subprocess
import shutil
import sys
import time

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WS)

STATE_FILE = os.path.join(WS, "CURRENT_EVOLUTION_STATE.json")
BACKLOG_FILE = os.path.join(WS, "EVOLUTION_BACKLOG.md")


def load_state():
    try:
        return json.loads(open(STATE_FILE, encoding="utf-8").read())
    except Exception:
        return {"status": "IDLE", "task": None, "step": None, "retries": 0}


def save_state(state):
    state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    open(STATE_FILE, "w", encoding="utf-8").write(json.dumps(state, indent=2, ensure_ascii=False))


def get_next_task():
    """Read first unchecked task from backlog."""
    try:
        lines = open(BACKLOG_FILE, encoding="utf-8").readlines()
        for line in lines:
            if line.strip().startswith("- [ ]"):
                return line.strip()[6:].strip()
    except Exception:
        pass
    return None


def mark_task_done(task_desc):
    """Move task from Active to Completed."""
    try:
        content = open(BACKLOG_FILE, encoding="utf-8").read()
        content = content.replace(f"- [ ] {task_desc}", f"- [x] {task_desc}")
        # Move to completed section
        if "## Completed" in content:
            content = content.replace("## Completed\n", f"## Completed\n- [x] {task_desc}\n")
        open(BACKLOG_FILE, "w", encoding="utf-8").write(content)
    except Exception:
        pass


def mark_task_failed(task_desc, reason):
    """Move task to Failed section."""
    try:
        content = open(BACKLOG_FILE, encoding="utf-8").read()
        content = content.replace(f"- [ ] {task_desc}", "")
        if "## Failed" in content:
            content = content.replace(
                "## Failed (需人工介入)\n",
                f"## Failed (需人工介入)\n- {task_desc} | Reason: {reason}\n"
            )
        open(BACKLOG_FILE, "w", encoding="utf-8").write(content)
    except Exception:
        pass


def run_claude(prompt, timeout=300):
    """Run Claude CLI with full tool access."""
    claude_bin = shutil.which("claude") or "claude"
    try:
        r = subprocess.run(
            [claude_bin, "--dangerously-skip-permissions", "-p", prompt[:4000]],
            capture_output=True, text=True, timeout=timeout,
            cwd=WS, encoding="utf-8", errors="replace",
        )
        return r.stdout.strip() if r.returncode == 0 else f"ERROR: {r.stderr[:500]}"
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception as e:
        return f"EXCEPTION: {e}"


def run_pytest(test_file):
    """Run a specific test file."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
            capture_output=True, text=True, timeout=60,
            cwd=WS, encoding="utf-8", errors="replace",
        )
        return r.returncode == 0, (r.stdout + r.stderr)[:2000]
    except Exception as e:
        return False, str(e)


def main():
    os.system("")  # Enable ANSI on Windows
    print("=" * 60)
    print("  AUTONOMOUS ENGINEER — Physical State Machine")
    print("  TDD-driven evolution of DevClaw")
    print("=" * 60)

    while True:
        state = load_state()
        print(f"\n[State: {state['status']}]")

        if state["status"] == "IDLE":
            task = get_next_task()
            if not task:
                print("No tasks in backlog. Sleeping 60s...")
                time.sleep(60)
                continue

            print(f"\n{'='*60}")
            print(f"  CLAIMING TASK: {task[:80]}")
            print(f"{'='*60}")

            save_state({"status": "WORKING", "task": task, "step": "WRITING_TEST", "retries": 0})

            # Step 1: Write test
            print("\n[Step 1: Writing test...]")
            test_prompt = (
                f"你是DevClaw的工程师。任务：{task}\n\n"
                f"请在 tests/ 目录下创建一个 pytest 测试文件来验证这个功能。"
                f"测试应该导入相关模块并验证核心功能。文件名用 test_evolution_*.py 格式。"
                f"只写测试，不要修改业务代码。"
            )
            result = run_claude(test_prompt, timeout=120)
            print(f"  Claude: {result[:200]}")

            save_state({"status": "WORKING", "task": task, "step": "IMPLEMENTING", "retries": 0})

            # Step 2: Implement
            print("\n[Step 2: Implementing...]")
            impl_prompt = (
                f"你是DevClaw的工程师。任务：{task}\n\n"
                f"请修改 claw_runtime/ 或 tg_dev_claw.py 中的相关代码来实现这个功能。"
                f"实现后运行 tests/ 下相关的测试确保通过。"
                f"如果需要安装新依赖，用 pip install。"
            )

            for attempt in range(5):
                result = run_claude(impl_prompt, timeout=180)
                print(f"  Attempt {attempt+1}: {result[:200]}")

                # Check if any test passes
                test_files = [f for f in os.listdir(os.path.join(WS, "tests"))
                              if f.startswith("test_evolution_")]
                if test_files:
                    passed, output = run_pytest(os.path.join("tests", test_files[-1]))
                    if passed:
                        print(f"  TESTS PASSED!")
                        break
                    else:
                        print(f"  Tests failed: {output[:200]}")
                        impl_prompt = f"测试失败了：{output[:500]}\n请修复代码重试。任务：{task}"
                else:
                    print("  No test files found, skipping test check")
                    break
            else:
                print("  FAILED after 5 attempts")
                mark_task_failed(task, "5 implementation attempts failed")
                save_state({"status": "IDLE", "task": None, "step": None, "retries": 0})
                continue

            # Step 3: Commit
            print("\n[Step 3: Committing...]")
            subprocess.run(
                ["git", "add", "-A"],
                cwd=WS, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-m", f"feat(evolution): {task[:50]}"],
                cwd=WS, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=WS, capture_output=True, timeout=60,
            )

            mark_task_done(task)
            save_state({"status": "IDLE", "task": None, "step": None, "retries": 0})
            print(f"\n  TASK COMPLETE: {task[:60]}")
            print("  Committed and pushed. Moving to next task.\n")

        elif state["status"] == "WORKING":
            # Resume interrupted task
            print(f"  Resuming: {state.get('task', '?')[:60]} at step {state.get('step')}")
            # Reset to IDLE to re-claim
            save_state({"status": "IDLE", "task": None, "step": None, "retries": 0})

        time.sleep(5)


if __name__ == "__main__":
    try:
        sys.stdin = open(os.devnull, "r")
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        print("\nEngineer stopped.")
