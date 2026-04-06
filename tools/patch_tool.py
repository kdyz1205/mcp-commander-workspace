"""
Code patching tool for the DevClaw agent framework.

Provides a disciplined patch workflow: every write creates a backup,
diffs are generated and applied cleanly, and all git operations go
through the CLI. All mutations are logged.
"""

import difflib
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


BACKUP_DIR = Path(".claw/backups")
LOG_DIR = Path(".claw/logs")

logger = logging.getLogger("devclaw.patch_tool")


def _ensure_dirs():
    """Create backup and log directories if they do not exist."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    """Return a compact UTC timestamp string for filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")


def _log_operation(operation: str, details: dict):
    """Append an operation record to the patch log."""
    _ensure_dirs()
    log_file = LOG_DIR / "patch_tool.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        **details,
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    logger.info("patch_tool: %s %s", operation, details.get("path", ""))


def _backup_file(path: str) -> Optional[str]:
    """Create a timestamped backup of *path* and return the backup path.

    Returns None if the source file does not exist.
    """
    src = Path(path)
    if not src.exists():
        return None
    _ensure_dirs()
    # Preserve original name with a timestamp prefix to avoid collisions
    backup_name = f"{_ts()}_{src.name}"
    backup_path = BACKUP_DIR / backup_name
    shutil.copy2(str(src), str(backup_path))

    # Write a small sidecar so we can map backup -> original
    meta_path = Path(str(backup_path) + ".meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "original": str(src.resolve()),
            "backup": str(backup_path.resolve()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)

    return str(backup_path.resolve())


def _run_git(*args, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git CLI command and return the CompletedProcess."""
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True,
        text=True,
        check=check,
    )


class PatchTool:
    """Disciplined code patching tool for DevClaw."""

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def read_file(self, path: str) -> dict:
        """Read a file and return its content with metadata.

        Returns:
            dict with keys: content, lines, size, encoding.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")

        # Try utf-8 first, fall back to latin-1 (never fails)
        encoding = "utf-8"
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            encoding = "latin-1"
            content = p.read_text(encoding="latin-1")

        result = {
            "content": content,
            "lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
            "size": p.stat().st_size,
            "encoding": encoding,
        }
        _log_operation("read_file", {"path": path, "size": result["size"]})
        return result

    def write_file(self, path: str, content: str, backup: bool = True) -> dict:
        """Write content to a file, optionally creating a backup first.

        Args:
            path: Destination file path.
            content: The text content to write.
            backup: Whether to back up the existing file before overwriting.

        Returns:
            dict with keys: written (int, bytes written), backup_path (str or None).
        """
        backup_path = None
        if backup:
            backup_path = _backup_file(path)

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

        result = {
            "written": len(content.encode("utf-8")),
            "backup_path": backup_path,
        }
        _log_operation("write_file", {"path": path, **result})
        return result

    # ------------------------------------------------------------------
    # Diff operations
    # ------------------------------------------------------------------

    def generate_diff(self, original: str, modified: str, filepath: str = "file") -> str:
        """Generate a unified diff string between two text blobs.

        Args:
            original: The original file content.
            modified: The modified file content.
            filepath: Label used in the diff header.

        Returns:
            A unified diff string.
        """
        original_lines = original.splitlines(keepends=True)
        modified_lines = modified.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{filepath}",
            tofile=f"b/{filepath}",
            lineterm="",
        )
        result = "\n".join(diff)
        _log_operation("generate_diff", {"filepath": filepath, "diff_len": len(result)})
        return result

    def apply_unified_diff(self, diff: str, dry_run: bool = False) -> dict:
        """Apply a unified diff to the working tree.

        Tries the system `patch` command first for robustness.  If that
        is unavailable, falls back to a simple Python-based applier that
        handles single-file unified diffs.

        Args:
            diff: The unified diff text.
            dry_run: If True, only check whether the patch applies cleanly.

        Returns:
            dict with keys: applied (bool), files_changed (list[str]), errors (list[str]).
        """
        files_changed = []
        errors = []

        # --- Extract target files from diff headers for backup ---
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                target = line[6:].strip()
                if target and target != "/dev/null":
                    files_changed.append(target)

        # Back up targets before patching (unless dry run)
        if not dry_run:
            for fpath in files_changed:
                _backup_file(fpath)

        # --- Try system patch command ---
        try:
            cmd = ["patch", "-p1", "--forward"]
            if dry_run:
                cmd.append("--dry-run")
            proc = subprocess.run(
                cmd,
                input=diff,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                _log_operation("apply_unified_diff", {
                    "method": "patch",
                    "dry_run": dry_run,
                    "files_changed": files_changed,
                })
                return {
                    "applied": True,
                    "files_changed": files_changed,
                    "errors": [],
                }
            else:
                errors.append(f"patch command failed (rc={proc.returncode}): {proc.stderr.strip()}")
        except FileNotFoundError:
            errors.append("patch command not found, falling back to Python applier")
        except subprocess.TimeoutExpired:
            errors.append("patch command timed out")

        # --- Fallback: simple Python-based single-file applier ---
        if not dry_run:
            try:
                applied_any = self._apply_diff_python(diff)
                if applied_any:
                    _log_operation("apply_unified_diff", {
                        "method": "python_fallback",
                        "dry_run": dry_run,
                        "files_changed": files_changed,
                    })
                    return {
                        "applied": True,
                        "files_changed": files_changed,
                        "errors": [],
                    }
                else:
                    errors.append("Python fallback applier could not apply the diff")
            except Exception as exc:
                errors.append(f"Python fallback error: {exc}")

        _log_operation("apply_unified_diff", {
            "applied": False,
            "dry_run": dry_run,
            "errors": errors,
        })
        return {
            "applied": False,
            "files_changed": [],
            "errors": errors,
        }

    def _apply_diff_python(self, diff: str) -> bool:
        """Minimal Python unified-diff applier for single-file diffs.

        Parses the unified diff, reads the target file, applies hunks
        sequentially, and writes the result.  Returns True on success.
        """
        lines = diff.splitlines(keepends=True)
        target_path = None
        hunks = []
        current_hunk_lines = []

        for line in lines:
            if line.startswith("+++ b/"):
                target_path = line[6:].strip()
            elif line.startswith("@@ "):
                if current_hunk_lines:
                    hunks.append(current_hunk_lines)
                current_hunk_lines = [line]
            elif current_hunk_lines:
                current_hunk_lines.append(line)
        if current_hunk_lines:
            hunks.append(current_hunk_lines)

        if not target_path or not hunks:
            return False

        p = Path(target_path)
        if p.exists():
            original = p.read_text(encoding="utf-8").splitlines(keepends=True)
        else:
            original = []

        # Simple approach: use difflib to reconstruct from the diff info
        # Build expected output by replaying hunks
        result = list(original)
        offset = 0  # track line shifts from previous hunks

        for hunk in hunks:
            header = hunk[0]
            # Parse @@ -start,count +start,count @@
            parts = header.split()
            old_spec = parts[1]  # e.g. -1,5
            old_start = int(old_spec.split(",")[0].lstrip("-")) - 1  # 0-indexed

            remove_lines = []
            add_lines = []
            context_before = 0
            started = False

            for hl in hunk[1:]:
                if hl.startswith("-"):
                    started = True
                    remove_lines.append(hl[1:])
                elif hl.startswith("+"):
                    started = True
                    add_lines.append(hl[1:])
                else:
                    if not started:
                        context_before += 1

            pos = old_start + offset + context_before
            # Remove old lines
            for _ in remove_lines:
                if pos < len(result):
                    result.pop(pos)
            # Insert new lines
            for i, al in enumerate(add_lines):
                result.insert(pos + i, al)

            offset += len(add_lines) - len(remove_lines)

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(result), encoding="utf-8")
        return True

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def git_diff(self, staged: bool = False) -> str:
        """Return the output of `git diff` (or `git diff --staged`).

        Args:
            staged: If True, show only staged changes.

        Returns:
            The diff text.
        """
        args = ["diff"]
        if staged:
            args.append("--staged")
        proc = _run_git(*args, check=False)
        _log_operation("git_diff", {"staged": staged})
        return proc.stdout

    def git_status(self) -> dict:
        """Return a structured summary of `git status --porcelain`.

        Returns:
            dict with keys: modified, untracked, staged, deleted.
        """
        proc = _run_git("status", "--porcelain", check=False)
        modified = []
        untracked = []
        staged = []
        deleted = []

        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            index_status = line[0]
            worktree_status = line[1]
            filepath = line[3:]

            if index_status == "?":
                untracked.append(filepath)
            elif index_status == "D" or worktree_status == "D":
                deleted.append(filepath)
            else:
                if index_status in ("M", "A", "R", "C"):
                    staged.append(filepath)
                if worktree_status == "M":
                    modified.append(filepath)

        result = {
            "modified": modified,
            "untracked": untracked,
            "staged": staged,
            "deleted": deleted,
        }
        _log_operation("git_status", {"counts": {k: len(v) for k, v in result.items()}})
        return result

    def git_commit(self, message: str, files: list = None) -> dict:
        """Stage files and create a git commit.

        Args:
            message: Commit message.
            files: Specific files to stage. If None, commits whatever is
                   already staged.

        Returns:
            dict with keys: commit_hash, files_committed.
        """
        if files:
            _run_git("add", "--", *files)

        proc = _run_git("commit", "-m", message, check=False)

        if proc.returncode != 0:
            _log_operation("git_commit", {"error": proc.stderr.strip()})
            raise RuntimeError(f"git commit failed: {proc.stderr.strip()}")

        # Extract short hash
        hash_proc = _run_git("rev-parse", "--short", "HEAD")
        commit_hash = hash_proc.stdout.strip()

        # Determine committed files from the commit
        show_proc = _run_git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        files_committed = [f for f in show_proc.stdout.strip().splitlines() if f]

        result = {
            "commit_hash": commit_hash,
            "files_committed": files_committed,
        }
        _log_operation("git_commit", result)
        return result

    # ------------------------------------------------------------------
    # Revert / restore
    # ------------------------------------------------------------------

    def revert_file(self, path: str) -> bool:
        """Restore a file from its most recent backup.

        Args:
            path: The original file path to restore.

        Returns:
            True if restored successfully, False if no backup found.
        """
        _ensure_dirs()
        resolved = str(Path(path).resolve())

        # Find the most recent backup for this path
        candidates = []
        for meta_file in BACKUP_DIR.glob("*.meta.json"):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if meta.get("original") == resolved:
                    candidates.append(meta)
            except Exception:
                continue

        if not candidates:
            _log_operation("revert_file", {"path": path, "restored": False})
            return False

        # Sort by timestamp descending
        candidates.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        best = candidates[0]
        backup_src = best["backup"]

        shutil.copy2(backup_src, path)
        _log_operation("revert_file", {"path": path, "from_backup": backup_src, "restored": True})
        return True

    def restore_worktree(self) -> dict:
        """Discard all unstaged changes in the working tree (git checkout -- .).

        Returns:
            dict with keys: restored (bool), output (str).
        """
        proc = _run_git("checkout", "--", ".", check=False)
        restored = proc.returncode == 0
        result = {
            "restored": restored,
            "output": (proc.stdout + proc.stderr).strip(),
        }
        _log_operation("restore_worktree", result)
        return result

    # ------------------------------------------------------------------
    # Backup listing
    # ------------------------------------------------------------------

    def list_backups(self) -> list:
        """List all backups in .claw/backups/.

        Returns:
            List of dicts with keys: backup_path, original, timestamp, size_bytes.
        """
        _ensure_dirs()
        results = []

        for meta_file in sorted(BACKUP_DIR.glob("*.meta.json"), reverse=True):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                backup_path = meta.get("backup", "")
                bp = Path(backup_path)
                results.append({
                    "backup_path": backup_path,
                    "original": meta.get("original", ""),
                    "timestamp": meta.get("timestamp", ""),
                    "size_bytes": bp.stat().st_size if bp.exists() else 0,
                })
            except Exception:
                continue

        _log_operation("list_backups", {"count": len(results)})
        return results
