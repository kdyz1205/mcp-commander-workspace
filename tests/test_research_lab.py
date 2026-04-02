from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from claw_runtime.offline_brain import run_offline_brain
from claw_runtime.research_lab import _request_text, extract_factor_hypotheses, run_public_research_scout


_ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://arxiv.org/abs/2601.00001</id>
    <published>2026-01-02T00:00:00Z</published>
    <title>Deep Representation Learning for Market Microstructure Signals</title>
    <summary>We study order book liquidity, order flow imbalance, and latent regime representation for trading systems.</summary>
  </entry>
  <entry>
    <id>https://arxiv.org/abs/2602.00002</id>
    <published>2026-02-18T00:00:00Z</published>
    <title>Multimodal Language Models for Financial News Sentiment</title>
    <summary>This paper models news sentiment, volatility, and cross-asset correlation for systematic trading research.</summary>
  </entry>
</feed>
"""
_ZH_RESEARCH_PROMPT = "\u5f00\u59cb\u5b66\u4e602026\u7684\u6df1\u5ea6\u5b66\u4e60\u8bba\u6587\u5e76\u6293\u53d6\u4ea4\u6613\u56e0\u5b50"


class ResearchLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        (self.workspace / "task_plan.md").write_text("# Task plan\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_public_research_scout_writes_report(self) -> None:
        with patch("claw_runtime.research_lab._request_text", return_value=_ATOM_SAMPLE):
            result = run_public_research_scout(
                self.workspace,
                _ZH_RESEARCH_PROMPT,
            )
        self.assertTrue(result.markdown_path.is_file())
        self.assertTrue(result.json_path.is_file())
        data = json.loads(result.json_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["papers"]), 2)
        self.assertTrue(any(factor["name"] == "microstructure_liquidity" for factor in data["factors"]))

    def test_factor_extraction_detects_signal_families(self) -> None:
        with patch("claw_runtime.research_lab._request_text", return_value=_ATOM_SAMPLE):
            result = run_public_research_scout(self.workspace, "research factors")
        factors = extract_factor_hypotheses(result.papers)
        names = {factor.name for factor in factors}
        self.assertIn("microstructure_liquidity", names)
        self.assertIn("news_sentiment_alpha", names)

    def test_request_text_falls_back_to_direct_when_proxy_fails(self) -> None:
        calls: list[dict[str, str]] = []

        def fake_get(self, url, headers=None, timeout=25.0, proxies=None):
            calls.append(proxies or {})
            if proxies:
                raise requests.exceptions.ProxyError("proxy down")

            class FakeResponse:
                text = "ok"

                def raise_for_status(self):
                    return None

            return FakeResponse()

        with patch("claw_runtime.research_lab.current_proxy_env", return_value={"HTTP_PROXY": "http://127.0.0.1:8080"}):
            with patch("requests.Session.get", new=fake_get):
                payload = _request_text("https://example.com", self.workspace)
        self.assertEqual(payload, "ok")
        self.assertEqual(calls, [{"http": "http://127.0.0.1:8080"}, {}])

    def test_offline_brain_runs_research_action(self) -> None:
        with patch("claw_runtime.research_lab._request_text", return_value=_ATOM_SAMPLE):
            result = run_offline_brain(
                self.workspace,
                _ZH_RESEARCH_PROMPT,
                failure_reason="quota exhausted",
            )
        action_names = {item["name"] for item in result.actions}
        self.assertIn("research_scout", action_names)
        self.assertTrue((self.workspace / ".claw" / "research" / "latest_research_factor_report.md").is_file())


if __name__ == "__main__":
    unittest.main()
