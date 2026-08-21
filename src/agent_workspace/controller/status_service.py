"""Aggregate independent read-only observations for HTML and JSON views."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..config import load_app_config
from ..models import (
    AppConfig,
    BoundedCommandRunner,
    ProjectRecord,
    ProjectRegistry,
    SourceObservation,
    utc_now_iso,
)
from ..redaction import redact_text
from .beads_reader import BeadsReader
from .git_reader import GitReader
from .github_reader import GitHubReader
from .openclaw_reader import OpenClawReader
from .registry import RegistryReader


def _data(observation: SourceObservation) -> dict[str, Any]:
    return dict(observation.data) if observation.data is not None else {}


class StatusService:
    """Controller facade. It deliberately exposes no mutation method."""

    def __init__(
        self,
        config: AppConfig,
        registry_reader: RegistryReader | None = None,
        git_reader: GitReader | None = None,
        beads_reader: BeadsReader | None = None,
        github_reader: GitHubReader | None = None,
        openclaw_reader: OpenClawReader | None = None,
    ) -> None:
        self.config = config
        runner = BoundedCommandRunner(
            config.commands.timeout_seconds,
            config.commands.stdout_limit_bytes,
            config.commands.stderr_limit_bytes,
        )
        self.registry_reader = registry_reader or RegistryReader(config)
        self.git_reader = git_reader or GitReader(
            config.commands.git, runner, cache_ttl_seconds=config.cache_ttl_seconds
        )
        self.beads_reader = beads_reader or BeadsReader(
            runner, cache_ttl_seconds=config.cache_ttl_seconds
        )
        self.github_reader = github_reader or GitHubReader(
            config.commands.gh, runner, cache_ttl_seconds=config.cache_ttl_seconds
        )
        self.openclaw_reader = openclaw_reader or OpenClawReader(
            config.commands.openclaw,
            runner,
            cache_ttl_seconds=config.cache_ttl_seconds,
            working_directory=config.blueprint_root,
        )

    @classmethod
    def from_config(cls, config: AppConfig | str | Path) -> "StatusService":
        loaded = config if isinstance(config, AppConfig) else load_app_config(config)
        return cls(loaded)

    def _registry(self) -> ProjectRegistry:
        return self.registry_reader.load()

    @staticmethod
    def _agent_index(observation: SourceObservation) -> dict[str, Mapping[str, Any]]:
        agents = _data(observation).get("agents", [])
        if not isinstance(agents, list):
            return {}
        return {
            str(agent["id"]): agent
            for agent in agents
            if isinstance(agent, Mapping) and isinstance(agent.get("id"), str)
        }

    def _project(
        self,
        project: ProjectRecord,
        agent_observation: SourceObservation | None = None,
        session_observation: SourceObservation | None = None,
    ) -> dict[str, Any]:
        git = self.git_reader.read(project)
        beads = self.beads_reader.read(project)
        github = self.github_reader.read(project)
        agents = agent_observation or self.openclaw_reader.cached_agents()
        agent_index = self._agent_index(agents)
        agent_present = project.agent_id in agent_index if agents.data is not None else None
        session = (
            self.openclaw_reader.session_for_agent(
                project.agent_id,
                session_observation or self.openclaw_reader.cached_agent_sessions(),
            )
            if agent_present
            else SourceObservation.unknown("openclaw_sessions", "registered agent is not present")
        )

        git_data = _data(git)
        beads_data = _data(beads)
        github_data = _data(github)
        session_data = _data(session)
        confirmations: list[str] = []
        if project.policies.public_publish == "approval_required":
            confirmations.append("Explicit approval is required before public publication.")
        if git_data.get("branch") and git_data.get("branch") != project.git.active_branch:
            confirmations.append("The observed Git branch differs from the registered active branch.")
        if git_data.get("clean") is False:
            confirmations.append("The project worktree is dirty; review is required before further operations.")

        progress_percent = beads_data.get("progress_percent")
        progress = (
            {
                "kind": "task_ratio",
                "percent": progress_percent,
                "completed": beads_data.get("completed_count"),
                "total": beads_data.get("list_count"),
                "label": project.phase,
            }
            if progress_percent is not None
            else {"kind": "phase", "percent": None, "label": project.phase}
        )
        counts = beads_data.get("counts") if isinstance(beads_data.get("counts"), Mapping) else {}
        return {
            "id": project.id,
            "name": project.name,
            "type": project.type,
            "lifecycle": project.lifecycle,
            "phase": project.phase,
            "agent_id": project.agent_id,
            "workspace": str(project.workspace),
            "registry": {
                "id": project.id,
                "name": project.name,
                "type": project.type,
                "lifecycle": project.lifecycle,
                "phase": project.phase,
                "agent_id": project.agent_id,
                "draft_pr": project.git.draft_pr,
            },
            "git": git_data,
            "beads": beads_data,
            "github": {"pull_request": github_data} if github_data else {},
            "registered_active_branch": project.git.active_branch,
            "canonical_branch": project.git.canonical_branch,
            "branch": git_data.get("branch"),
            "head": git_data.get("head"),
            "git_clean": git_data.get("clean"),
            "git_status": git_data.get("status", "UNKNOWN"),
            "upstream": git_data.get("upstream"),
            "ahead": git_data.get("ahead"),
            "behind": git_data.get("behind"),
            "recent_commit": git_data.get("latest_commit"),
            "beads_version": beads_data.get("version"),
            "beads_list": beads_data.get("list"),
            "beads_ready": beads_data.get("ready"),
            "task_counts": {
                "open": counts.get("open") if counts else None,
                "ready": counts.get("ready") if counts else None,
                "blocked": counts.get("blocked") if counts else None,
                "total": beads_data.get("list_count"),
            },
            "progress": progress,
            "pull_request": github_data or None,
            "agent_exists": agent_present,
            "recent_session_at": session_data.get("recent_session_at"),
            "user_confirmation_required": confirmations,
            "last_status_query_at": utc_now_iso(),
            "sources": {
                "git": git.as_dict(),
                "beads": beads.as_dict(),
                "github": github.as_dict(),
                "openclaw_agents": agents.as_dict(),
                "openclaw_sessions": session.as_dict(),
            },
        }

    def projects(self) -> list[dict[str, Any]]:
        registry = self._registry()
        agents = self.openclaw_reader.cached_agents()
        sessions = self.openclaw_reader.cached_agent_sessions()
        return [self._project(project, agents, sessions) for project in registry.projects]

    def project(self, project_id: str) -> dict[str, Any] | None:
        registry = self._registry()
        project = registry.get(project_id)
        if project is None:
            return None
        return self._project(
            project,
            self.openclaw_reader.cached_agents(),
            self.openclaw_reader.cached_agent_sessions(),
        )

    def _openclaw_live_pair(self) -> tuple[SourceObservation, SourceObservation]:
        """Fetch agent and session summaries concurrently with a small UI budget."""

        timeout_seconds = float(getattr(self.openclaw_reader, "ui_timeout_seconds", 7.0))
        deadline = time.monotonic() + timeout_seconds + 0.5
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-ops-openclaw")
        futures = {
            "agents": executor.submit(self.openclaw_reader.list_agents),
            "sessions": executor.submit(self.openclaw_reader.list_agent_sessions),
        }

        def result(
            name: str,
            fallback: Callable[[str], SourceObservation],
        ) -> SourceObservation:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                return futures[name].result(timeout=remaining)
            except FutureTimeout:
                return fallback(f"OpenClaw {name} lookup exceeded the UI budget")
            except Exception as exc:
                return fallback(redact_text(str(exc) or exc.__class__.__name__))

        try:
            agents = result("agents", self.openclaw_reader.cached_agents)
            sessions = result("sessions", self.openclaw_reader.cached_agent_sessions)
            return agents, sessions
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def agents(self) -> list[dict[str, Any]]:
        registry = self._registry()
        observation, sessions = self._openclaw_live_pair()
        source_agents = self._agent_index(observation)
        projects_by_agent: dict[str, list[str]] = {}
        for project in registry.projects:
            projects_by_agent.setdefault(project.agent_id, []).append(project.id)

        results: list[dict[str, Any]] = []
        for agent_id, agent in sorted(source_agents.items()):
            session = self.openclaw_reader.session_for_agent(agent_id, sessions)
            session_data = _data(session)
            is_default = bool(agent.get("is_default"))
            explicitly_legacy = agent_id in self.config.openclaw.legacy_agents
            assigned = projects_by_agent.get(agent_id, [])
            role = "PROJECT_LEAD" if assigned else "UNREGISTERED"
            if agent_id == self.config.openclaw.manager_agent_id:
                role = "MANAGER"
            elif not assigned and (is_default or explicitly_legacy):
                role = "LEGACY"
            results.append(
                {
                    "id": agent_id,
                    "name": agent.get("name"),
                    "model": agent.get("model"),
                    "exists": True,
                    "is_default": is_default,
                    "role": role,
                    "projects": assigned,
                    "recent_session_at": session_data.get("recent_session_at"),
                    "availability": observation.availability,
                    "session_source": session.as_dict(),
                }
            )

        for agent_id, project_ids in sorted(projects_by_agent.items()):
            if agent_id not in source_agents:
                results.append(
                    {
                        "id": agent_id,
                        "name": None,
                        "model": None,
                        "exists": False if observation.data is not None else None,
                        "is_default": False,
                        "role": "PROJECT_LEAD",
                        "projects": project_ids,
                        "recent_session_at": None,
                        "availability": observation.availability,
                        "session_source": None,
                    }
                )
        return results

    def activity(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for project in self.projects():
            commit = project.get("recent_commit")
            if isinstance(commit, Mapping) and commit.get("committed_at"):
                events.append(
                    {
                        "kind": "git_commit",
                        "project_id": project["id"],
                        "at": commit.get("committed_at"),
                        "summary": commit.get("subject"),
                        "revision": commit.get("sha"),
                    }
                )
            pull_request = project.get("pull_request")
            if isinstance(pull_request, Mapping) and pull_request.get("updated_at"):
                events.append(
                    {
                        "kind": "pull_request",
                        "project_id": project["id"],
                        "at": pull_request.get("updated_at"),
                        "summary": f"PR #{pull_request.get('number')} {pull_request.get('display_state')}",
                        "url": pull_request.get("url"),
                    }
                )
        for agent in self.agents():
            if agent.get("recent_session_at"):
                events.append(
                    {
                        "kind": "agent_session",
                        "agent_id": agent["id"],
                        "at": agent["recent_session_at"],
                        "summary": "Recent agent activity",
                    }
                )
        return sorted(events, key=lambda event: str(event.get("at") or ""), reverse=True)

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            registry = self._registry()
            checks.append({"name": "registry", "status": "PASS", "projects": len(registry.projects)})
        except Exception as exc:
            checks.append(
                {"name": "registry", "status": "FAIL", "detail": redact_text(str(exc))}
            )
        for name, path in (
            ("knowledge_root", self.config.knowledge_root),
            ("blueprint_root", self.config.blueprint_root),
        ):
            checks.append({"name": name, "status": "PASS" if path.is_dir() else "FAIL"})
        for name, path in (
            ("git", self.config.commands.git),
            ("gh", self.config.commands.gh),
            ("openclaw", self.config.commands.openclaw),
        ):
            checks.append({"name": name, "status": "PASS" if path.is_file() else "FAIL"})
        return {
            "status": "PASS" if all(check["status"] == "PASS" for check in checks) else "FAIL",
            "read_only": True,
            "checks": checks,
            "checked_at": utc_now_iso(),
        }
