"""
DevClaw Execution Manager -- core execution loop for the agent framework.

Takes an ExecutionPlan produced by the planner and drives it to completion
by dispatching each PlanStep to the appropriate tool. Handles dependency
ordering, retries, timeout enforcement, artifact collection, lazy tool
initialisation, progress callbacks, and cleanup.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from claw_runtime.planner import ExecutionPlan, PlanStep

logger = logging.getLogger("devclaw.execution_manager")

# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    step_id: int
    status: str  # "success" | "failed" | "skipped" | "timed_out"
    output: Any
    error: Optional[str]
    duration_sec: float
    retries_used: int


@dataclass
class ExecutionReport:
    task_id: str
    status: str  # "completed" | "partial" | "failed" | "aborted"
    steps_completed: int
    steps_total: int
    results: list[StepResult]
    artifacts: list[str]  # paths to screenshots, logs, etc.
    started_at: float
    finished_at: float
    total_duration_sec: float


# ---------------------------------------------------------------------------
# ExecutionManager
# ---------------------------------------------------------------------------


class ExecutionManager:
    """Drives an ExecutionPlan to completion using DevClaw tools."""

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = os.path.abspath(workspace)
        self._aborted = False

        # Lazy-initialised tool instances
        self._terminal_tool = None
        self._browser_tool = None
        self._screenshot_tool = None
        self._patch_tool = None
        self._test_tool = None
        self._git_tool = None

        # Shared execution context that steps can read/write
        self._context: dict[str, Any] = {}

        # Collected artifact paths
        self._artifacts: list[str] = []

    # ------------------------------------------------------------------
    # Lazy tool accessors
    # ------------------------------------------------------------------

    def _get_terminal_tool(self):
        if self._terminal_tool is None:
            from tools.terminal_tool import TerminalTool
            self._terminal_tool = TerminalTool()
        return self._terminal_tool

    def _get_browser_tool(self):
        if self._browser_tool is None:
            from tools.browser_tool import BrowserTool
            self._browser_tool = BrowserTool()
            self._browser_tool.launch(headless=True)
        return self._browser_tool

    def _get_screenshot_tool(self):
        if self._screenshot_tool is None:
            from tools.screenshot_tool import ScreenshotTool
            self._screenshot_tool = ScreenshotTool()
        return self._screenshot_tool

    def _get_patch_tool(self):
        if self._patch_tool is None:
            from tools.patch_tool import PatchTool
            self._patch_tool = PatchTool()
        return self._patch_tool

    def _get_test_tool(self):
        if self._test_tool is None:
            from tools.test_tool import TestTool
            self._test_tool = TestTool()
        return self._test_tool

    def _get_git_tool(self):
        if self._git_tool is None:
            from tools.git_tool import GitTool
            self._git_tool = GitTool()
        return self._git_tool

    # ------------------------------------------------------------------
    # Artifact helpers
    # ------------------------------------------------------------------

    def _artifact_dir(self, task_id: str) -> str:
        p = os.path.join(self.workspace, ".claw", "artifacts", task_id)
        os.makedirs(p, exist_ok=True)
        return p

    def _collect_artifact(self, task_id: str, src_path: str) -> str:
        """Copy an artifact into the task artifact directory and record it."""
        dest_dir = self._artifact_dir(task_id)
        dest = os.path.join(dest_dir, os.path.basename(src_path))
        try:
            shutil.copy2(src_path, dest)
        except Exception:
            dest = src_path  # fallback: keep original path
        self._artifacts.append(dest)
        return dest

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_execution_order(steps: list[PlanStep]) -> list[list[PlanStep]]:
        """Return steps grouped into waves respecting dependency order.

        Each wave is a list of steps whose dependencies are all satisfied by
        earlier waves.  Steps within a wave could in principle run in parallel.
        """
        completed: set[int] = set()
        remaining = {s.step_id: s for s in steps}
        waves: list[list[PlanStep]] = []

        while remaining:
            ready = [
                s for s in remaining.values()
                if all(dep in completed for dep in s.depends_on)
            ]
            if not ready:
                # Circular or unresolvable dependency -- take everything left
                logger.warning(
                    "Unresolvable dependencies detected; forcing remaining steps"
                )
                ready = list(remaining.values())
            wave = sorted(ready, key=lambda s: s.step_id)
            waves.append(wave)
            for s in wave:
                completed.add(s.step_id)
                remaining.pop(s.step_id)
        return waves

    # ------------------------------------------------------------------
    # Top-level execute
    # ------------------------------------------------------------------

    def execute(
        self,
        plan: ExecutionPlan,
        on_step_complete: Optional[Callable[[StepResult], None]] = None,
    ) -> ExecutionReport:
        """Execute all steps in *plan* and return an ExecutionReport."""

        self._aborted = False
        self._context = {
            "workspace": self.workspace,
            "task_id": plan.task_id,
        }
        self._artifacts = []
        results: list[StepResult] = []
        started_at = time.time()
        failed_ids: set[int] = set()

        waves = self._resolve_execution_order(plan.steps)

        for wave in waves:
            if self._aborted:
                # Mark remaining steps as skipped
                for step in wave:
                    results.append(StepResult(
                        step_id=step.step_id,
                        status="skipped",
                        output=None,
                        error="Execution aborted",
                        duration_sec=0.0,
                        retries_used=0,
                    ))
                continue

            for step in wave:
                if self._aborted:
                    results.append(StepResult(
                        step_id=step.step_id,
                        status="skipped",
                        output=None,
                        error="Execution aborted",
                        duration_sec=0.0,
                        retries_used=0,
                    ))
                    continue

                # Skip if any dependency failed
                if any(dep in failed_ids for dep in step.depends_on):
                    sr = StepResult(
                        step_id=step.step_id,
                        status="skipped",
                        output=None,
                        error="Skipped due to failed dependency",
                        duration_sec=0.0,
                        retries_used=0,
                    )
                    results.append(sr)
                    failed_ids.add(step.step_id)
                    if on_step_complete:
                        on_step_complete(sr)
                    continue

                sr = self.execute_step(step, self._context)
                results.append(sr)

                if sr.status in ("failed", "timed_out"):
                    failed_ids.add(step.step_id)

                if on_step_complete:
                    on_step_complete(sr)

        finished_at = time.time()

        steps_completed = sum(1 for r in results if r.status == "success")
        steps_total = len(plan.steps)

        if self._aborted:
            overall_status = "aborted"
        elif steps_completed == steps_total:
            overall_status = "completed"
        elif steps_completed > 0:
            overall_status = "partial"
        else:
            overall_status = "failed"

        report = ExecutionReport(
            task_id=plan.task_id,
            status=overall_status,
            steps_completed=steps_completed,
            steps_total=steps_total,
            results=results,
            artifacts=list(self._artifacts),
            started_at=started_at,
            finished_at=finished_at,
            total_duration_sec=finished_at - started_at,
        )

        self.cleanup()
        return report

    # ------------------------------------------------------------------
    # Single step execution (with retries)
    # ------------------------------------------------------------------

    def execute_step(self, step: PlanStep, context: dict) -> StepResult:
        """Execute a single PlanStep, handling retries."""

        dispatch = {
            "terminal": self._run_terminal_step,
            "browser": self._run_browser_step,
            "patch": self._run_patch_step,
            "test": self._run_test_step,
            "screenshot": self._run_screenshot_step,
            "git": self._run_git_step,
            "observe": self._run_observe_step,
        }

        handler = dispatch.get(step.action)
        if handler is None:
            return StepResult(
                step_id=step.step_id,
                status="failed",
                output=None,
                error=f"Unknown action: {step.action}",
                duration_sec=0.0,
                retries_used=0,
            )

        max_attempts = (step.max_retries + 1) if step.retry_on_fail else 1
        last_result: Optional[StepResult] = None

        for attempt in range(max_attempts):
            t0 = time.time()
            try:
                result = handler(step, context)
            except Exception as exc:
                elapsed = time.time() - t0
                logger.exception(
                    "Step %d (%s) raised an exception (attempt %d/%d)",
                    step.step_id, step.action, attempt + 1, max_attempts,
                )
                result = StepResult(
                    step_id=step.step_id,
                    status="failed",
                    output=None,
                    error=str(exc),
                    duration_sec=elapsed,
                    retries_used=attempt,
                )

            last_result = result
            last_result.retries_used = attempt

            if result.status == "success":
                return result

            if attempt < max_attempts - 1:
                logger.info(
                    "Retrying step %d (%s) -- attempt %d/%d",
                    step.step_id, step.action, attempt + 2, max_attempts,
                )

        return last_result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    def _run_terminal_step(self, step: PlanStep, context: dict) -> StepResult:
        t0 = time.time()
        tool = self._get_terminal_tool()

        session_id = context.get("terminal_session_id")
        if session_id is None:
            session_id = tool.open_session(cwd=self.workspace)
            context["terminal_session_id"] = session_id

        command = step.params.get("command", "")
        timeout = step.timeout_sec

        result = tool.run(session_id, command, timeout_sec=timeout)
        elapsed = time.time() - t0

        timed_out = result.get("timed_out", False)
        exit_code = result.get("exit_code")
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")

        # Store output in context for downstream steps
        context[f"step_{step.step_id}_stdout"] = stdout
        context[f"step_{step.step_id}_stderr"] = stderr
        context[f"step_{step.step_id}_exit_code"] = exit_code

        if timed_out:
            return StepResult(
                step_id=step.step_id,
                status="timed_out",
                output={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
                error="Command timed out",
                duration_sec=elapsed,
                retries_used=0,
            )

        success = exit_code == 0
        return StepResult(
            step_id=step.step_id,
            status="success" if success else "failed",
            output={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
            error=stderr.strip() if not success else None,
            duration_sec=elapsed,
            retries_used=0,
        )

    def _run_browser_step(self, step: PlanStep, context: dict) -> StepResult:
        t0 = time.time()
        tool = self._get_browser_tool()

        page_id = context.get("browser_page_id")
        if page_id is None:
            page_id = tool.new_page()
            context["browser_page_id"] = page_id

        action = step.params.get("browser_action", "goto")
        url = step.params.get("url")
        selector = step.params.get("selector")
        text = step.params.get("text")

        try:
            if action == "goto" and url:
                result = tool.goto(page_id, url)
            elif action == "click" and selector:
                result = tool.click(page_id, selector)
            elif action == "type" and selector and text:
                result = tool.type(page_id, selector, text)
            elif action == "evaluate":
                js = step.params.get("js", "")
                result = tool.evaluate(page_id, js)
            elif action == "wait":
                wait_ms = step.params.get("wait_ms", 1000)
                time.sleep(wait_ms / 1000.0)
                result = {"waited_ms": wait_ms}
            else:
                # Generic: try calling the action as a method on the tool
                method = getattr(tool, action, None)
                if method and callable(method):
                    result = method(page_id, **{k: v for k, v in step.params.items()
                                                if k not in ("browser_action",)})
                else:
                    raise ValueError(f"Unsupported browser action: {action}")

            elapsed = time.time() - t0
            context[f"step_{step.step_id}_output"] = result
            return StepResult(
                step_id=step.step_id,
                status="success",
                output=result,
                error=None,
                duration_sec=elapsed,
                retries_used=0,
            )
        except Exception as exc:
            elapsed = time.time() - t0
            return StepResult(
                step_id=step.step_id,
                status="failed",
                output=None,
                error=str(exc),
                duration_sec=elapsed,
                retries_used=0,
            )

    def _run_patch_step(self, step: PlanStep, context: dict) -> StepResult:
        t0 = time.time()
        tool = self._get_patch_tool()

        file_path = step.params.get("file_path", "")
        # Resolve relative paths against workspace
        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace, file_path)

        try:
            # Support different patch modes
            mode = step.params.get("mode", "apply")
            if mode == "create":
                content = step.params.get("content", "")
                result = tool.create(file_path, content)
            elif mode == "replace":
                old = step.params.get("old", "")
                new = step.params.get("new", "")
                result = tool.replace(file_path, old, new)
            elif mode == "apply":
                patch = step.params.get("patch", "")
                result = tool.apply(file_path, patch)
            elif mode == "delete":
                result = tool.delete(file_path)
            else:
                raise ValueError(f"Unknown patch mode: {mode}")

            elapsed = time.time() - t0
            return StepResult(
                step_id=step.step_id,
                status="success",
                output=result,
                error=None,
                duration_sec=elapsed,
                retries_used=0,
            )
        except Exception as exc:
            elapsed = time.time() - t0
            return StepResult(
                step_id=step.step_id,
                status="failed",
                output=None,
                error=str(exc),
                duration_sec=elapsed,
                retries_used=0,
            )

    def _run_test_step(self, step: PlanStep, context: dict) -> StepResult:
        t0 = time.time()
        tool = self._get_test_tool()

        try:
            test_cmd = step.params.get("command", "")
            path = step.params.get("path", self.workspace)
            timeout = step.timeout_sec

            if hasattr(tool, "run"):
                result = tool.run(command=test_cmd, cwd=path, timeout_sec=timeout)
            elif hasattr(tool, "execute"):
                result = tool.execute(command=test_cmd, cwd=path, timeout_sec=timeout)
            else:
                # Fallback: use terminal tool to run tests
                terminal = self._get_terminal_tool()
                sid = context.get("terminal_session_id")
                if sid is None:
                    sid = terminal.open_session(cwd=self.workspace)
                    context["terminal_session_id"] = sid
                result = terminal.run(sid, test_cmd, timeout_sec=timeout)

            elapsed = time.time() - t0

            # Determine success from result
            if isinstance(result, dict):
                exit_code = result.get("exit_code", result.get("returncode"))
                passed = exit_code == 0
            else:
                passed = bool(result)

            context[f"step_{step.step_id}_output"] = result
            return StepResult(
                step_id=step.step_id,
                status="success" if passed else "failed",
                output=result,
                error=None if passed else "Tests failed",
                duration_sec=elapsed,
                retries_used=0,
            )
        except Exception as exc:
            elapsed = time.time() - t0
            return StepResult(
                step_id=step.step_id,
                status="failed",
                output=None,
                error=str(exc),
                duration_sec=elapsed,
                retries_used=0,
            )

    def _run_screenshot_step(self, step: PlanStep, context: dict) -> StepResult:
        t0 = time.time()
        tool = self._get_screenshot_tool()
        task_id = context.get("task_id", "unknown")

        try:
            source = step.params.get("source", "desktop")

            if source == "browser":
                page_id = context.get("browser_page_id")
                if page_id is None:
                    raise RuntimeError("No browser page open for screenshot")
                path = tool.capture_browser(
                    page_id=page_id,
                    full_page=step.params.get("full_page", False),
                    browser_tool=self._browser_tool,
                )
            elif source == "element":
                page_id = context.get("browser_page_id")
                selector = step.params.get("selector", "body")
                path = tool.capture_element(
                    page_id=page_id,
                    selector=selector,
                    browser_tool=self._browser_tool,
                )
            else:
                region = step.params.get("region")
                path = tool.capture_desktop(region=region)

            # Collect as artifact
            self._collect_artifact(task_id, path)

            elapsed = time.time() - t0
            context[f"step_{step.step_id}_screenshot"] = path
            return StepResult(
                step_id=step.step_id,
                status="success",
                output={"path": path},
                error=None,
                duration_sec=elapsed,
                retries_used=0,
            )
        except Exception as exc:
            elapsed = time.time() - t0
            return StepResult(
                step_id=step.step_id,
                status="failed",
                output=None,
                error=str(exc),
                duration_sec=elapsed,
                retries_used=0,
            )

    def _run_git_step(self, step: PlanStep, context: dict) -> StepResult:
        t0 = time.time()
        tool = self._get_git_tool()

        try:
            git_action = step.params.get("git_action", "status")
            params = {k: v for k, v in step.params.items() if k != "git_action"}

            method = getattr(tool, git_action, None)
            if method and callable(method):
                result = method(**params)
            else:
                # Fallback: run as terminal command
                cmd = step.params.get("command", f"git {git_action}")
                terminal = self._get_terminal_tool()
                sid = context.get("terminal_session_id")
                if sid is None:
                    sid = terminal.open_session(cwd=self.workspace)
                    context["terminal_session_id"] = sid
                result = terminal.run(sid, cmd, timeout_sec=step.timeout_sec)

            elapsed = time.time() - t0
            context[f"step_{step.step_id}_output"] = result
            return StepResult(
                step_id=step.step_id,
                status="success",
                output=result,
                error=None,
                duration_sec=elapsed,
                retries_used=0,
            )
        except Exception as exc:
            elapsed = time.time() - t0
            return StepResult(
                step_id=step.step_id,
                status="failed",
                output=None,
                error=str(exc),
                duration_sec=elapsed,
                retries_used=0,
            )

    def _run_observe_step(self, step: PlanStep, context: dict) -> StepResult:
        """Observe step: gather information without side effects.

        Reads from context, inspects files, or captures state for later use.
        """
        t0 = time.time()

        try:
            observe_type = step.params.get("observe", "context")

            if observe_type == "context":
                # Return relevant context data
                keys = step.params.get("keys", list(context.keys()))
                output = {k: context.get(k) for k in keys}
            elif observe_type == "file":
                file_path = step.params.get("file_path", "")
                if not os.path.isabs(file_path):
                    file_path = os.path.join(self.workspace, file_path)
                with open(file_path, "r", encoding="utf-8") as f:
                    output = {"path": file_path, "content": f.read()}
            elif observe_type == "directory":
                dir_path = step.params.get("dir_path", self.workspace)
                if not os.path.isabs(dir_path):
                    dir_path = os.path.join(self.workspace, dir_path)
                entries = os.listdir(dir_path)
                output = {"path": dir_path, "entries": entries}
            elif observe_type == "env":
                var_names = step.params.get("vars", [])
                output = {v: os.environ.get(v) for v in var_names}
            else:
                output = {"observe_type": observe_type, "note": "No handler for this observe type"}

            elapsed = time.time() - t0
            context[f"step_{step.step_id}_output"] = output
            return StepResult(
                step_id=step.step_id,
                status="success",
                output=output,
                error=None,
                duration_sec=elapsed,
                retries_used=0,
            )
        except Exception as exc:
            elapsed = time.time() - t0
            return StepResult(
                step_id=step.step_id,
                status="failed",
                output=None,
                error=str(exc),
                duration_sec=elapsed,
                retries_used=0,
            )

    # ------------------------------------------------------------------
    # Abort / Cleanup
    # ------------------------------------------------------------------

    def abort(self) -> None:
        """Signal the execution loop to stop after the current step."""
        logger.warning("Execution abort requested")
        self._aborted = True

    def cleanup(self) -> None:
        """Release all tool resources (terminal sessions, browser, etc.)."""
        if self._terminal_tool is not None:
            try:
                self._terminal_tool.close_all()
            except Exception:
                logger.debug("Error closing terminal sessions", exc_info=True)
            self._terminal_tool = None

        if self._browser_tool is not None:
            try:
                self._browser_tool.close()
            except Exception:
                logger.debug("Error closing browser", exc_info=True)
            self._browser_tool = None

        # Reset context
        self._context = {}
        self._screenshot_tool = None
        self._patch_tool = None
        self._test_tool = None
        self._git_tool = None
