from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_runtime.runtime_control import (
    ensure_runtime_control,
    interpret_control_message,
    load_runtime_control,
    panel_summary,
)


class RuntimeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.env_patch = patch.dict(
            "os.environ",
            {
                "DEVCLAW_WORKSPACE": str(self.workspace),
                "TG_IDLE_AUTOTICK": "1",
                "TG_AUTONOMOUS_LOGIC_CHAIN": "1",
                "TG_AUTONOMOUS_LIFE": "",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tempdir.cleanup()

    def test_default_panel_is_simulation_only(self) -> None:
        state = ensure_runtime_control(self.workspace)
        self.assertEqual(state.trading_mode, "simulation")
        self.assertFalse(state.live_trading_enabled)
        self.assertTrue((self.workspace / ".claw" / "runtime_control.json").is_file())
        self.assertIn("交易模式: simulation", panel_summary(state))

    def test_start_and_simulation_message_can_queue_residual_work(self) -> None:
        outcome = interpret_control_message(
            self.workspace,
            "开始干活，先别用真钱交易，检查一下仓库状态并汇报。",
            actor="test",
        )
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertTrue(outcome.state.accepting_tasks)
        self.assertEqual(outcome.state.trading_mode, "simulation")
        self.assertFalse(outcome.state.live_trading_enabled)
        self.assertIsNotNone(outcome.queue_instruction)
        self.assertIn("检查", outcome.queue_instruction or "")

    def test_pause_message_stops_intake_and_background(self) -> None:
        outcome = interpret_control_message(self.workspace, "先暂停，保持安静。", actor="test")
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertFalse(outcome.state.accepting_tasks)
        self.assertFalse(outcome.state.background_master_enabled)
        self.assertIsNone(outcome.queue_instruction)

    def test_manual_only_turns_off_autonomy_but_keeps_manual_tasks(self) -> None:
        outcome = interpret_control_message(self.workspace, "只在我叫你时工作，别自主巡航。", actor="test")
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertTrue(outcome.state.accepting_tasks)
        self.assertFalse(outcome.state.background_master_enabled)

    def test_status_message_returns_current_panel(self) -> None:
        interpret_control_message(self.workspace, "暂停", actor="test")
        outcome = interpret_control_message(self.workspace, "控制面板", actor="test")
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertIn("控制面板", outcome.reply)
        state = load_runtime_control(self.workspace)
        self.assertFalse(state.accepting_tasks)


if __name__ == "__main__":
    unittest.main()
