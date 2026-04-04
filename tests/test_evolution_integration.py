"""
TDD Test: Full integration — all 11 organs working together.

Tests the REAL autonomous evolution cycle:
1. Supervisor reads TTL → picks strategy
2. Backlog Manager picks task based on strategy
3. Knowledge Forager digests documentation
4. TDD Engine validates code
5. Experience Consolidator saves lessons
6. Three-layer memory enables restart
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_full_cycle_research_mode():
    """In RESEARCH mode (TTL>30), supervisor picks a RESEARCH task from backlog."""
    from core.supervisor import DevClawSupervisor
    from core.backlog_manager import get_strategic_task

    with tempfile.TemporaryDirectory() as td:
        # Setup balance (TTL=50 → RESEARCH)
        auth = os.path.join(td, ".auth")
        os.makedirs(auth)
        with open(os.path.join(auth, "balance.json"), "w") as f:
            json.dump({"balance": 50, "bmr": 1.0}, f)

        # Setup backlog
        backlog = os.path.join(td, "BACKLOG.md")
        with open(backlog, "w") as f:
            f.write("""## [HIGH_PRIORITY]
- [RESEARCH] Improve WebLLM stability
- [PROFIT] Build price monitor
""")

        # Supervisor decides RESEARCH mode
        sup = DevClawSupervisor.__new__(DevClawSupervisor)
        mode, _ = sup.decide_strategy(50.0)
        assert mode == "RESEARCH"

        # Backlog returns RESEARCH task
        task = get_strategic_task(backlog, ttl=50.0)
        assert "WebLLM" in task or "stability" in task.lower()


def test_full_cycle_survival_mode():
    """In SURVIVAL mode (TTL<7), supervisor picks PROFIT task."""
    from core.supervisor import DevClawSupervisor
    from core.backlog_manager import get_strategic_task

    with tempfile.TemporaryDirectory() as td:
        backlog = os.path.join(td, "BACKLOG.md")
        with open(backlog, "w") as f:
            f.write("""## [HIGH_PRIORITY]
- [RESEARCH] Improve WebLLM stability
- [PROFIT] Build price monitor
""")

        sup = DevClawSupervisor.__new__(DevClawSupervisor)
        mode, _ = sup.decide_strategy(3.0)
        assert mode == "SURVIVAL"

        task = get_strategic_task(backlog, ttl=3.0)
        assert "price" in task.lower() or "monitor" in task.lower()


def test_tdd_engine_with_experience_consolidator():
    """After TDD success, experience consolidator must extract rule."""
    from claw_runtime.tdd_engine import run_tdd_cycle
    from claw_runtime.experience_consolidator import consolidate_from_tdd

    with tempfile.TemporaryDirectory() as td:
        failed = "def add(a, b):\n    return a - b  # wrong\n"
        traceback = "AssertionError: assert -1 == 5"
        success = "def add(a, b):\n    return a + b\n"

        test = "def test_add():\n    from impl import add\n    assert add(2, 3) == 5\n"

        # TDD cycle passes with correct code
        result = run_tdd_cycle(td, test, success, impl_filename="impl.py")
        assert result["success"]

        # Consolidator extracts lesson
        lesson = consolidate_from_tdd(td, failed, traceback, success)
        assert lesson["saved"]
        assert lesson["total_rules"] >= 1


def test_knowledge_forager_feeds_tdd():
    """Knowledge capsule can be used as context for TDD generation."""
    from claw_runtime.knowledge_forager import build_knowledge_capsule

    html = """<html><body><main>
    <h2>API Guide</h2>
    <pre><code>import requests
response = requests.get("https://api.example.com/data")
print(response.json())</code></pre>
    </main></body></html>"""

    capsule = build_knowledge_capsule(html, ["api", "requests"])
    assert len(capsule["code_snippets"]) >= 1
    assert "requests" in capsule["code_snippets"][0]


def test_three_layer_restart_prompt():
    """Restart prompt combines handover + rules into compact context."""
    from claw_runtime.three_layer_memory import save_handover, build_restart_prompt
    from claw_runtime.experience_consolidator import persist_rule

    with tempfile.TemporaryDirectory() as td:
        save_handover(td, "FIX_BUG", "TESTING", "Changed line 42 in foo.py")
        persist_rule(td, {
            "trigger_condition": "When using aiohttp",
            "forbidden_action": "Never skip SSL",
            "enforced_action": "Always use ssl context",
        })

        prompt = build_restart_prompt(td)
        assert "FIX_BUG" in prompt
        assert len(prompt) < 3000


def test_meta_creator_forges_and_registers():
    """Meta creator can forge a new skill and register it."""
    from claw_runtime.meta_creator import forge_skill

    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "skills"), exist_ok=True)

        result = forge_skill(td, "sk_adder", '''
"""Add numbers."""

def execute(**kwargs):
    return {"sum": kwargs.get("a", 0) + kwargs.get("b", 0)}
''', "Adds two numbers")
        assert result["success"]

        # Verify it's loadable
        from claw_runtime.meta_creator import load_skill_module
        mod = load_skill_module(result["skill_path"], "sk_adder")
        assert mod.execute(a=5, b=3)["sum"] == 8
