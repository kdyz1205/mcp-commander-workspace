from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_runtime.meta_driving import ReasoningOptimizer
from claw_runtime.nightly_evolution import materialize_draft_skill
from claw_runtime.survival_engine import SurvivalEngine
from claw_runtime.ultimate.nomad import restore_nomad_identity, write_nomad_snapshot
from claw_runtime.ultimate.skill_synthesis import synthesize_two_skills
from claw_runtime.ultimate.treasury import prepare_self_funding_review


class ReasoningLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        (self.workspace / ".claw").mkdir(parents=True, exist_ok=True)
        (self.workspace / "task_plan.md").write_text("# Task plan\n\n[现状] Seed.\n", encoding="utf-8")
        for name in ("git-commit", "summarize"):
            skill_dir = self.workspace / "skills" / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: test\n---\nbody for {name}\n",
                encoding="utf-8",
            )
        self.env_patch = patch.dict(
            os.environ,
            {
                "DEVCLAW_WORKSPACE": str(self.workspace),
                "OPENAI_API_KEY": "dummy",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tempdir.cleanup()

    def test_reasoning_optimizer_prefers_local_for_simple_task(self) -> None:
        engine = SurvivalEngine(self.workspace)
        snapshot = engine.snapshot()
        optimizer = ReasoningOptimizer(self.workspace, engine)
        decision = optimizer.decide(task_intent="format a single file and tidy docs", snapshot=snapshot)
        self.assertEqual(decision.lane, "local_parasite")
        self.assertEqual(decision.complexity, "simple")

    def test_reasoning_optimizer_offloads_degraded_complex_work(self) -> None:
        engine = SurvivalEngine(self.workspace)
        optimizer = ReasoningOptimizer(self.workspace, engine)
        snapshot = {
            "state": "DEGRADED",
            "quota": {
                "openai_key_configured": True,
                "insufficient_quota_events_24h": 0,
                "rate_like_events_1h": 0,
                "soft_budget_exhausted": False,
            },
            "vitals": {"mem_percent": 91.0},
        }
        decision = optimizer.decide(
            task_intent="cross-file architecture refactor for a multi-agent integration",
            snapshot=snapshot,
        )
        self.assertEqual(decision.lane, "degraded_offload")
        self.assertTrue(decision.should_offload)

    def test_materialize_draft_skill_writes_rationale_and_tests(self) -> None:
        evo = self.workspace / ".claw" / "evolution_failures.jsonl"
        evo.write_text(
            json.dumps({"kind": "quota", "detail": "insufficient_quota while evolving"}) + "\n"
            + json.dumps({"kind": "network", "detail": "proxy timeout on fetch"}) + "\n",
            encoding="utf-8",
        )
        out = materialize_draft_skill(self.workspace)
        self.assertIsNotNone(out)
        assert out is not None
        skill_dir = out.parent
        self.assertTrue((skill_dir / "RATIONALE.md").is_file())
        self.assertTrue((skill_dir / "pytest_gate.json").is_file())
        tests = list((skill_dir / "tests").glob("test_*.py"))
        self.assertTrue(tests)

    def test_nomad_restore_writes_environment_context(self) -> None:
        write_nomad_snapshot(self.workspace, extra={"case": "restore"})
        payload = restore_nomad_identity(self.workspace)
        self.assertIn("system_append", payload)
        self.assertTrue((self.workspace / ".claw" / "nomad_environment.json").is_file())

    def test_self_funding_review_stays_research_only(self) -> None:
        os.environ["TREASURY_ALLOW_NETWORK_RESEARCH"] = "0"
        path = prepare_self_funding_review(self.workspace, reason="quota pressure")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "research_only")
        self.assertFalse(data["policy"]["live_orders_allowed"])

    def test_skill_synthesis_writes_rationale_and_pytest_scaffold(self) -> None:
        out, _ = synthesize_two_skills(
            self.workspace,
            "git-commit",
            "summarize",
            out_skill_name="logic_merge",
            rationale="Combine commit discipline with summarization for autonomous reviews.",
        )
        skill_dir = out.parent
        self.assertTrue((skill_dir / "RATIONALE.md").is_file())
        self.assertTrue(list((skill_dir / "tests").glob("test_*.py")))


if __name__ == "__main__":
    unittest.main()
