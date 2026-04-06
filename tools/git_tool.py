"""
Git operations wrapper for the DevClaw agent framework.

All operations are performed via ``subprocess.run(["git", ...])``.
Includes logging, error handling, and edge-case coverage (not a repo,
merge conflicts, dirty worktree, etc.).
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    """Raised when a git command fails unexpectedly."""

    def __init__(self, message: str, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class GitTool:
    """Convenience wrapper around git CLI commands."""

    def __init__(self, repo_path: str = "."):
        self.repo_path = str(Path(repo_path).resolve())
        self._ensure_repo()

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess:
        """Run ``git <args>`` inside the repo and return the result."""
        cmd = ["git", "-C", self.repo_path] + args
        logger.debug("git command: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git command timed out after {timeout}s: {' '.join(args)}"
            ) from exc

        if check and proc.returncode != 0:
            msg = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
            raise GitError(
                f"git {' '.join(args)} failed (exit {proc.returncode}): {msg}",
                returncode=proc.returncode,
                stderr=proc.stderr,
            )
        return proc

    def _ensure_repo(self) -> None:
        """Verify that *repo_path* is inside a git repository."""
        proc = self._run(["rev-parse", "--is-inside-work-tree"], check=False)
        if proc.returncode != 0 or proc.stdout.strip() != "true":
            raise GitError(
                f"Not a git repository: {self.repo_path}",
                returncode=proc.returncode,
                stderr=proc.stderr,
            )

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def status(self) -> dict:
        """Return structured working-tree status.

        Keys: branch, modified, untracked, staged, deleted, ahead, behind.
        """
        branch = self.branch_current()

        proc = self._run(["status", "--porcelain=v1", "-b"])
        lines = proc.stdout.strip().splitlines()

        modified: list[str] = []
        untracked: list[str] = []
        staged: list[str] = []
        deleted: list[str] = []

        ahead = 0
        behind = 0

        for line in lines:
            if line.startswith("##"):
                m = re.search(r"\[ahead (\d+)", line)
                if m:
                    ahead = int(m.group(1))
                m = re.search(r"behind (\d+)", line)
                if m:
                    behind = int(m.group(1))
                continue

            if len(line) < 4:
                continue

            index_status = line[0]
            worktree_status = line[1]
            filepath = line[3:]

            # Staged changes (index column)
            if index_status in ("A", "M", "R", "C", "D"):
                staged.append(filepath)

            # Worktree changes
            if worktree_status == "M":
                modified.append(filepath)
            elif worktree_status == "D":
                deleted.append(filepath)
            elif worktree_status == "?" and index_status == "?":
                untracked.append(filepath)

        return {
            "branch": branch,
            "modified": modified,
            "untracked": untracked,
            "staged": staged,
            "deleted": deleted,
            "ahead": ahead,
            "behind": behind,
        }

    def diff(self, staged: bool = False, file: Optional[str] = None) -> str:
        """Return diff output as a string."""
        args = ["diff"]
        if staged:
            args.append("--cached")
        if file:
            args.extend(["--", file])
        proc = self._run(args)
        return proc.stdout

    def log(self, n: int = 10, oneline: bool = True) -> list[dict]:
        """Return the last *n* commits.

        Each entry: ``{hash, message, author, date}``.
        """
        fmt = "%H%x00%s%x00%an%x00%aI"
        args = ["log", f"-{n}", f"--format={fmt}"]
        proc = self._run(args, check=False)
        if proc.returncode != 0:
            # Likely an empty repo with no commits.
            return []

        entries: list[dict] = []
        for line in proc.stdout.strip().splitlines():
            parts = line.split("\x00")
            if len(parts) < 4:
                continue
            entries.append({
                "hash": parts[0],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
        return entries

    def add(self, files: Optional[list[str]] = None) -> dict:
        """Stage files. If *files* is None, stage all modified/deleted files."""
        if files:
            self._run(["add", "--"] + files)
        else:
            self._run(["add", "-u"])
        return {"staged": files or ["(all modified)"]}

    def commit(self, message: str) -> dict:
        """Create a commit with the given *message*.

        Returns ``{hash, message}``.
        """
        self._run(["commit", "-m", message])
        proc = self._run(["rev-parse", "HEAD"])
        commit_hash = proc.stdout.strip()
        return {"hash": commit_hash, "message": message}

    def branch_current(self) -> str:
        """Return the name of the current branch (or HEAD hash if detached)."""
        proc = self._run(["symbolic-ref", "--short", "HEAD"], check=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        # Detached HEAD -- return short SHA.
        proc = self._run(["rev-parse", "--short", "HEAD"], check=False)
        return proc.stdout.strip() or "HEAD"

    def branch_create(self, name: str, checkout: bool = True) -> dict:
        """Create a new branch and optionally check it out."""
        if checkout:
            self._run(["checkout", "-b", name])
        else:
            self._run(["branch", name])
        return {"branch": name, "checked_out": checkout}

    def checkout(self, ref: str) -> dict:
        """Checkout a branch, tag, or commit."""
        self._run(["checkout", ref])
        return {"ref": ref}

    def stash(self, message: Optional[str] = None) -> dict:
        """Stash current changes."""
        args = ["stash", "push"]
        if message:
            args.extend(["-m", message])
        proc = self._run(args)
        return {"output": proc.stdout.strip()}

    def stash_pop(self) -> dict:
        """Pop the latest stash entry."""
        proc = self._run(["stash", "pop"])
        return {"output": proc.stdout.strip()}

    def reset_file(self, path: str) -> dict:
        """Discard working-tree changes for a single file."""
        self._run(["checkout", "--", path])
        return {"reset": path}

    def clean_worktree(self) -> dict:
        """Discard all working-tree changes (``git checkout -- .``)."""
        self._run(["checkout", "--", "."])
        return {"cleaned": True}

    def create_worktree(self, path: str, branch: str) -> dict:
        """Create a linked worktree at *path* on *branch*."""
        self._run(["worktree", "add", path, branch])
        return {"worktree": path, "branch": branch}

    def remove_worktree(self, path: str) -> dict:
        """Remove a linked worktree."""
        self._run(["worktree", "remove", path])
        return {"removed": path}
