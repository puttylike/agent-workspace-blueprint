from __future__ import annotations

import json
from pathlib import Path

from agent_workspace.controller.github_reader import GitHubReader
from agent_workspace.controller.openclaw_reader import OpenClawReader
from agent_workspace.controller.status_service import StatusService
from agent_workspace.models import (
    AppConfig,
    CommandConfig,
    CommandResult,
    OpenClawConfig,
    ProjectRegistry,
    RuntimeConfig,
    SourceObservation,
)

from conftest import FakeRunner


class StubRegistry:
    def __init__(self, project) -> None:
        self.value = ProjectRegistry(1, (project,))

    def load(self):
        return self.value


class StubProjectReader:
    def __init__(self, source: str, data: dict) -> None:
        self.observation = SourceObservation.success(source, data)

    def read(self, project):
        return self.observation


class StubOpenClaw:
    def list_agents(self):
        return SourceObservation.success(
            "openclaw_agents",
            {
                "agents": [
                    {"id": "sample-lead", "name": "Sample Lead", "is_default": False},
                    {"id": "workspace-manager", "name": "Workspace Manager", "is_default": False},
                    {"id": "legacy-default", "name": None, "is_default": True},
                ]
            },
        )

    def recent_session(self, agent_id: str):
        return SourceObservation.success(
            "openclaw_sessions",
            {"agent_id": agent_id, "recent_session_at": "2030-01-01T00:00:00Z"},
        )


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        version=1,
        mode="read_only",
        listen_host="127.0.0.1",
        listen_port=3001,
        project_registry=tmp_path / "projects.yaml",
        knowledge_root=tmp_path,
        blueprint_root=tmp_path,
        runtime=RuntimeConfig(tmp_path / "runtime" / "cache.db"),
        commands=CommandConfig(Path("/usr/bin/git"), Path("/usr/bin/gh"), Path("/bin/true")),
        cache_ttl_seconds=0,
        openclaw=OpenClawConfig(
            "local_gateway_rpc", ("legacy-default",), "workspace-manager"
        ),
        source_path=tmp_path / "agent-ops.yaml",
    )


def test_status_service_uses_phase_when_total_tasks_zero(tmp_path, sample_project) -> None:
    service = StatusService(
        _config(tmp_path),
        registry_reader=StubRegistry(sample_project),
        git_reader=StubProjectReader(
            "git",
            {
                "branch": "research/draft",
                "head": "a" * 40,
                "clean": True,
                "status": "CLEAN",
                "ahead": 0,
                "behind": 0,
            },
        ),
        beads_reader=StubProjectReader(
            "beads",
            {
                "version": "1.2.2",
                "list": [],
                "ready": [],
                "list_count": 0,
                "ready_count": 0,
                "counts": {"open": 0, "ready": 0, "blocked": 0},
                "completed_count": 0,
                "progress_percent": None,
            },
        ),
        github_reader=StubProjectReader(
            "github",
            {"number": 7, "state": "OPEN", "is_draft": True, "display_state": "DRAFT"},
        ),
        openclaw_reader=StubOpenClaw(),
    )

    project = service.project("sample-paper")
    assert project is not None
    assert project["phase"] == "WRITING"
    assert project["progress"] == {"kind": "phase", "percent": None, "label": "WRITING"}
    assert project["task_counts"] == {"open": 0, "ready": 0, "blocked": 0, "total": 0}
    assert project["git_clean"] is True
    assert project["agent_exists"] is True

    agents = service.agents()
    assert {agent["id"] for agent in agents} == {
        "sample-lead",
        "workspace-manager",
        "legacy-default",
    }
    assert next(agent for agent in agents if agent["id"] == "workspace-manager")["role"] == "MANAGER"
    assert next(agent for agent in agents if agent["id"] == "legacy-default")["role"] == "LEGACY"


def test_github_5xx_uses_last_success_without_retry(sample_project) -> None:
    success = CommandResult(
        0,
        json.dumps(
            {
                "number": 7,
                "state": "OPEN",
                "isDraft": True,
                "url": "https://example.invalid/pull/7",
                "updatedAt": "2030-01-01T00:00:00Z",
                "headRefName": "research/draft",
                "baseRefName": "agent/canonical",
                "mergeStateStatus": "CLEAN",
            }
        ),
        "",
    )
    runner = FakeRunner([success, CommandResult(1, "", "HTTP 503 upstream unavailable")])
    reader = GitHubReader(Path("/usr/bin/gh"), runner, cache_ttl_seconds=0)
    first = reader.read(sample_project)
    second = reader.read(sample_project)
    assert first.availability == "AVAILABLE"
    assert second.availability == "UNAVAILABLE"
    assert second.stale is True
    assert second.data == first.data
    assert "UPSTREAM_5XX" in str(second.error)
    assert len(runner.calls) == 2


def test_github_5xx_is_throttled_without_automatic_retry(sample_project) -> None:
    runner = FakeRunner([CommandResult(1, "", "HTTP 503 upstream unavailable")])
    reader = GitHubReader(Path("/usr/bin/gh"), runner, cache_ttl_seconds=30)
    first = reader.read(sample_project)
    second = reader.read(sample_project)
    assert first.availability == "UNAVAILABLE"
    assert second.availability == "UNAVAILABLE"
    assert second.cache_hit is True
    assert len(runner.calls) == 1


def test_openclaw_failure_falls_back_and_session_keys_never_escape() -> None:
    agents_payload = {
        "defaultId": "legacy-default",
        "agents": [{"id": "sample-lead", "name": "Sample Lead"}],
    }
    runner = FakeRunner(
        [
            CommandResult(0, json.dumps(agents_payload), ""),
            CommandResult(1, "", "gateway unavailable"),
        ]
    )
    reader = OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0)
    first = reader.list_agents()
    second = reader.list_agents()
    assert first.availability == "AVAILABLE"
    assert second.availability == "UNAVAILABLE"
    assert second.stale is True
    assert second.data == first.data
    assert len(runner.calls) == 2

    session_runner = FakeRunner(
        [
            CommandResult(
                0,
                json.dumps(
                    {
                        "sessions": [
                            {
                                "key": ":".join(
                                    ("agent", "sample-lead", "dashboard", "opaque")
                                ),
                                "updatedAt": 1_893_456_000_000,
                            }
                        ]
                    }
                ),
                "",
            )
        ]
    )
    session = OpenClawReader(Path("/bin/true"), session_runner).recent_session("sample-lead")
    serialized = json.dumps(session.as_dict())
    assert "opaque" not in serialized
    assert "session" not in serialized.lower() or "openclaw_sessions" in serialized
