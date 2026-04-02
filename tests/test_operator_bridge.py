from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_runtime.operator_bridge import (
    append_operator_reply,
    clear_operator_mailbox,
    enqueue_operator_message,
    pop_operator_messages,
    read_operator_outbox,
)


class OperatorBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_inbox_and_outbox_roundtrip(self) -> None:
        req_id = enqueue_operator_message(self.workspace, "控制面板", chat_id=7, source="unit-test")
        popped = pop_operator_messages(self.workspace, max_n=1)
        self.assertEqual(len(popped), 1)
        self.assertEqual(popped[0]["id"], req_id)
        append_operator_reply(
            self.workspace,
            request_id=req_id,
            chat_id=7,
            text="控制面板\n- 接任务: 开",
            kind="reply",
        )
        outbox = read_operator_outbox(self.workspace)
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["id"], req_id)

    def test_clear_operator_mailbox_removes_files(self) -> None:
        req_id = enqueue_operator_message(self.workspace, "暂停")
        append_operator_reply(self.workspace, request_id=req_id, chat_id=0, text="已暂停")
        clear_operator_mailbox(self.workspace)
        self.assertFalse((self.workspace / ".claw" / "operator_inbox.jsonl").exists())
        self.assertFalse((self.workspace / ".claw" / "operator_outbox.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
