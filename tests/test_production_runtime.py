from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_runtime.loopback import run_loopback_instruction
from claw_runtime.self_test import run_production_self_test
from claw_runtime.ultimate.proxy_env import apply_proxy_env, current_proxy_env, rotate_proxy_index


class FakeQuotaError(Exception):
    code = "insufficient_quota"
    status_code = 429
    body = {"error": {"code": "insufficient_quota"}}


class FakeLocalError(Exception):
    pass


class FakeChatCompletions:
    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url

    def create(self, **_: object) -> object:
        if self.base_url:
            raise FakeLocalError("ollama down")
        raise FakeQuotaError("quota exhausted")


class FakeChat:
    def __init__(self, base_url: str | None) -> None:
        self.completions = FakeChatCompletions(base_url)


class FakeOpenAI:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.chat = FakeChat(base_url)


class ProductionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        (self.workspace / "skills" / "git-commit").mkdir(parents=True, exist_ok=True)
        (self.workspace / "skills" / "summarize").mkdir(parents=True, exist_ok=True)
        (self.workspace / "skills" / "git-commit" / "SKILL.md").write_text(
            "---\nname: git-commit\ndescription: test skill\n---\ncommit skill\n",
            encoding="utf-8",
        )
        (self.workspace / "skills" / "summarize" / "SKILL.md").write_text(
            "---\nname: summarize\ndescription: test skill\n---\nsummarize skill\n",
            encoding="utf-8",
        )
        (self.workspace / "task_plan.md").write_text("# Task plan\n", encoding="utf-8")
        self.proxy_file = self.workspace / ".claw" / "proxies.txt"
        self.proxy_file.parent.mkdir(parents=True, exist_ok=True)
        self.proxy_file.write_text("http://127.0.0.1:8080\nhttp://127.0.0.1:8081\n", encoding="utf-8")
        self.env_patch = patch.dict(
            os.environ,
            {
                "DEVCLAW_WORKSPACE": str(self.workspace),
                "DEVCLAW_OFFLINE_BRAIN": "1",
                "PROXY_LIST_FILE": str(self.proxy_file),
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tempdir.cleanup()

    def test_proxy_rotation_applies_process_env(self) -> None:
        rotated = rotate_proxy_index(self.workspace)
        self.assertTrue(rotated["ok"])
        env_map = current_proxy_env(self.workspace)
        self.assertEqual(env_map["HTTP_PROXY"], rotated["proxy"])
        applied = apply_proxy_env(self.workspace)
        self.assertEqual(applied["HTTPS_PROXY"], rotated["proxy"])

    def test_loopback_runs_offline_brain_without_keys(self) -> None:
        out = run_loopback_instruction(
            self.workspace,
            "请生成自愈脚本、收益计划、nomad snapshot，并融合 git-commit 和 summarize。",
            max_iterations=4,
        )
        self.assertTrue(out.success)
        self.assertTrue(any("离线降级脑" in msg for msg in out.messages))
        self.assertTrue((self.workspace / ".claw" / "revenue_plan.json").is_file())
        self.assertTrue((self.workspace / ".claw" / "nomad_snapshot.json").is_file())
        self.assertTrue((self.workspace / "skills" / "survival_hybrid" / "SKILL.md").is_file())

    def test_cloud_quota_falls_back_to_offline_brain(self) -> None:
        with patch("dev_claw.main.OpenAI", FakeOpenAI):
            os.environ["OPENAI_API_KEY"] = "test-key"
            from dev_claw.main import dev_claw_run

            messages: list[str] = []
            ok = dev_claw_run(
                "请在额度耗尽后继续完成自愈和提案。",
                max_iterations=3,
                progress_hook=messages.append,
            )
        self.assertTrue(ok)
        self.assertTrue(any("Fallback" in msg or "离线降级脑" in msg for msg in messages))
        self.assertTrue((self.workspace / "CURSOR_OUTBOX.md").is_file())
        self.assertTrue((self.workspace / ".claw" / "parasite_mode.json").is_file())

    def test_production_self_test_report_passes(self) -> None:
        report = run_production_self_test(self.workspace)
        self.assertTrue(report["passed"])
        saved = self.workspace / ".claw" / "production_self_test.json"
        self.assertTrue(saved.is_file())
        loaded = json.loads(saved.read_text(encoding="utf-8"))
        self.assertTrue(loaded["artifact_status"]["treasury_proposal"])


if __name__ == "__main__":
    unittest.main()
