"""Production-grade self-test for the four survival dimensions."""

from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from claw_runtime.loopback import run_loopback_instruction
from claw_runtime.operator_bridge import (
    clear_operator_mailbox,
    enqueue_operator_message,
    read_operator_outbox,
    wait_for_operator_output,
)
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


def run_live_operator_self_test(workspace: Path | str, *, timeout_sec: float = 180.0) -> dict[str, object]:
    workspace = Path(workspace).resolve()
    clear_operator_mailbox(workspace)
    start_ts = time.time()

    artifact_targets = {
        "treasury_proposal": workspace / ".claw" / "treasury_proposal.json",
        "revenue_plan": workspace / ".claw" / "revenue_plan.json",
        "nomad_snapshot": workspace / ".claw" / "nomad_snapshot.json",
        "self_heal_ps1": workspace / "scripts" / "self_heal_rebuild_venv.ps1",
        "self_heal_sh": workspace / "scripts" / "self_heal_rebuild_venv.sh",
        "synth_skill": workspace / "skills" / "survival_hybrid" / "SKILL.md",
    }
    before_mtime = {
        name: (path.stat().st_mtime if path.exists() else 0.0)
        for name, path in artifact_targets.items()
    }

    panel_id = enqueue_operator_message(workspace, "控制面板", source="live-self-test")
    panel_msgs = wait_for_operator_output(workspace, panel_id, timeout_sec=20)

    pause_id = enqueue_operator_message(workspace, "暂停", source="live-self-test")
    pause_msgs = wait_for_operator_output(workspace, pause_id, timeout_sec=20)

    blocked_id = enqueue_operator_message(workspace, "检查一下仓库状态并汇报", source="live-self-test")
    blocked_msgs = wait_for_operator_output(workspace, blocked_id, timeout_sec=20)

    run_id = enqueue_operator_message(
        workspace,
        (
            "开始干活，只模拟交易，并做一轮 production self-test：生成自愈脚本，融合 git-commit 和 summarize，"
            "写 treasury proposal、赚钱计划、nomad snapshot，并汇报离线闭环状态。"
        ),
        source="live-self-test",
    )
    run_msgs = wait_for_operator_output(
        workspace,
        run_id,
        min_messages=4,
        timeout_sec=20.0,
    )
    deadline = time.time() + max(timeout_sec, 60.0)
    while time.time() < deadline:
        run_msgs = [item for item in read_operator_outbox(workspace) if str(item.get("id")) == run_id]
        if any(str(item.get("kind")) == "complete" for item in run_msgs):
            break
        time.sleep(0.5)

    after_mtime = {
        name: (path.stat().st_mtime if path.exists() else 0.0)
        for name, path in artifact_targets.items()
    }
    updated_artifacts = {
        name: after_mtime[name] > before_mtime[name]
        for name in artifact_targets
    }
    text_blob = "\n".join(str(item.get("text") or "") for item in run_msgs)

    report = {
        "passed": all(
            (
                any("控制面板" in str(item.get("text") or "") for item in panel_msgs),
                any("已暂停" in str(item.get("text") or "") for item in pause_msgs),
                any("暂停/静默" in str(item.get("text") or "") for item in blocked_msgs),
                any("simulation" in str(item.get("text") or "") for item in run_msgs),
                any("已恢复开工" in str(item.get("text") or "") for item in run_msgs),
                "离线降级脑" in text_blob,
                "treasury proposal" in text_blob,
                "revenue_plan.json" in text_blob,
                "nomad_snapshot.json" in text_blob,
                any(str(item.get("kind")) == "complete" for item in run_msgs),
                all(updated_artifacts[name] for name in ("treasury_proposal", "revenue_plan", "nomad_snapshot", "self_heal_ps1", "self_heal_sh")),
            )
        ),
        "workspace": str(workspace),
        "started_at": start_ts,
        "panel_request_id": panel_id,
        "pause_request_id": pause_id,
        "blocked_request_id": blocked_id,
        "run_request_id": run_id,
        "panel_messages": panel_msgs,
        "pause_messages": pause_msgs,
        "blocked_messages": blocked_msgs,
        "run_messages": run_msgs,
        "updated_artifacts": updated_artifacts,
    }
    out = workspace / ".claw" / "live_operator_self_test.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
