"""
TDD Test: Autonomous execution cycle — Supervisor + TDD Engine + Consolidator.

Tests the FULL loop: task claimed → code generated → test run → pass → task DONE.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_supervisor_executes_task_via_tdd():
    """Supervisor must use TDD engine to physically execute a task."""
    from core.supervisor import DevClawSupervisor

    with tempfile.TemporaryDirectory() as td:
        # Setup balance
        auth = os.path.join(td, ".auth")
        os.makedirs(auth)
        with open(os.path.join(auth, "balance.json"), "w") as f:
            json.dump({"balance": 50, "bmr": 1.0}, f)

        # Setup backlog with a simple task
        backlog = os.path.join(td, "BACKLOG.md")
        with open(backlog, "w") as f:
            f.write("## [HIGH_PRIORITY]\n- [RESEARCH] Create a utility that adds two numbers\n")

        sup = DevClawSupervisor(
            balance_path=os.path.join(auth, "balance.json"),
            backlog_path=backlog,
        )

        result = sup.run_single_cycle()
        assert result["mode"] == "RESEARCH"
        assert result["action_taken"] is not None


def test_tdd_torture_loop_succeeds():
    """Torture loop must succeed when generate_fn produces correct code."""
    from claw_runtime.tdd_engine import torture_loop

    with tempfile.TemporaryDirectory() as td:
        def good_generator(task, error_feedback):
            test = "def test_add():\n    from impl_attempt_0 import add\n    assert add(2, 3) == 5\n"
            impl = "def add(a, b):\n    return a + b\n"
            return test, impl

        result = torture_loop(td, "write add function", good_generator, max_retries=3)
        assert result["success"]
        assert result["attempts"] == 1


def test_tdd_torture_loop_fails_then_succeeds():
    """Torture loop must retry on failure and eventually succeed."""
    from claw_runtime.tdd_engine import torture_loop

    attempt_count = [0]

    def improving_generator(task, error_feedback):
        attempt_count[0] += 1
        test = f"def test_mult():\n    from impl_attempt_{attempt_count[0]-1} import mult\n    assert mult(3, 4) == 12\n"
        if attempt_count[0] <= 2:
            # First 2 attempts: wrong
            impl = "def mult(a, b):\n    return a + b\n"  # wrong!
        else:
            # 3rd attempt: correct
            impl = "def mult(a, b):\n    return a * b\n"
        return test, impl

    result = torture_loop(td_path(), "write mult function", improving_generator, max_retries=5)
    assert result["success"]
    assert result["attempts"] >= 2  # took multiple tries but succeeded


def test_backlog_marks_done_after_success():
    """After TDD success, backlog task must be marked DONE."""
    from core.backlog_manager import mark_task_done, get_all_tasks

    with tempfile.TemporaryDirectory() as td:
        backlog = os.path.join(td, "BACKLOG.md")
        with open(backlog, "w") as f:
            f.write("## [HIGH_PRIORITY]\n- [RESEARCH] Build adder utility\n")

        mark_task_done(backlog, "Build adder utility")

        tasks = get_all_tasks(backlog)
        # Should have 0 pending tasks (the one task is now DONE)
        pending = [t for t in tasks if "[DONE]" not in t.get("raw_line", "")]
        assert len(pending) == 0


def test_experience_saved_after_tdd():
    """After TDD cycle, experience must be persisted."""
    from claw_runtime.experience_consolidator import consolidate_from_tdd, load_all_rules

    with tempfile.TemporaryDirectory() as td:
        result = consolidate_from_tdd(
            td,
            failed_code="x = 1/0",
            traceback_log="ZeroDivisionError: division by zero",
            success_code="x = 1 if True else 0",
        )
        assert result["saved"]
        rules = load_all_rules(td)
        assert len(rules) >= 1


def td_path():
    """Helper to create a temp dir that persists for the test."""
    import tempfile
    return tempfile.mkdtemp()
