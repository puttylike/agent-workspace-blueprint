"""Typed configuration, registry, and observation models.

The command runner in this module is intentionally small and non-generic from
the application's point of view: only reader classes construct argv arrays.
There is no route from user input to this runner.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .redaction import redact_text


class ControllerError(RuntimeError):
    """Base exception for safe, user-displayable controller failures."""


class ConfigurationError(ControllerError):
    """Configuration or registry data is invalid."""


class SecurityBoundaryError(ControllerError):
    """A path or command crossed a declared security boundary."""


class ReaderError(ControllerError):
    """A read-only source could not be queried or parsed."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RuntimeConfig:
    sqlite_cache: Path


@dataclass(frozen=True)
class CommandConfig:
    git: Path
    gh: Path
    openclaw: Path
    timeout_seconds: float = 10.0
    stdout_limit_bytes: int = 1_048_576
    stderr_limit_bytes: int = 65_536


@dataclass(frozen=True)
class ExpectedAgentConfig:
    id: str
    display_name: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class OpenClawConfig:
    access_method: str
    legacy_agents: tuple[str, ...] = ()
    manager_agent_id: str | None = None
    expected_agents: tuple[ExpectedAgentConfig, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    version: int
    mode: str
    listen_host: str
    listen_port: int
    project_registry: Path
    knowledge_root: Path
    blueprint_root: Path
    runtime: RuntimeConfig
    commands: CommandConfig
    cache_ttl_seconds: int
    openclaw: OpenClawConfig
    source_path: Path

    @property
    def sqlite_cache(self) -> Path:
        return self.runtime.sqlite_cache


@dataclass(frozen=True)
class GitProjectConfig:
    repository: str
    canonical_branch: str
    active_branch: str
    draft_pr: int | None = None


@dataclass(frozen=True)
class BeadsProjectConfig:
    binary: Path
    directory: Path


@dataclass(frozen=True)
class ProjectPolicies:
    manager_write_access: bool = False
    direct_merge: bool = False
    public_publish: str = "approval_required"


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    type: str
    lifecycle: str
    phase: str
    agent_id: str
    workspace: Path
    legacy_workspace: str | None
    git: GitProjectConfig
    beads: BeadsProjectConfig
    policies: ProjectPolicies


@dataclass(frozen=True)
class ProjectRegistry:
    version: int
    projects: tuple[ProjectRecord, ...]

    def get(self, project_id: str) -> ProjectRecord | None:
        return next((project for project in self.projects if project.id == project_id), None)


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    execution_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.execution_error is None

    def safe_error(self) -> str:
        if self.timed_out:
            return "read command timed out"
        if self.execution_error:
            return redact_text(self.execution_error)
        message = self.stderr.strip() or f"read command exited with status {self.returncode}"
        if self.stderr_truncated:
            message += " [stderr truncated]"
        return redact_text(message)


@dataclass(frozen=True)
class SourceObservation:
    source: str
    availability: str
    data: Mapping[str, Any] | None
    observed_at: str | None
    queried_at: str
    stale: bool = False
    cache_hit: bool = False
    error: str | None = None

    @classmethod
    def success(cls, source: str, data: Mapping[str, Any]) -> "SourceObservation":
        now = utc_now_iso()
        return cls(source, "AVAILABLE", dict(data), now, now)

    @classmethod
    def unknown(cls, source: str, reason: str) -> "SourceObservation":
        return cls(source, "UNKNOWN", None, None, utc_now_iso(), error=redact_text(reason))

    @classmethod
    def unavailable(
        cls,
        source: str,
        reason: str,
        cached: "SourceObservation | None" = None,
    ) -> "SourceObservation":
        now = utc_now_iso()
        if cached and cached.data is not None:
            return cls(
                source,
                "UNAVAILABLE",
                cached.data,
                cached.observed_at,
                now,
                stale=True,
                cache_hit=True,
                error=redact_text(reason),
            )
        return cls(source, "UNAVAILABLE", None, None, now, error=redact_text(reason))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "availability": self.availability,
            "data": dict(self.data) if self.data is not None else None,
            "observed_at": self.observed_at,
            "queried_at": self.queried_at,
            "stale": self.stale,
            "cache_hit": self.cache_hit,
            "error": self.error,
        }


class ObservationCache:
    """Process-local last-success cache with explicit freshness metadata."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = max(0, ttl_seconds)
        self._entries: dict[str, tuple[datetime, SourceObservation]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, observation: SourceObservation) -> None:
        if observation.availability != "AVAILABLE":
            return
        self.remember(key, observation)

    def remember(self, key: str, observation: SourceObservation) -> None:
        """Cache any observation, used to throttle repeated upstream failures."""

        with self._lock:
            self._entries[key] = (utc_now(), observation)

    def fresh(self, key: str) -> SourceObservation | None:
        with self._lock:
            entry = self._entries.get(key)
        if not entry:
            return None
        inserted, observation = entry
        age = (utc_now() - inserted).total_seconds()
        if age > self.ttl_seconds:
            return None
        return replace(observation, cache_hit=True)

    def last(self, key: str) -> SourceObservation | None:
        with self._lock:
            entry = self._entries.get(key)
        return entry[1] if entry else None


class BoundedCommandRunner:
    """Run a pre-built argv without a shell and drain output within bounds."""

    def __init__(
        self,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.stdout_limit_bytes = stdout_limit_bytes
        self.stderr_limit_bytes = stderr_limit_bytes

    @staticmethod
    def _drain(stream: Any, limit: int, target: bytearray, truncated: list[bool]) -> None:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    break
                remaining = max(0, limit - len(target))
                if remaining:
                    target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[0] = True
        finally:
            stream.close()

    def run(self, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        if not isinstance(argv, (list, tuple)) or not argv:
            raise SecurityBoundaryError("command argv must be a non-empty array")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise SecurityBoundaryError("command argv contains an invalid value")
        executable = Path(argv[0])
        if not executable.is_absolute():
            raise SecurityBoundaryError("command executable must be an absolute path")

        stdout = bytearray()
        stderr = bytearray()
        stdout_truncated = [False]
        stderr_truncated = [False]
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "GH_PROMPT_DISABLED": "1",
                "PAGER": "cat",
                "NO_COLOR": "1",
            }
        )
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=environment,
            )
        except OSError as exc:
            return CommandResult(None, "", "", execution_error=redact_text(str(exc)))

        assert process.stdout is not None and process.stderr is not None
        threads = (
            threading.Thread(
                target=self._drain,
                args=(process.stdout, self.stdout_limit_bytes, stdout, stdout_truncated),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=(process.stderr, self.stderr_limit_bytes, stderr, stderr_truncated),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            returncode = process.wait()
        for thread in threads:
            thread.join(timeout=1)

        return CommandResult(
            returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            stdout_truncated=stdout_truncated[0],
            stderr_truncated=stderr_truncated[0],
        )
