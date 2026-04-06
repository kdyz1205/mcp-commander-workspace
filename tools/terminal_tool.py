"""
DevClaw Terminal Tool — session-based terminal executor for autonomous agents.

Provides long-lived terminal sessions with PTY support on Linux, background
stdout/stderr capture, timeout-based execution, and Ctrl+C interrupt capability.
"""

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("devclaw.terminal_tool")

# ---------------------------------------------------------------------------
# PTY helpers (Linux only)
# ---------------------------------------------------------------------------

_USE_PTY = platform.system() == "Linux"

if _USE_PTY:
    import errno
    import fcntl
    import pty
    import select


def _set_nonblocking(fd: int) -> None:
    """Set a file descriptor to non-blocking mode."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


# ---------------------------------------------------------------------------
# TerminalSession
# ---------------------------------------------------------------------------

@dataclass
class TerminalSession:
    """State container for a single terminal session."""

    session_id: str
    cwd: str
    env: dict
    pid: Optional[int] = None
    stdout_buffer: list[str] = field(default_factory=list)
    stderr_buffer: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_exit_code: Optional[int] = None
    status: str = "idle"  # "idle" | "running" | "closed"

    # Internal bookkeeping — not part of the public interface.
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _master_fd: Optional[int] = field(default=None, repr=False)
    _reader_threads: list[threading.Thread] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _drain_stdout(self, source) -> None:
        """Background thread: continuously read *source* and append to stdout_buffer."""
        try:
            if isinstance(source, int):
                # PTY file-descriptor path
                self._drain_fd(source, self.stdout_buffer)
            else:
                # Pipe (file object) path
                self._drain_pipe(source, self.stdout_buffer)
        except Exception:
            logger.debug("stdout reader exiting for session %s", self.session_id)

    def _drain_stderr(self, source) -> None:
        """Background thread: continuously read *source* and append to stderr_buffer."""
        try:
            if isinstance(source, int):
                self._drain_fd(source, self.stderr_buffer)
            else:
                self._drain_pipe(source, self.stderr_buffer)
        except Exception:
            logger.debug("stderr reader exiting for session %s", self.session_id)

    def _drain_pipe(self, pipe, buffer: list[str]) -> None:
        """Read lines from a pipe until EOF or stop event."""
        for raw_line in iter(pipe.readline, b""):
            if self._stop_event.is_set():
                break
            line = raw_line.decode("utf-8", errors="replace")
            with self._lock:
                buffer.append(line)
        pipe.close()

    def _drain_fd(self, fd: int, buffer: list[str]) -> None:
        """Read from a PTY file descriptor until closed or stop event."""
        _set_nonblocking(fd)
        leftover = b""
        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
            except (ValueError, OSError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # EIO signals the slave side closed — normal on exit.
                    break
                raise
            if not chunk:
                break
            data = leftover + chunk
            # Split on newlines but keep partial last line for next iteration.
            lines = data.split(b"\n")
            leftover = lines.pop()
            for raw in lines:
                with self._lock:
                    buffer.append(raw.decode("utf-8", errors="replace") + "\n")
        # Flush any remaining partial line.
        if leftover:
            with self._lock:
                buffer.append(leftover.decode("utf-8", errors="replace"))
        try:
            os.close(fd)
        except OSError:
            pass

    def _start_readers(self, stdout_source, stderr_source) -> None:
        """Spin up daemon reader threads for stdout (and optionally stderr)."""
        self._stop_event.clear()

        t_out = threading.Thread(
            target=self._drain_stdout,
            args=(stdout_source,),
            daemon=True,
            name=f"session-{self.session_id[:8]}-stdout",
        )
        t_out.start()
        self._reader_threads.append(t_out)

        if stderr_source is not None:
            t_err = threading.Thread(
                target=self._drain_stderr,
                args=(stderr_source,),
                daemon=True,
                name=f"session-{self.session_id[:8]}-stderr",
            )
            t_err.start()
            self._reader_threads.append(t_err)

    def _stop_readers(self, join_timeout: float = 2.0) -> None:
        self._stop_event.set()
        for t in self._reader_threads:
            t.join(timeout=join_timeout)
        self._reader_threads.clear()

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def flush_buffers(self) -> tuple[str, str]:
        """Return and clear the accumulated stdout/stderr buffers."""
        with self._lock:
            stdout = "".join(self.stdout_buffer)
            stderr = "".join(self.stderr_buffer)
            self.stdout_buffer.clear()
            self.stderr_buffer.clear()
        return stdout, stderr

    def snapshot(self) -> dict:
        """Return a JSON-safe summary of the session."""
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "pid": self.pid,
            "started_at": self.started_at,
            "last_exit_code": self.last_exit_code,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# TerminalTool
# ---------------------------------------------------------------------------

class TerminalTool:
    """Manages multiple long-lived terminal sessions for DevClaw agents."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._global_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> TerminalSession:
        with self._global_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id}")
        if session.status == "closed":
            raise RuntimeError(f"Session already closed: {session_id}")
        return session

    def _kill_process(self, session: TerminalSession) -> None:
        """Best-effort process termination: SIGTERM then SIGKILL."""
        proc = session._process
        if proc is None or proc.poll() is not None:
            return
        pid = proc.pid
        logger.info("Terminating process %d in session %s", pid, session.session_id)
        try:
            # Send to entire process group so child processes are also killed.
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning("SIGTERM did not stop PID %d — sending SIGKILL", pid)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.error("Failed to kill PID %d", pid)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_session(
        self, cwd: str = ".", env: Optional[dict] = None
    ) -> str:
        """Create a new terminal session and return its session_id."""
        resolved_cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(resolved_cwd):
            raise FileNotFoundError(f"Working directory does not exist: {resolved_cwd}")

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        session = TerminalSession(
            session_id=str(uuid.uuid4()),
            cwd=resolved_cwd,
            env=merged_env,
        )

        with self._global_lock:
            self._sessions[session.session_id] = session

        logger.info(
            "Opened session %s (cwd=%s)", session.session_id, resolved_cwd
        )
        return session.session_id

    def run(
        self,
        session_id: str,
        command: str,
        timeout_sec: float = 120,
    ) -> dict:
        """
        Execute *command* in the given session.

        Returns ``{"exit_code": int|None, "stdout": str, "stderr": str, "timed_out": bool}``.
        """
        session = self._get_session(session_id)

        if session.status == "running":
            raise RuntimeError(
                f"Session {session_id} already has a running command. "
                "Send Ctrl+C or wait for it to finish."
            )

        session.status = "running"
        # Clear buffers for the new command.
        with session._lock:
            session.stdout_buffer.clear()
            session.stderr_buffer.clear()

        timed_out = False

        try:
            if _USE_PTY:
                exit_code, timed_out = self._run_pty(session, command, timeout_sec)
            else:
                exit_code, timed_out = self._run_pipe(session, command, timeout_sec)
        except Exception:
            logger.exception("Error running command in session %s", session_id)
            session.status = "idle"
            raise

        session.last_exit_code = exit_code
        session.status = "idle"

        stdout, stderr = session.flush_buffers()

        result = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
        }
        logger.debug(
            "Command finished in session %s: exit_code=%s timed_out=%s",
            session_id,
            exit_code,
            timed_out,
        )
        return result

    # ------------------------------------------------------------------
    # Execution back-ends
    # ------------------------------------------------------------------

    def _run_pty(
        self, session: TerminalSession, command: str, timeout_sec: float
    ) -> tuple[Optional[int], bool]:
        """Run via PTY (Linux). Returns (exit_code, timed_out)."""
        master_fd, slave_fd = pty.openpty()
        timed_out = False

        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-c", command],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=session.cwd,
                env=session.env,
                start_new_session=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        finally:
            # The parent doesn't need the slave side.
            try:
                os.close(slave_fd)
            except OSError:
                pass

        session._process = proc
        session._master_fd = master_fd
        session.pid = proc.pid

        # In PTY mode, stdout and stderr are merged onto master_fd.
        session._start_readers(stdout_source=master_fd, stderr_source=None)

        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning(
                "Command timed out after %.1fs in session %s",
                timeout_sec,
                session.session_id,
            )
            self._kill_process(session)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        # Give readers a moment to drain remaining output.
        session._stop_readers(join_timeout=2.0)

        # Update cwd: read /proc/<pid>/cwd if possible (best-effort).
        self._update_cwd(session)

        exit_code = proc.returncode
        session._process = None
        session._master_fd = None
        session.pid = None
        return exit_code, timed_out

    def _run_pipe(
        self, session: TerminalSession, command: str, timeout_sec: float
    ) -> tuple[Optional[int], bool]:
        """Run via plain pipes (non-Linux fallback). Returns (exit_code, timed_out)."""
        timed_out = False

        proc = subprocess.Popen(
            ["/bin/bash", "-c", command] if os.name != "nt" else ["cmd", "/c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=session.cwd,
            env=session.env,
            start_new_session=True,
        )

        session._process = proc
        session.pid = proc.pid

        session._start_readers(
            stdout_source=proc.stdout, stderr_source=proc.stderr
        )

        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning(
                "Command timed out after %.1fs in session %s",
                timeout_sec,
                session.session_id,
            )
            self._kill_process(session)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        session._stop_readers(join_timeout=2.0)
        self._update_cwd(session)

        exit_code = proc.returncode
        session._process = None
        session.pid = None
        return exit_code, timed_out

    # ------------------------------------------------------------------
    # CWD tracking
    # ------------------------------------------------------------------

    @staticmethod
    def _update_cwd(session: TerminalSession) -> None:
        """Try to read the child's final working directory from /proc (Linux)."""
        if session.pid is None:
            return
        proc_link = f"/proc/{session.pid}/cwd"
        try:
            real = os.readlink(proc_link)
            if os.path.isdir(real):
                session.cwd = real
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Output reading
    # ------------------------------------------------------------------

    def read_output(self, session_id: str) -> dict:
        """Return and clear buffered stdout/stderr for the session."""
        session = self._get_session(session_id)
        stdout, stderr = session.flush_buffers()
        return {"stdout": stdout, "stderr": stderr}

    # ------------------------------------------------------------------
    # Ctrl+C
    # ------------------------------------------------------------------

    def send_ctrl_c(self, session_id: str) -> bool:
        """
        Send SIGINT to the running process in the given session.

        Returns True if signal was sent, False if no running process.
        """
        session = self._get_session(session_id)
        proc = session._process
        if proc is None or proc.poll() is not None:
            logger.debug(
                "No running process to interrupt in session %s", session_id
            )
            return False

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            logger.info("Sent SIGINT to PID %d (session %s)", proc.pid, session_id)
            return True
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.warning("Failed to send SIGINT in session %s: %s", session_id, exc)
            return False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def close_session(self, session_id: str) -> bool:
        """
        Close a session, killing any running process and freeing resources.

        Returns True if the session existed and was closed.
        """
        with self._global_lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            logger.debug("close_session called for unknown session %s", session_id)
            return False

        # Kill any lingering process.
        self._kill_process(session)
        session._stop_readers(join_timeout=2.0)

        # Close master fd if still open.
        if session._master_fd is not None:
            try:
                os.close(session._master_fd)
            except OSError:
                pass
            session._master_fd = None

        session.status = "closed"
        session._process = None
        session.pid = None
        logger.info("Closed session %s", session_id)
        return True

    def list_sessions(self) -> list[dict]:
        """Return a snapshot list of all active (non-closed) sessions."""
        with self._global_lock:
            sessions = list(self._sessions.values())
        return [s.snapshot() for s in sessions]

    # ------------------------------------------------------------------
    # Context manager / cleanup
    # ------------------------------------------------------------------

    def close_all(self) -> None:
        """Close every open session. Intended for graceful shutdown."""
        with self._global_lock:
            ids = list(self._sessions.keys())
        for sid in ids:
            self.close_session(sid)
        logger.info("All sessions closed.")

    def __del__(self) -> None:
        try:
            self.close_all()
        except Exception:
            pass
