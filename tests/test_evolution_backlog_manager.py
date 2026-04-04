"""
TDD Test: Backlog Manager — DevClaw's prefrontal cortex.

Prioritizes tasks by mode (RESEARCH/PROFIT) and survival urgency.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE_BACKLOG = """# DevClaw Evolution Backlog

## [HIGH_PRIORITY]
- [RESEARCH] Improve WebLLM proxy stability
- [PROFIT] Build DEX price monitor

## [NORMAL_PRIORITY]
- [RESEARCH] Optimize TDD retry logic
- [PROFIT] Add Telegram push notifications

## [LOW_PRIORITY]
- [RESEARCH] Clean up .cursorrules clustering
"""


def _write_backlog(td, content=SAMPLE_BACKLOG):
    path = os.path.join(td, "BACKLOG.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_get_next_research_task():
    """In RESEARCH mode, must return highest-priority RESEARCH task."""
    from core.backlog_manager import get_next_task

    with tempfile.TemporaryDirectory() as td:
        path = _write_backlog(td)
        task = get_next_task(path, mode="RESEARCH")
        assert task is not None
        assert "WebLLM" in task or "proxy" in task.lower()


def test_get_next_profit_task():
    """In PROFIT/SURVIVAL mode, must return highest-priority PROFIT task."""
    from core.backlog_manager import get_next_task

    with tempfile.TemporaryDirectory() as td:
        path = _write_backlog(td)
        task = get_next_task(path, mode="PROFIT")
        assert task is not None
        assert "DEX" in task or "price" in task.lower() or "monitor" in task.lower()


def test_no_matching_task():
    """If no tasks match the mode, return None."""
    from core.backlog_manager import get_next_task

    with tempfile.TemporaryDirectory() as td:
        path = _write_backlog(td, "# Empty backlog\n")
        task = get_next_task(path, mode="RESEARCH")
        assert task is None


def test_mark_task_done():
    """Completed task must be marked [DONE]."""
    from core.backlog_manager import mark_task_done

    with tempfile.TemporaryDirectory() as td:
        path = _write_backlog(td)
        mark_task_done(path, "Improve WebLLM proxy stability")
        content = open(path, encoding="utf-8").read()
        assert "[DONE]" in content
        assert "Improve WebLLM proxy stability" in content


def test_mark_done_does_not_affect_other_tasks():
    """Marking one task done must not change others."""
    from core.backlog_manager import mark_task_done

    with tempfile.TemporaryDirectory() as td:
        path = _write_backlog(td)
        mark_task_done(path, "Improve WebLLM proxy stability")
        content = open(path, encoding="utf-8").read()
        assert "- [PROFIT] Build DEX" in content  # unchanged


def test_priority_ordering():
    """HIGH_PRIORITY tasks must come before NORMAL and LOW."""
    from core.backlog_manager import get_all_tasks

    with tempfile.TemporaryDirectory() as td:
        path = _write_backlog(td)
        tasks = get_all_tasks(path)
        # First task should be from HIGH_PRIORITY section
        assert len(tasks) >= 2
        assert tasks[0]["priority"] == "HIGH"


def test_strategy_aware_selection():
    """Given TTL-based mode, must pick the right task type."""
    from core.backlog_manager import get_strategic_task

    with tempfile.TemporaryDirectory() as td:
        path = _write_backlog(td)
        # SURVIVAL → pick PROFIT first
        task = get_strategic_task(path, ttl=3.0)
        assert task is not None
        assert "PROFIT" in str(task) or "DEX" in str(task) or "price" in str(task).lower()

        # RESEARCH → pick RESEARCH first
        task = get_strategic_task(path, ttl=50.0)
        assert task is not None
