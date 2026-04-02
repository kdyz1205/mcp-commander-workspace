"""Loopback execution for self-tests and local message simulation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class LoopbackTranscript:
    instruction: str
    messages: list[str]
    success: bool


def run_loopback_instruction(
    workspace: Path | str,
    instruction: str,
    *,
    max_iterations: int = 8,
    system_append: str | None = None,
) -> LoopbackTranscript:
    from dev_claw.main import dev_claw_run

    workspace = Path(workspace).resolve()
    messages: list[str] = []
    success = dev_claw_run(
        instruction,
        max_iterations=max_iterations,
        system_append=system_append,
        progress_hook=messages.append,
    )
    return LoopbackTranscript(
        instruction=instruction,
        messages=messages,
        success=bool(success),
    )

