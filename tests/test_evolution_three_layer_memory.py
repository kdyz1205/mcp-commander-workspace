"""
TDD Test: Three-Layer External Memory System.

Layer 1: Handover Note — task-level context survives restart
Layer 2: Code as Truth — git diff + traceback as memory source
Layer 3: Rule Embeddings — lessons persist in .cursorrules
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════
# Layer 1: Handover Note
# ═══════════════════════════════════════

def test_handover_save_and_load():
    """Handover memory must persist to disk and be loadable."""
    from claw_runtime.three_layer_memory import save_handover, load_handover

    with tempfile.TemporaryDirectory() as td:
        save_handover(td, task_id="FIX_DB", step="PATCHING",
                      memory="不要在process_message里加await lock，直接改db_handler.py")

        h = load_handover(td)
        assert h["task_id"] == "FIX_DB"
        assert h["step"] == "PATCHING"
        assert "db_handler" in h["memory"]


def test_handover_survives_state_reset():
    """After resetting status to IDLE, handover memory is still readable."""
    from claw_runtime.three_layer_memory import save_handover, load_handover, clear_handover

    with tempfile.TemporaryDirectory() as td:
        save_handover(td, task_id="T1", step="DONE", memory="这个任务的关键发现是X")
        clear_handover(td)  # Simulate task completion

        h = load_handover(td)
        assert h["status"] == "IDLE"
        # But the last_memory should still be accessible
        assert "关键发现" in h.get("last_memory", "")


# ═══════════════════════════════════════
# Layer 2: Code as Truth
# ═══════════════════════════════════════

def test_get_physical_context():
    """Physical context must return real git diff and file slices."""
    from claw_runtime.three_layer_memory import get_physical_context

    ws = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ctx = get_physical_context(ws)

    assert "git_diff" in ctx  # may be empty if no changes
    assert "recent_commits" in ctx
    assert isinstance(ctx["recent_commits"], str)
    assert len(ctx["recent_commits"]) > 0  # at least some commits exist


def test_extract_error_context():
    """Error context extraction must parse traceback to file+line."""
    from claw_runtime.three_layer_memory import extract_error_context

    traceback_text = '''Traceback (most recent call last):
  File "claw_runtime/foo.py", line 42, in bar
    x = d["missing_key"]
KeyError: 'missing_key'
'''
    ctx = extract_error_context(traceback_text, os.getcwd())
    assert ctx["error_type"] == "KeyError"
    assert ctx["file"] == "claw_runtime/foo.py"
    assert ctx["line"] == 42


# ═══════════════════════════════════════
# Layer 3: Rule Embeddings
# ═══════════════════════════════════════

def test_add_rule_to_cursorrules():
    """Rules must be appendable to .cursorrules without duplicates."""
    from claw_runtime.three_layer_memory import add_learned_rule, load_learned_rules

    with tempfile.TemporaryDirectory() as td:
        cr = os.path.join(td, ".cursorrules")
        open(cr, "w").write("# Existing rules\n")

        add_learned_rule(td, "RULE_99: Always check OS before using asyncio")
        add_learned_rule(td, "RULE_100: Never trust LLM output without verification")
        add_learned_rule(td, "RULE_99: Always check OS before using asyncio")  # duplicate

        rules = load_learned_rules(td)
        # Should have 2 unique rules, not 3
        assert len([r for r in rules if "RULE_99" in r]) == 1
        assert len([r for r in rules if "RULE_100" in r]) == 1


def test_build_restart_prompt():
    """Restart prompt must combine all 3 layers into a dense context."""
    from claw_runtime.three_layer_memory import build_restart_prompt

    with tempfile.TemporaryDirectory() as td:
        # Setup minimal state
        cr = os.path.join(td, ".cursorrules")
        open(cr, "w").write("# Rules\n- RULE_1: test rule\n")

        from claw_runtime.three_layer_memory import save_handover
        save_handover(td, task_id="FIX_X", step="TESTING", memory="改了foo.py第42行")

        prompt = build_restart_prompt(td)
        assert "FIX_X" in prompt  # Layer 1: handover
        assert "RULE_1" in prompt  # Layer 3: rules
        assert len(prompt) < 3000  # Must be compact
