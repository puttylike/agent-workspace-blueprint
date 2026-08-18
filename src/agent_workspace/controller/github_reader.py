"""Single-attempt, read-only GitHub pull-request reader."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from ..models import (
    BoundedCommandRunner,
    ObservationCache,
    ProjectRecord,
    ReaderError,
    SourceObservation,
)
from ..redaction import redact_text
from .registry import secure_workspace_root

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubReader:
    _JSON_FIELDS = "number,state,isDraft,url,updatedAt,headRefName,baseRefName,mergeStateStatus"

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
        self.failure_cache = ObservationCache(cache_ttl_seconds)

    @staticmethod
    def _failure_kind(message: str, timed_out: bool) -> str:
        if timed_out:
            return "TIMEOUT"
        if re.search(r"(?:HTTP\s*)?5\d\d\b", message, re.IGNORECASE):
            return "UPSTREAM_5XX"
        return "UNAVAILABLE"

    def read(self, project: ProjectRecord) -> SourceObservation:
        if project.git.draft_pr is None:
            return SourceObservation.unknown("github", "no pull request is registered")
        cache_key = f"{project.git.repository}#{project.git.draft_pr}"
        cached = self.cache.fresh(cache_key)
        if cached:
            return cached
        recent_failure = self.failure_cache.fresh(cache_key)
        if recent_failure:
            return recent_failure
        try:
            if not _REPOSITORY.fullmatch(project.git.repository):
                raise ReaderError("GitHub repository identifier is invalid")
            workspace = secure_workspace_root(project.workspace)
            argv = [
                str(self.executable),
                "pr",
                "view",
                str(project.git.draft_pr),
                "--repo",
                project.git.repository,
                "--json",
                self._JSON_FIELDS,
            ]
            result = self.runner.run(argv, cwd=workspace)
            if not result.ok:
                kind = self._failure_kind(result.safe_error(), result.timed_out)
                failure = SourceObservation.unavailable(
                    "github",
                    f"{kind}: {result.safe_error()}",
                    self.cache.last(cache_key),
                )
                self.failure_cache.remember(cache_key, failure)
                return failure
            if result.stdout_truncated:
                raise ReaderError("GitHub output exceeded the configured limit")
            try:
                raw = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise ReaderError("GitHub returned invalid JSON") from exc
            if not isinstance(raw, Mapping):
                raise ReaderError("GitHub returned an invalid pull-request record")
            number = raw.get("number")
            if isinstance(number, bool) or not isinstance(number, int):
                raise ReaderError("GitHub returned an invalid pull-request number")
            state = str(raw.get("state", "UNKNOWN")).upper()
            is_draft = bool(raw.get("isDraft", False))
            observation = SourceObservation.success(
                "github",
                {
                    "number": number,
                    "state": state,
                    "is_draft": is_draft,
                    "display_state": "DRAFT" if is_draft and state == "OPEN" else state,
                    "url": redact_text(str(raw.get("url", ""))) or None,
                    "updated_at": redact_text(str(raw.get("updatedAt", ""))) or None,
                    "head_branch": redact_text(str(raw.get("headRefName", ""))) or None,
                    "base_branch": redact_text(str(raw.get("baseRefName", ""))) or None,
                    "merge_state": redact_text(str(raw.get("mergeStateStatus", "UNKNOWN"))).upper(),
                },
            )
            self.cache.put(cache_key, observation)
            return observation
        except Exception as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            failure = SourceObservation.unavailable(
                "github", redact_text(str(exc) or exc.__class__.__name__), self.cache.last(cache_key)
            )
            self.failure_cache.remember(cache_key, failure)
            return failure
