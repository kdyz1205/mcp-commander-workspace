"""
Centralized artifact storage for DevClaw.

Stores screenshots, logs, diffs, test results, session records, and error
reports under `.claw/artifacts/` organized by task_id and type.

Directory layout::

    .claw/artifacts/
      <task_id>/
        screenshots/
        logs/
        diffs/
        test_results/
        metadata.json
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Allowed artifact types and their subdirectory names
_TYPE_DIRS: dict[str, str] = {
    "screenshot": "screenshots",
    "log": "logs",
    "diff": "diffs",
    "test_result": "test_results",
    "session_record": "session_records",
    "error_report": "error_reports",
}


@dataclass
class Artifact:
    artifact_id: str
    task_id: str
    type: str  # "screenshot" | "log" | "diff" | "test_result" | "session_record" | "error_report"
    path: str
    created_at: float
    size_bytes: int
    metadata: dict


class ArtifactStore:
    """Manage artifact lifecycle: store, retrieve, list, and clean up."""

    def __init__(self, base_dir: str = ".claw/artifacts") -> None:
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _task_dir(self, task_id: str) -> Path:
        return self._base_dir / task_id

    def _type_subdir(self, task_id: str, artifact_type: str) -> Path:
        subdir_name = _TYPE_DIRS.get(artifact_type, artifact_type)
        return self._task_dir(task_id) / subdir_name

    def _metadata_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "metadata.json"

    def _load_task_metadata(self, task_id: str) -> dict[str, Any]:
        mp = self._metadata_path(task_id)
        if not mp.is_file():
            return {"task_id": task_id, "artifacts": []}
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"task_id": task_id, "artifacts": []}
        except (OSError, json.JSONDecodeError, TypeError):
            return {"task_id": task_id, "artifacts": []}

    def _save_task_metadata(self, task_id: str, meta: dict[str, Any]) -> None:
        mp = self._metadata_path(task_id)
        mp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp = mp.with_suffix(".tmp")
            tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(mp)
        except OSError:
            pass

    def _register_artifact(self, artifact: Artifact) -> None:
        meta = self._load_task_metadata(artifact.task_id)
        artifacts_list = meta.setdefault("artifacts", [])
        artifacts_list.append({
            "artifact_id": artifact.artifact_id,
            "type": artifact.type,
            "path": artifact.path,
            "created_at": artifact.created_at,
            "size_bytes": artifact.size_bytes,
            "metadata": artifact.metadata,
        })
        meta["updated_at"] = time.time()
        self._save_task_metadata(artifact.task_id, meta)

    @staticmethod
    def _generate_id() -> str:
        return uuid.uuid4().hex[:16]

    # ── Public API ───────────────────────────────────────────────────────────

    def store(
        self,
        task_id: str,
        artifact_type: str,
        content: str | bytes,
        filename: str | None = None,
        metadata: dict | None = None,
    ) -> Artifact:
        """Store content (str or bytes) as an artifact and return the record."""
        artifact_id = self._generate_id()
        if filename is None:
            ext = ".txt" if isinstance(content, str) else ".bin"
            filename = f"{artifact_id}{ext}"

        dest_dir = self._type_subdir(task_id, artifact_type)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        if isinstance(content, str):
            dest.write_text(content, encoding="utf-8")
        else:
            dest.write_bytes(content)

        size = dest.stat().st_size
        now = time.time()

        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            type=artifact_type,
            path=str(dest),
            created_at=now,
            size_bytes=size,
            metadata=metadata or {},
        )
        self._register_artifact(artifact)
        return artifact

    def store_file(
        self,
        task_id: str,
        artifact_type: str,
        source_path: str,
        metadata: dict | None = None,
    ) -> Artifact:
        """Copy an existing file into the artifact store."""
        src = Path(source_path)
        if not src.is_file():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        artifact_id = self._generate_id()
        dest_dir = self._type_subdir(task_id, artifact_type)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name

        # Avoid name collisions
        if dest.exists():
            stem = src.stem
            suffix = src.suffix
            dest = dest_dir / f"{stem}_{artifact_id[:8]}{suffix}"

        shutil.copy2(str(src), str(dest))
        size = dest.stat().st_size
        now = time.time()

        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            type=artifact_type,
            path=str(dest),
            created_at=now,
            size_bytes=size,
            metadata=metadata or {},
        )
        self._register_artifact(artifact)
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        """Retrieve an artifact record by its ID (searches all tasks)."""
        if not self._base_dir.is_dir():
            return None
        for task_dir in self._base_dir.iterdir():
            if not task_dir.is_dir():
                continue
            meta = self._load_task_metadata(task_dir.name)
            for entry in meta.get("artifacts", []):
                if entry.get("artifact_id") == artifact_id:
                    return Artifact(
                        artifact_id=entry["artifact_id"],
                        task_id=task_dir.name,
                        type=entry.get("type", ""),
                        path=entry.get("path", ""),
                        created_at=float(entry.get("created_at", 0)),
                        size_bytes=int(entry.get("size_bytes", 0)),
                        metadata=entry.get("metadata", {}),
                    )
        return None

    def list_by_task(self, task_id: str) -> list[Artifact]:
        meta = self._load_task_metadata(task_id)
        results: list[Artifact] = []
        for entry in meta.get("artifacts", []):
            results.append(Artifact(
                artifact_id=entry.get("artifact_id", ""),
                task_id=task_id,
                type=entry.get("type", ""),
                path=entry.get("path", ""),
                created_at=float(entry.get("created_at", 0)),
                size_bytes=int(entry.get("size_bytes", 0)),
                metadata=entry.get("metadata", {}),
            ))
        return results

    def list_by_type(self, artifact_type: str, limit: int = 50) -> list[Artifact]:
        results: list[Artifact] = []
        if not self._base_dir.is_dir():
            return results
        for task_dir in self._base_dir.iterdir():
            if not task_dir.is_dir():
                continue
            meta = self._load_task_metadata(task_dir.name)
            for entry in meta.get("artifacts", []):
                if entry.get("type") == artifact_type:
                    results.append(Artifact(
                        artifact_id=entry.get("artifact_id", ""),
                        task_id=task_dir.name,
                        type=entry.get("type", ""),
                        path=entry.get("path", ""),
                        created_at=float(entry.get("created_at", 0)),
                        size_bytes=int(entry.get("size_bytes", 0)),
                        metadata=entry.get("metadata", {}),
                    ))
        # Sort by created_at descending, return up to limit
        results.sort(key=lambda a: a.created_at, reverse=True)
        return results[:limit]

    def get_task_dir(self, task_id: str) -> str:
        return str(self._task_dir(task_id))

    def cleanup_old(self, max_age_days: int = 30) -> int:
        """Remove artifacts older than max_age_days. Returns count of deleted artifacts."""
        cutoff = time.time() - (max_age_days * 86400)
        deleted = 0

        if not self._base_dir.is_dir():
            return 0

        for task_dir in list(self._base_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            meta = self._load_task_metadata(task_dir.name)
            kept: list[dict[str, Any]] = []
            for entry in meta.get("artifacts", []):
                created = float(entry.get("created_at", 0))
                if created < cutoff:
                    # Remove the file
                    p = Path(entry.get("path", ""))
                    if p.is_file():
                        try:
                            p.unlink()
                        except OSError:
                            pass
                    deleted += 1
                else:
                    kept.append(entry)

            meta["artifacts"] = kept
            self._save_task_metadata(task_dir.name, meta)

            # Remove empty task directories
            try:
                remaining_files = list(task_dir.rglob("*"))
                # Only metadata.json left (or nothing)
                non_meta = [f for f in remaining_files if f.is_file() and f.name != "metadata.json"]
                if not non_meta:
                    shutil.rmtree(str(task_dir), ignore_errors=True)
            except OSError:
                pass

        return deleted

    def total_size_bytes(self) -> int:
        total = 0
        if not self._base_dir.is_dir():
            return 0
        for f in self._base_dir.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def store_execution_report(self, report: Any) -> Artifact:
        """Store a full ExecutionReport as JSON artifact."""
        task_id = getattr(report, "task_id", None) or "unknown"
        data: str
        if hasattr(report, "__dict__"):
            # dataclass or regular object
            try:
                from dataclasses import asdict
                data = json.dumps(asdict(report), indent=2, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                data = json.dumps(report.__dict__, indent=2, default=str, ensure_ascii=False)
        elif isinstance(report, dict):
            data = json.dumps(report, indent=2, default=str, ensure_ascii=False)
        else:
            data = str(report)

        return self.store(
            task_id=task_id,
            artifact_type="log",
            content=data,
            filename=f"execution_report_{int(time.time())}.json",
            metadata={"report_type": "execution"},
        )

    def store_verification_report(self, report: Any) -> Artifact:
        """Store a VerificationReport as JSON artifact."""
        task_id = getattr(report, "task_id", None) or "unknown"
        data: str
        if hasattr(report, "__dict__"):
            try:
                from dataclasses import asdict
                data = json.dumps(asdict(report), indent=2, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                data = json.dumps(report.__dict__, indent=2, default=str, ensure_ascii=False)
        elif isinstance(report, dict):
            data = json.dumps(report, indent=2, default=str, ensure_ascii=False)
        else:
            data = str(report)

        return self.store(
            task_id=task_id,
            artifact_type="test_result",
            content=data,
            filename=f"verification_report_{int(time.time())}.json",
            metadata={"report_type": "verification"},
        )
