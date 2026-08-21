"""Schema-validated OpenClaw Gateway read operations."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from ..models import (
    BoundedCommandRunner,
    ObservationCache,
    ReaderError,
    SourceObservation,
)
from ..redaction import redact_text

_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _payload(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ReaderError("OpenClaw returned a non-object response")
    for key in ("result", "payload", "data"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            return nested
    return raw


def _timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        return redact_text(value)
    return None


class OpenClawReader:
    """Expose only agent metadata and per-agent latest activity, never keys."""

    def __init__(
        self,
        executable: Path,
        runner: BoundedCommandRunner,
        *,
        cache_ttl_seconds: int = 30,
        working_directory: Path | None = None,
        ui_timeout_seconds: float = 7.0,
    ) -> None:
        self.executable = Path(executable)
        self.runner = runner
        self.cache = ObservationCache(cache_ttl_seconds)
        self.failure_cache = ObservationCache(cache_ttl_seconds)
        self.working_directory = (working_directory or Path.cwd()).resolve()
        self.ui_timeout_seconds = max(
            0.1, min(float(getattr(runner, "timeout_seconds", 10.0)), ui_timeout_seconds)
        )

    def _call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method not in {"agents.list", "sessions.list"}:
            raise ReaderError("OpenClaw operation is not in the read-only allowlist")
        encoded = json.dumps(params, separators=(",", ":"), sort_keys=True)
        argv = [
            str(self.executable),
            "gateway",
            "call",
            method,
            "--json",
            "--params",
            encoded,
            "--timeout",
            str(max(1, int(self.ui_timeout_seconds * 1000))),
        ]
        result = self.runner.run(argv, cwd=self.working_directory)
        if not result.ok:
            raise ReaderError(result.safe_error())
        if result.stdout_truncated:
            raise ReaderError("OpenClaw output exceeded the configured limit")
        try:
            return _payload(json.loads(result.stdout))
        except json.JSONDecodeError as exc:
            raise ReaderError("OpenClaw returned invalid JSON") from exc

    def _cached_or_unknown(self, cache_key: str, source: str, reason: str) -> SourceObservation:
        cached = self.cache.fresh(cache_key)
        if cached:
            return cached
        last = self.cache.last(cache_key)
        if last and last.data is not None:
            return SourceObservation.unavailable(source, reason, last)
        return SourceObservation.unknown(source, reason)

    def list_agents(self) -> SourceObservation:
        cache_key = "agents.list"
        cached = self.cache.fresh(cache_key)
        if cached:
            return cached
        recent_failure = self.failure_cache.fresh(cache_key)
        if recent_failure:
            return recent_failure
        try:
            raw = self._call("agents.list", {})
            agents_raw = raw.get("agents")
            if not isinstance(agents_raw, list):
                raise ReaderError("OpenClaw agents.list omitted agents")
            default_id = raw.get("defaultId")
            agents: list[dict[str, Any]] = []
            for item in agents_raw:
                if not isinstance(item, Mapping):
                    continue
                agent_id = item.get("id")
                if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
                    continue
                model_raw = item.get("model")
                model: str | None = None
                if isinstance(model_raw, str):
                    model = redact_text(model_raw)
                elif isinstance(model_raw, Mapping) and isinstance(model_raw.get("primary"), str):
                    model = redact_text(str(model_raw["primary"]))
                agents.append(
                    {
                        "id": agent_id,
                        "name": redact_text(str(item.get("name", ""))) or None,
                        "model": model,
                        "is_default": agent_id == default_id,
                    }
                )
            observation = SourceObservation.success(
                "openclaw_agents",
                {"agents": agents, "count": len(agents)},
            )
            self.cache.put(cache_key, observation)
            return observation
        except Exception as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            failure = SourceObservation.unavailable(
                "openclaw_agents",
                redact_text(str(exc) or exc.__class__.__name__),
                self.cache.last(cache_key),
            )
            self.failure_cache.remember(cache_key, failure)
            return failure

    def cached_agents(self, reason: str = "live OpenClaw agents were not queried") -> SourceObservation:
        return self._cached_or_unknown("agents.list", "openclaw_agents", reason)

    @staticmethod
    def _session_agent_id(item: Mapping[str, Any]) -> str | None:
        runtime = item.get("agentRuntime")
        if isinstance(runtime, Mapping):
            agent_id = runtime.get("id")
            if isinstance(agent_id, str) and _AGENT_ID.fullmatch(agent_id):
                return agent_id
        for key in ("agentId", "agent_id"):
            agent_id = item.get(key)
            if isinstance(agent_id, str) and _AGENT_ID.fullmatch(agent_id):
                return agent_id
        return None

    @staticmethod
    def _session_timestamp(item: Mapping[str, Any]) -> str | None:
        for key in ("updatedAt", "lastActivityAt", "endedAt", "startedAt"):
            stamp = _timestamp(item.get(key))
            if stamp is not None:
                return stamp
        return None

    def list_agent_sessions(self) -> SourceObservation:
        """Read all visible sessions once and summarize them by agent id."""

        cache_key = "sessions.list"
        cached = self.cache.fresh(cache_key)
        if cached:
            return cached
        recent_failure = self.failure_cache.fresh(cache_key)
        if recent_failure:
            return recent_failure
        try:
            raw = self._call("sessions.list", {"limit": 100})
            sessions = raw.get("sessions")
            if not isinstance(sessions, list):
                raise ReaderError("OpenClaw sessions.list omitted sessions")
            by_agent: dict[str, dict[str, Any]] = {}
            for item in sessions:
                if not isinstance(item, Mapping):
                    continue
                agent_id = self._session_agent_id(item)
                stamp = self._session_timestamp(item)
                if agent_id is None:
                    continue
                current = by_agent.setdefault(
                    agent_id,
                    {"agent_id": agent_id, "recent_session_at": None, "session_count": 0},
                )
                current["session_count"] += 1
                if stamp is not None and (
                    current["recent_session_at"] is None or stamp > current["recent_session_at"]
                ):
                    current["recent_session_at"] = stamp
            observation = SourceObservation.success(
                "openclaw_sessions",
                {
                    "sessions_by_agent": by_agent,
                    "session_count": len(sessions),
                    "agent_count": len(by_agent),
                },
            )
            self.cache.put(cache_key, observation)
            return observation
        except Exception as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            failure = SourceObservation.unavailable(
                "openclaw_sessions",
                redact_text(str(exc) or exc.__class__.__name__),
                self.cache.last(cache_key),
            )
            self.failure_cache.remember(cache_key, failure)
            return failure

    def cached_agent_sessions(
        self, reason: str = "live OpenClaw sessions were not queried"
    ) -> SourceObservation:
        return self._cached_or_unknown("sessions.list", "openclaw_sessions", reason)

    def session_for_agent(
        self,
        agent_id: str,
        sessions: SourceObservation | None = None,
    ) -> SourceObservation:
        if not _AGENT_ID.fullmatch(agent_id):
            return SourceObservation.unknown("openclaw_sessions", "agent id is invalid")
        observation = sessions if sessions is not None else self.list_agent_sessions()
        if observation.data is None:
            if observation.availability == "UNKNOWN":
                return SourceObservation.unknown(
                    "openclaw_sessions", observation.error or "session source unavailable"
                )
            return SourceObservation.unavailable(
                "openclaw_sessions", observation.error or "session source unavailable"
            )
        by_agent = observation.data.get("sessions_by_agent")
        if not isinstance(by_agent, Mapping):
            return SourceObservation.unavailable(
                "openclaw_sessions", "session source returned invalid grouped data"
            )
        raw = by_agent.get(agent_id)
        data = dict(raw) if isinstance(raw, Mapping) else {
            "agent_id": agent_id,
            "recent_session_at": None,
            "session_count": 0,
        }
        return SourceObservation(
            "openclaw_sessions",
            observation.availability,
            data,
            observation.observed_at,
            observation.queried_at,
            stale=observation.stale,
            cache_hit=observation.cache_hit,
            error=observation.error,
        )

    def recent_session(self, agent_id: str) -> SourceObservation:
        return self.session_for_agent(agent_id)
