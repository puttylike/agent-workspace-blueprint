"""Logical, read-only Beads state reader.

Raw Dolt chunks, manifests, hashes, and mtimes are deliberately ignored. Some
versions may update physical metadata while servicing a logical read.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from ..models import (
    BoundedCommandRunner,
    ObservationCache,
    ProjectRecord,
    ReaderError,
    SecurityBoundaryError,
    SourceObservation,
)
from ..redaction import redact_text
from .registry import secure_path_within, secure_workspace_root


class BeadsReader:
    _ALLOWED_SIGNATURES = {("--version",), ("list", "--json"), ("ready", "--json")}

    def __init__(
        self,
        runner: BoundedCommandRunner,
        *,
        cache_ttl_seconds: int = 30,
    ) -> None:
        self.runner = runner
        self.cache = ObservationCache(cache_ttl_seconds)

    @staticmethod
    def _validate_binary(path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise SecurityBoundaryError("Beads binary must be a normalized absolute path")
        lexical = Path(os.path.abspath(path))
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise SecurityBoundaryError("pinned Beads binary is unavailable") from exc
        if resolved != lexical or not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise SecurityBoundaryError("pinned Beads binary must be a non-symlink executable")
        return resolved

    def _run(self, binary: Path, workspace: Path, args: tuple[str, ...]) -> str:
        if args not in self._ALLOWED_SIGNATURES:
            raise ReaderError("Beads operation is not in the read-only allowlist")
        result = self.runner.run([str(binary), *args], cwd=workspace)
        if not result.ok:
            raise ReaderError(result.safe_error())
        if result.stdout_truncated:
            raise ReaderError("Beads output exceeded the configured limit")
        return result.stdout

    @staticmethod
    def _items(raw: str, label: str) -> list[Mapping[str, Any]]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReaderError(f"{label} returned invalid JSON") from exc
        if isinstance(value, Mapping):
            for key in ("tasks", "issues", "items", "data"):
                candidate = value.get(key)
                if isinstance(candidate, list):
                    value = candidate
                    break
        if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
            raise ReaderError(f"{label} JSON must contain a task list")
        return list(value)

    @staticmethod
    def _summary(items: list[Mapping[str, Any]]) -> list[dict[str, str | None]]:
        summaries: list[dict[str, str | None]] = []
        for item in items:
            task_id = item.get("id")
            status = item.get("status")
            summaries.append(
                {
                    "id": redact_text(str(task_id)) if task_id is not None else None,
                    "status": redact_text(str(status).lower()) if status is not None else "unknown",
                }
            )
        return summaries

    def _interaction_summary(self, workspace: Path, beads_directory: Path) -> dict[str, Any]:
        candidate = beads_directory / "interactions.jsonl"
        if not os.path.lexists(candidate):
            return {"count": 0, "latest_at": None, "truncated": False}
        if candidate.is_symlink():
            raise SecurityBoundaryError("symlinked interactions.jsonl is not allowed")
        path = secure_path_within(workspace, candidate)
        if not path.is_file():
            raise SecurityBoundaryError("interactions.jsonl is not a regular file")
        with path.open("rb") as handle:
            payload = handle.read(self.runner.stdout_limit_bytes + 1)
        truncated = len(payload) > self.runner.stdout_limit_bytes
        payload = payload[: self.runner.stdout_limit_bytes]
        count = 0
        latest: str | None = None
        for line in payload.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            count += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping):
                continue
            for key in ("timestamp", "created_at", "updated_at", "at"):
                value = event.get(key)
                if isinstance(value, str) and (latest is None or value > latest):
                    latest = redact_text(value)
                    break
        return {"count": count, "latest_at": latest, "truncated": truncated}

    def read(self, project: ProjectRecord) -> SourceObservation:
        cache_key = project.id
        cached = self.cache.fresh(cache_key)
        if cached:
            return cached
        try:
            workspace = secure_workspace_root(project.workspace)
            beads_directory = secure_path_within(
                workspace, project.beads.directory, require_directory=True
            )
            binary = self._validate_binary(project.beads.binary)
            version = redact_text(self._run(binary, workspace, ("--version",)).strip())
            list_items = self._items(self._run(binary, workspace, ("list", "--json")), "bd list")
            ready_items = self._items(self._run(binary, workspace, ("ready", "--json")), "bd ready")

            list_summary = self._summary(list_items)
            ready_summary = self._summary(ready_items)
            statuses = Counter(str(item.get("status", "unknown")).lower() for item in list_items)
            ready_ids = {str(item.get("id")) for item in ready_items if item.get("id") is not None}
            implicit_blocked = sum(
                1
                for item in list_items
                if str(item.get("status", "")).lower() == "open"
                and item.get("id") is not None
                and str(item.get("id")) not in ready_ids
            )
            explicit_blocked = statuses.get("blocked", 0)
            completed = sum(statuses.get(name, 0) for name in ("closed", "done", "completed"))
            total = len(list_items)
            progress = round(completed * 100 / total, 1) if total else None

            counts = dict(sorted(statuses.items()))
            counts["open"] = statuses.get("open", 0)
            counts["ready"] = len(ready_items)
            counts["blocked"] = explicit_blocked + implicit_blocked
            observation = SourceObservation.success(
                "beads",
                {
                    "version": version,
                    "list": list_summary,
                    "ready": ready_summary,
                    "list_count": total,
                    "ready_count": len(ready_items),
                    "counts": counts,
                    "completed_count": completed,
                    "progress_percent": progress,
                    "interactions": self._interaction_summary(workspace, beads_directory),
                },
            )
            self.cache.put(cache_key, observation)
            return observation
        except Exception as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            return SourceObservation.unavailable(
                "beads", redact_text(str(exc) or exc.__class__.__name__), self.cache.last(cache_key)
            )
