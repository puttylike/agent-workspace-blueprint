"""Read-only Git state reader with a fixed subcommand allowlist."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..models import (
    BoundedCommandRunner,
    ObservationCache,
    ProjectRecord,
    ReaderError,
    SourceObservation,
)
from ..redaction import redact_text
from .registry import secure_workspace_root


class GitReader:
    """Collect branch, revision, status, divergence, and latest commit only."""

    _ALLOWED_SIGNATURES = {
        ("branch", "--show-current"),
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=normal"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"),
        ("log", "-1", "--format=%H%x00%cI%x00%s"),
        ("remote", "get-url", "origin"),
    }

    def __init__(
        self,
        executable: Path,
        runner: BoundedCommandRunner,
        *,
        cache_ttl_seconds: int = 30,
    ) -> None:
        self.executable = Path(executable)
        self.runner = runner
        self.cache = ObservationCache(cache_ttl_seconds)

    def _run(self, workspace: Path, args: Iterable[str], *, required: bool = True) -> str | None:
        signature = tuple(args)
        if signature not in self._ALLOWED_SIGNATURES:
            raise ReaderError("Git operation is not in the read-only allowlist")
        result = self.runner.run([str(self.executable), *signature], cwd=workspace)
        if not result.ok:
            if required:
                raise ReaderError(result.safe_error())
            return None
        if result.stdout_truncated:
            if required:
                raise ReaderError("Git output exceeded the configured limit")
            return None
        return result.stdout

    def read_workspace(self, workspace: str | Path) -> SourceObservation:
        cache_key = str(Path(workspace))
        cached = self.cache.fresh(cache_key)
        if cached:
            return cached
        try:
            root = secure_workspace_root(workspace)
            branch = (self._run(root, ("branch", "--show-current")) or "").strip() or "DETACHED"
            head = (self._run(root, ("rev-parse", "HEAD")) or "").strip()
            if len(head) != 40 or any(char not in "0123456789abcdefABCDEF" for char in head):
                raise ReaderError("Git returned an invalid HEAD revision")

            porcelain = self._run(root, ("status", "--porcelain=v1", "-z", "--untracked-files=normal"))
            changed_count = len([entry for entry in (porcelain or "").split("\x00") if entry])
            upstream_raw = self._run(
                root,
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
                required=False,
            )
            upstream = upstream_raw.strip() if upstream_raw else None
            ahead: int | None = None
            behind: int | None = None
            if upstream:
                divergence = self._run(
                    root,
                    ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"),
                    required=False,
                )
                if divergence:
                    parts = divergence.strip().split()
                    if len(parts) == 2 and all(part.isdigit() for part in parts):
                        ahead, behind = int(parts[0]), int(parts[1])

            latest_raw = self._run(root, ("log", "-1", "--format=%H%x00%cI%x00%s"), required=False)
            latest: dict[str, str] | None = None
            if latest_raw:
                parts = latest_raw.rstrip("\n").split("\x00", 2)
                if len(parts) == 3:
                    latest = {
                        "sha": parts[0],
                        "committed_at": parts[1],
                        "subject": redact_text(parts[2]),
                    }

            observation = SourceObservation.success(
                "git",
                {
                    "branch": redact_text(branch),
                    "head": head.lower(),
                    "clean": changed_count == 0,
                    "status": "CLEAN" if changed_count == 0 else "DIRTY",
                    "changed_path_count": changed_count,
                    "upstream": redact_text(upstream) if upstream else None,
                    "ahead": ahead,
                    "behind": behind,
                    "latest_commit": latest,
                },
            )
            self.cache.put(cache_key, observation)
            return observation
        except Exception as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            return SourceObservation.unavailable(
                "git", redact_text(str(exc) or exc.__class__.__name__), self.cache.last(cache_key)
            )

    def read(self, project: ProjectRecord) -> SourceObservation:
        return self.read_workspace(project.workspace)
