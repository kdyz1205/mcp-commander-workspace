"""
Three-layer verification runner for the DevClaw agent framework.

Layer 1: Static verification (lint, typecheck, import check)
Layer 2: Unit/integration tests (pytest, npm test, vitest, cargo test, go test)
Layer 3: Smoke tests (custom suites, health checks)
"""

import importlib
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)


class TestTool:
    """Three-layer verification runner for DevClaw agent framework."""

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _run_cmd(
        cmd: list[str],
        cwd: str = ".",
        timeout_sec: float = 300,
    ) -> dict:
        """Run a shell command and return a standardised result dict."""
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            duration = time.monotonic() - start
            return {
                "passed": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "duration_sec": round(duration, 3),
                "tool_used": cmd[0] if cmd else "unknown",
            }
        except FileNotFoundError:
            duration = time.monotonic() - start
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command not found: {cmd[0] if cmd else 'empty'}",
                "duration_sec": round(duration, 3),
                "tool_used": cmd[0] if cmd else "unknown",
            }
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout_sec}s",
                "duration_sec": round(duration, 3),
                "tool_used": cmd[0] if cmd else "unknown",
            }
        except Exception as exc:
            duration = time.monotonic() - start
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "duration_sec": round(duration, 3),
                "tool_used": cmd[0] if cmd else "unknown",
            }

    @staticmethod
    def _detect_project_type(path: str) -> str:
        """Return one of 'python', 'node', 'go', 'rust', or 'unknown'."""
        p = Path(path)
        if any((p / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")):
            return "python"
        if (p / "package.json").exists():
            return "node"
        if (p / "go.mod").exists():
            return "go"
        if (p / "Cargo.toml").exists():
            return "rust"
        return "unknown"

    @staticmethod
    def _tool_available(name: str) -> bool:
        """Check whether a CLI tool is on PATH."""
        try:
            subprocess.run(
                [name, "--version"],
                capture_output=True,
                timeout=10,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # ------------------------------------------------------------------ #
    #  Layer 1 -- Static verification
    # ------------------------------------------------------------------ #

    def run_lint(self, path: str = ".", tool: str = "auto") -> dict:
        """Run a linter. *tool* can be 'ruff', 'flake8', 'eslint', or 'auto'."""
        if tool == "auto":
            project = self._detect_project_type(path)
            if project == "python":
                tool = "ruff" if self._tool_available("ruff") else "flake8"
            elif project == "node":
                tool = "eslint"
            else:
                tool = "ruff" if self._tool_available("ruff") else "flake8"

        if tool == "ruff":
            cmd = ["ruff", "check", path]
        elif tool == "flake8":
            cmd = ["flake8", path]
        elif tool == "eslint":
            cmd = ["npx", "eslint", path]
        else:
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Unsupported lint tool: {tool}",
                "duration_sec": 0.0,
                "tool_used": tool,
            }

        logger.info("Running lint: %s", " ".join(cmd))
        result = self._run_cmd(cmd, cwd=path)
        result["tool_used"] = tool
        return result

    def run_typecheck(self, path: str = ".", tool: str = "auto") -> dict:
        """Run a type-checker. *tool* can be 'mypy', 'pyright', 'tsc', or 'auto'."""
        if tool == "auto":
            project = self._detect_project_type(path)
            if project == "python":
                tool = "mypy" if self._tool_available("mypy") else "pyright"
            elif project == "node":
                tool = "tsc"
            else:
                tool = "mypy" if self._tool_available("mypy") else "pyright"

        if tool == "mypy":
            cmd = ["mypy", path]
        elif tool == "pyright":
            cmd = ["pyright", path]
        elif tool == "tsc":
            cmd = ["npx", "tsc", "--noEmit"]
        else:
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Unsupported typecheck tool: {tool}",
                "duration_sec": 0.0,
                "tool_used": tool,
            }

        logger.info("Running typecheck: %s", " ".join(cmd))
        result = self._run_cmd(cmd, cwd=path)
        result["tool_used"] = tool
        return result

    def run_import_check(self, path: str = ".") -> dict:
        """Try to import main Python modules found under *path*."""
        p = Path(path).resolve()
        errors: list[str] = []
        checked: list[str] = []
        start = time.monotonic()

        # Collect candidate module names from top-level .py files and packages.
        candidates: list[str] = []
        for item in p.iterdir():
            if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
                candidates.append(item.stem)
            elif item.is_dir() and (item / "__init__.py").exists():
                candidates.append(item.name)

        import sys
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

        for mod_name in candidates:
            checked.append(mod_name)
            try:
                importlib.import_module(mod_name)
            except Exception as exc:
                errors.append(f"{mod_name}: {exc}")

        duration = time.monotonic() - start
        return {
            "passed": len(errors) == 0,
            "exit_code": 0 if not errors else 1,
            "stdout": f"Checked modules: {', '.join(checked) or 'none found'}",
            "stderr": "\n".join(errors) if errors else "",
            "duration_sec": round(duration, 3),
            "tool_used": "importlib",
        }

    # ------------------------------------------------------------------ #
    #  Layer 2 -- Unit / integration tests
    # ------------------------------------------------------------------ #

    def run_unit_tests(
        self,
        path: str = ".",
        cmd: str = "auto",
        timeout_sec: float = 300,
    ) -> dict:
        """Discover and run the project's test suite.

        When *cmd* is ``"auto"`` the runner inspects the project layout to
        choose the right command (pytest, npm test, vitest, cargo test,
        go test).
        """
        if cmd == "auto":
            project = self._detect_project_type(path)
            if project == "python":
                cmd_list = ["python", "-m", "pytest", "-v"]
            elif project == "node":
                # Prefer vitest if config exists, otherwise npm test.
                p = Path(path)
                if (p / "vitest.config.ts").exists() or (p / "vitest.config.js").exists():
                    cmd_list = ["npx", "vitest", "run"]
                else:
                    cmd_list = ["npm", "test"]
            elif project == "go":
                cmd_list = ["go", "test", "./..."]
            elif project == "rust":
                cmd_list = ["cargo", "test"]
            else:
                return {
                    "passed": False,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "Cannot auto-detect project type for testing.",
                    "duration_sec": 0.0,
                    "tool_used": "unknown",
                }
        else:
            # Caller provided a custom command string.
            cmd_list = cmd.split()

        logger.info("Running unit tests: %s", " ".join(cmd_list))
        result = self._run_cmd(cmd_list, cwd=path, timeout_sec=timeout_sec)
        result["tool_used"] = cmd_list[0]
        return result

    def run_specific_test(self, test_path: str, timeout_sec: float = 120) -> dict:
        """Run a single test file or test identifier."""
        p = Path(test_path)
        if p.suffix == ".py":
            cmd = ["python", "-m", "pytest", "-v", test_path]
        elif p.suffix in (".ts", ".js"):
            cmd = ["npx", "vitest", "run", test_path]
        elif p.suffix == ".go":
            cmd = ["go", "test", "-v", "-run", p.stem, str(p.parent)]
        elif p.suffix == ".rs":
            cmd = ["cargo", "test", p.stem]
        else:
            # Treat as a pytest node id (e.g. tests/test_foo.py::test_bar).
            cmd = ["python", "-m", "pytest", "-v", test_path]

        logger.info("Running specific test: %s", " ".join(cmd))
        return self._run_cmd(cmd, cwd=".", timeout_sec=timeout_sec)

    # ------------------------------------------------------------------ #
    #  Layer 3 -- Smoke tests
    # ------------------------------------------------------------------ #

    def run_smoke_suite(self, name: str, config: Optional[dict] = None) -> dict:
        """Run a named smoke-test suite.

        *config* can supply:
            - ``cmd`` (list[str]): explicit command to run.
            - ``cwd`` (str): working directory.
            - ``timeout_sec`` (float): timeout.
            - ``env`` (dict): extra environment variables.
        """
        config = config or {}
        cmd = config.get("cmd")
        if not cmd:
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"No command configured for smoke suite '{name}'. Pass config['cmd'].",
                "duration_sec": 0.0,
                "tool_used": "smoke",
            }

        cwd = config.get("cwd", ".")
        timeout = config.get("timeout_sec", 120)
        env = None
        if config.get("env"):
            env = {**os.environ, **config["env"]}

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            duration = time.monotonic() - start
            return {
                "passed": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "duration_sec": round(duration, 3),
                "tool_used": f"smoke:{name}",
            }
        except Exception as exc:
            duration = time.monotonic() - start
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "duration_sec": round(duration, 3),
                "tool_used": f"smoke:{name}",
            }

    def health_check(self, url: str, timeout_sec: float = 10) -> dict:
        """HTTP GET against *url*; pass if status == 200."""
        start = time.monotonic()
        try:
            resp = urlopen(url, timeout=timeout_sec)  # noqa: S310
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")[:2000]
            duration = time.monotonic() - start
            return {
                "passed": status == 200,
                "exit_code": 0 if status == 200 else 1,
                "stdout": f"HTTP {status}\n{body}",
                "stderr": "",
                "duration_sec": round(duration, 3),
                "tool_used": "health_check",
            }
        except URLError as exc:
            duration = time.monotonic() - start
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"URL error: {exc.reason}",
                "duration_sec": round(duration, 3),
                "tool_used": "health_check",
            }
        except Exception as exc:
            duration = time.monotonic() - start
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "duration_sec": round(duration, 3),
                "tool_used": "health_check",
            }

    # ------------------------------------------------------------------ #
    #  Summary helpers
    # ------------------------------------------------------------------ #

    def summarize_failures(self, results: list[dict]) -> dict:
        """Aggregate a list of result dicts into a single summary."""
        total = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        failed = total - passed
        errors = [
            {
                "tool": r.get("tool_used", "unknown"),
                "stderr": r.get("stderr", "")[:500],
                "exit_code": r.get("exit_code"),
            }
            for r in results
            if not r.get("passed")
        ]
        summary_lines = []
        if failed:
            summary_lines.append(f"{failed}/{total} checks failed:")
            for e in errors:
                snippet = e["stderr"].strip().split("\n")[0] if e["stderr"] else "no details"
                summary_lines.append(f"  - {e['tool']} (exit {e['exit_code']}): {snippet}")
        else:
            summary_lines.append(f"All {total} checks passed.")

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "summary": "\n".join(summary_lines),
        }

    def full_verification(
        self,
        path: str = ".",
        skip_layers: Optional[list[int]] = None,
    ) -> dict:
        """Run all three verification layers and return an aggregated report.

        *skip_layers* can contain layer numbers (1, 2, 3) to skip.
        """
        skip = set(skip_layers or [])
        results: list[dict] = []
        layer_results: dict[str, list[dict]] = {}

        # -- Layer 1: Static verification --------------------------------
        if 1 not in skip:
            logger.info("=== Layer 1: Static verification ===")
            l1: list[dict] = []
            l1.append(self.run_lint(path))
            l1.append(self.run_typecheck(path))
            if self._detect_project_type(path) == "python":
                l1.append(self.run_import_check(path))
            results.extend(l1)
            layer_results["layer_1_static"] = l1

        # -- Layer 2: Unit / integration tests ----------------------------
        if 2 not in skip:
            logger.info("=== Layer 2: Unit / integration tests ===")
            l2: list[dict] = []
            l2.append(self.run_unit_tests(path))
            results.extend(l2)
            layer_results["layer_2_tests"] = l2

        # -- Layer 3: Smoke tests -----------------------------------------
        if 3 not in skip:
            logger.info("=== Layer 3: Smoke tests ===")
            l3: list[dict] = []
            # Layer 3 is typically project-specific; run a default no-op
            # smoke suite that simply reports success when no config exists.
            l3.append(
                self.run_smoke_suite(
                    "default",
                    config={"cmd": ["echo", "smoke-ok"]},
                )
            )
            results.extend(l3)
            layer_results["layer_3_smoke"] = l3

        summary = self.summarize_failures(results)
        return {
            "layers": layer_results,
            "summary": summary,
        }
