"""Production-grade self-test for the four survival dimensions."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from claw_runtime.loopback import run_loopback_instruction
from claw_runtime.meta_driving import autonomous_tick
from claw_runtime.survival_engine import SurvivalEngine


@contextmanager
def patched_environ(updates: dict[str, str]) -> Iterator[None]:
    original: dict[str, str | None] = {k: os.environ.get(k) for k in updates}
    try:
        for key, value in updates.items():
            os.environ[key] = value
        yield
    finally:
        for key, old in original.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _ensure_selftest_seed(workspace: Path) -> None:
    (workspace / ".claw").mkdir(parents=True, exist_ok=True)
    if not (workspace / "task_plan.md").is_file():
        (workspace / "task_plan.md").write_text("# Task plan\n\nSelf-test seed.\n", encoding="utf-8")
    proxy_file = workspace / ".claw" / "selftest_proxies.txt"
    proxy_file.write_text(
        "http://127.0.0.1:8080\nhttp://127.0.0.1:8081\n",
        encoding="utf-8",
    )


def run_production_self_test(workspace: Path | str) -> dict[str, object]:
    root_workspace = Path(workspace).resolve()
    sandbox_workspace = root_workspace / ".claw" / "production_selftest_workspace"
    if sandbox_workspace.exists():
        shutil.rmtree(sandbox_workspace)
    sandbox_workspace.mkdir(parents=True, exist_ok=True)
    workspace = sandbox_workspace
    _ensure_selftest_seed(workspace)
    proxy_file = workspace / ".claw" / "selftest_proxies.txt"
    env_updates = {
        "DEVCLAW_WORKSPACE": str(workspace),
        "DEVCLAW_OFFLINE_BRAIN": "1",
        "OPENAI_API_KEY": "self-test-dummy-key",
        "META_TASK_INTENT": "Cross-file architecture migration with survival reasoning and offload planning.",
        "PROXY_LIST_FILE": str(proxy_file),
        "META_COLAB_ON_DEGRADED": "1",
        "META_PROXY_ROTATE_ON_NET_PAIN": "1",
        "META_OBSERVE_FUNDING": "0",
        "SURVIVAL_429_DEGRADED_COUNT": "1",
        "SURVIVAL_429_CRITICAL_COUNT": "99",
        "SURVIVAL_AUTONOMOUS_FUND_CHECK": "1",
        "SURVIVAL_FUND_BALANCE_USD": "1.0",
    }

    with patched_environ(env_updates):
        engine = SurvivalEngine(workspace)
        engine.heartbeat()
        engine.record_api_error("429", "self-test synthetic rate pain")
        degraded = autonomous_tick(workspace)

        engine.record_api_error("insufficient_quota", "self-test synthetic quota pain")
        critical = autonomous_tick(workspace)

        loopback = run_loopback_instruction(
            workspace,
            (
                "请做一轮 production self-test：生成自愈脚本，融合 git-commit 和 summarize，"
                "写 treasury proposal、赚钱计划、nomad snapshot，并汇报离线闭环状态。"
            ),
            max_iterations=4,
        )

    artifacts = {
        "cursor_outbox": workspace / "CURSOR_OUTBOX.md",
        "parasite_mode": workspace / ".claw" / "parasite_mode.json",
        "treasury_proposal": workspace / ".claw" / "treasury_proposal.json",
        "revenue_plan": workspace / ".claw" / "revenue_plan.json",
        "nomad_snapshot": workspace / ".claw" / "nomad_snapshot.json",
        "proxy_state": workspace / ".claw" / "proxy_rotate.json",
        "colab_bundle": workspace / ".claw" / "colab_export" / "colab_bundle.zip",
        "self_heal_ps1": workspace / "scripts" / "self_heal_rebuild_venv.ps1",
        "self_heal_sh": workspace / "scripts" / "self_heal_rebuild_venv.sh",
        "synth_skill": workspace / "skills" / "survival_hybrid" / "SKILL.md",
    }
    artifact_status = {name: path.is_file() for name, path in artifacts.items()}
    passed = all(artifact_status.values()) and loopback.success

    report: dict[str, object] = {
        "passed": passed,
        "root_workspace": str(root_workspace),
        "workspace": str(workspace),
        "degraded_tick": {
            "state": degraded.get("state"),
            "actions": degraded.get("actions"),
        },
        "critical_tick": {
            "state": critical.get("state"),
            "actions": critical.get("actions"),
        },
        "loopback": {
            "success": loopback.success,
            "message_count": len(loopback.messages),
            "messages": loopback.messages[-12:],
        },
        "artifacts": {name: str(path) for name, path in artifacts.items()},
        "artifact_status": artifact_status,
    }
    out = root_workspace / ".claw" / "production_self_test.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
