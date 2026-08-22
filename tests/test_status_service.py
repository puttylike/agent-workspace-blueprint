from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock

import pytest

from agent_workspace.config import load_app_config
from agent_workspace.controller.github_reader import GitHubReader
from agent_workspace.controller.openclaw_reader import OpenClawReader
from agent_workspace.controller.status_service import StatusService
from agent_workspace.models import (
    AppConfig,
    CommandConfig,
    CommandResult,
    ConfigurationError,
    ExpectedAgentConfig,
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

    def cached_agents(self):
        return self.list_agents()

    def list_agent_sessions(self):
        return SourceObservation.success(
            "openclaw_sessions",
            {
                "sessions_by_agent": {
                    agent_id: {
                        "agent_id": agent_id,
                        "recent_session_at": "2030-01-01T00:00:00Z",
                        "session_count": 1,
                    }
                    for agent_id in ("sample-lead", "workspace-manager", "legacy-default")
                }
            },
        )

    def cached_agent_sessions(self):
        return self.list_agent_sessions()

    def session_for_agent(self, agent_id: str, sessions: SourceObservation | None = None):
        observation = sessions or self.list_agent_sessions()
        data = dict(observation.data or {})
        by_agent = data.get("sessions_by_agent", {})
        return SourceObservation.success(
            "openclaw_sessions",
            by_agent.get(
                agent_id,
                {"agent_id": agent_id, "recent_session_at": None, "session_count": 0},
            ),
        )


class MethodRunner:
    def __init__(self, by_method: dict[str, CommandResult], *, delay: float = 0.0) -> None:
        self.by_method = dict(by_method)
        self.delay = delay
        self.calls: list[tuple[list[str], Path]] = []
        self.timeout_seconds = 10.0
        self.stdout_limit_bytes = 65_536
        self.stderr_limit_bytes = 16_384
        self._lock = Lock()

    def run(self, argv: list[str], *, cwd: Path) -> CommandResult:
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls.append((list(argv), Path(cwd)))
        method = argv[3]
        if method not in self.by_method:
            raise AssertionError(f"unexpected command invocation: {method}")
        return self.by_method[method]


def _config(
    tmp_path: Path,
    *,
    expected_agents: tuple[ExpectedAgentConfig, ...] = (),
) -> AppConfig:
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
            "local_gateway_rpc", ("legacy-default",), "workspace-manager", expected_agents
        ),
        source_path=tmp_path / "agent-ops.yaml",
    )


def _config_yaml(tmp_path: Path, expected_yaml: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project_registry = tmp_path / "projects.yaml"
    project_registry.write_text("version: 1\nprojects: []\n", encoding="utf-8")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    runtime = tmp_path / "runtime.db"
    config = tmp_path / "agent-ops.yaml"
    config.write_text(
        f"""
version: 1
mode: read_only
listen_host: 127.0.0.1
listen_port: 3001
project_registry: {project_registry}
knowledge_root: {knowledge}
public_blueprint_path: {tmp_path}
runtime_sqlite_cache: {runtime}
commands:
  git: /bin/true
  gh: /bin/true
  openclaw: /bin/true
openclaw:
  access_method: local_gateway_rpc
{expected_yaml}
""".lstrip(),
        encoding="utf-8",
    )
    return config


def test_expected_agents_yaml_parsing_and_role_validation(tmp_path: Path) -> None:
    config = load_app_config(
        _config_yaml(
            tmp_path,
            """
  expected_agents:
    - id: sample-lead
      display_name: Sample Lead
      role: PROJECT_LEAD
    - id: workspace-builder
      display_name: Workspace Builder
      role: BUILDER
""",
        )
    )

    assert [agent.id for agent in config.openclaw.expected_agents] == [
        "sample-lead",
        "workspace-builder",
    ]
    assert [agent.role for agent in config.openclaw.expected_agents] == [
        "PROJECT_LEAD",
        "BUILDER",
    ]
    assert config.openclaw.expected_agents[0].display_name == "Sample Lead"


def test_expected_agents_reject_duplicate_ids_and_invalid_roles(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="duplicate"):
        load_app_config(
            _config_yaml(
                tmp_path,
                """
  expected_agents:
    - id: sample-lead
      role: PROJECT_LEAD
    - id: sample-lead
      role: BUILDER
""",
            )
        )

    with pytest.raises(ConfigurationError, match="uppercase role"):
        load_app_config(
            _config_yaml(
                tmp_path / "invalid-role",
                """
  expected_agents:
    - id: sample-lead
      role: project lead
""",
            )
        )


def test_public_fixtures_do_not_contain_deployment_agent_ids() -> None:
    fixture_root = Path(__file__).parent / "fixtures"
    forbidden = {
        "".join(("m", "a", "i", "n")),
        "".join(("m", "a", "n", "a", "g", "e", "r")),
        "".join(("o", "p", "s", "-", "b", "u", "i", "l", "d", "e", "r")),
        "".join(("p", "a", "p", "e", "r")),
    }
    for path in fixture_root.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for agent_id in forbidden:
            assert f'"{agent_id}"' not in text
            assert f"'{agent_id}'" not in text


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


def test_status_service_fetches_all_sessions_once_for_four_agents(tmp_path, sample_project) -> None:
    agents_payload = {
        "defaultId": "legacy-default",
        "agents": [
            {"id": "legacy-default", "name": "Legacy Default"},
            {"id": "workspace-manager", "name": "Workspace Manager"},
            {"id": "workspace-builder", "name": "Workspace Builder"},
            {"id": "sample-lead", "name": "Sample Lead"},
        ],
    }
    sessions_payload = {
        "sessions": [
            {"agentRuntime": {"id": "sample-lead"}, "updatedAt": 1_893_456_000_000},
            {"agentRuntime": {"id": "sample-lead"}, "updatedAt": 1_893_456_100_000},
            {"agentRuntime": {"id": "workspace-manager"}, "updatedAt": 1_893_456_050_000},
            {"agentRuntime": {"id": "workspace-builder"}, "updatedAt": 1_893_456_025_000},
        ]
    }
    runner = MethodRunner(
        {
            "agents.list": CommandResult(0, json.dumps(agents_payload), ""),
            "sessions.list": CommandResult(0, json.dumps(sessions_payload), ""),
        }
    )
    reader = OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0)
    service = StatusService(
        _config(tmp_path),
        registry_reader=StubRegistry(sample_project),
        openclaw_reader=reader,
    )

    agents = service.agents()

    assert {agent["id"] for agent in agents} == {
        "legacy-default",
        "workspace-manager",
        "workspace-builder",
        "sample-lead",
    }
    assert next(agent for agent in agents if agent["id"] == "sample-lead")[
        "recent_session_at"
    ] == "2030-01-01T00:01:40Z"
    methods = [call[0][3] for call in runner.calls]
    assert methods.count("agents.list") == 1
    assert methods.count("sessions.list") == 1
    assert all("agentId" not in call[0][6] for call in runner.calls if call[0][3] == "sessions.list")


def test_openclaw_groups_all_sessions_by_recent_agent_activity() -> None:
    payload = {
        "sessions": [
            {"agentRuntime": {"id": "sample-lead"}, "updatedAt": 1_893_456_000_000},
            {"agentRuntime": {"id": "sample-lead"}, "lastActivityAt": 1_893_456_500_000},
            {"agentRuntime": {"id": "workspace-manager"}, "updatedAt": 1_893_456_100_000},
            {
                "agentRuntime": {"id": "sample-lead"},
                "key": ":".join(("agent", "sample-lead", "dashboard", "must-not-escape")),
                "updatedAt": 1_893_456_250_000,
            },
        ]
    }
    runner = MethodRunner({"sessions.list": CommandResult(0, json.dumps(payload), "")})
    reader = OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0)

    sessions = reader.list_agent_sessions()
    sample = reader.session_for_agent("sample-lead", sessions)
    manager = reader.session_for_agent("workspace-manager", sessions)
    serialized = json.dumps(sessions.as_dict())

    assert sample.data == {
        "agent_id": "sample-lead",
        "recent_session_at": "2030-01-01T00:08:20Z",
        "session_count": 3,
    }
    assert manager.data["recent_session_at"] == "2030-01-01T00:01:40Z"
    assert "must-not-escape" not in serialized
    assert len(runner.calls) == 1


def test_agents_list_timeout_returns_registered_agents_without_live_source(tmp_path, sample_project) -> None:
    runner = MethodRunner(
        {
            "agents.list": CommandResult(1, "", "", timed_out=True),
            "sessions.list": CommandResult(1, "", "", timed_out=True),
        }
    )
    service = StatusService(
        _config(tmp_path),
        registry_reader=StubRegistry(sample_project),
        openclaw_reader=OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0),
    )

    agents = service.agents()

    assert agents.meta["availability"] == "UNAVAILABLE"
    assert [agent["id"] for agent in agents] == ["sample-lead"]
    assert agents[0]["exists"] is None
    assert agents[0]["role"] == "PROJECT_LEAD"
    assert agents[0]["projects"] == ["sample-paper"]
    assert agents[0]["recent_session_at"] is None
    assert agents[0]["live_confirmed"] is False
    assert agents[0]["session_source"]["availability"] == "UNAVAILABLE"


def test_live_agents_and_expected_roster_are_unioned(tmp_path, sample_project) -> None:
    expected = (
        ExpectedAgentConfig("sample-lead", "Configured Lead", "PROJECT_LEAD"),
        ExpectedAgentConfig("workspace-builder", "Workspace Builder", "BUILDER"),
    )
    runner = MethodRunner(
        {
            "agents.list": CommandResult(
                0,
                json.dumps(
                    {
                        "agents": [
                            {"id": "sample-lead", "name": "Runtime Lead"},
                            {"id": "runtime-extra", "name": "Runtime Extra"},
                        ]
                    }
                ),
                "",
            ),
            "sessions.list": CommandResult(0, json.dumps({"sessions": []}), ""),
        }
    )
    service = StatusService(
        _config(tmp_path, expected_agents=expected),
        registry_reader=StubRegistry(sample_project),
        openclaw_reader=OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0),
    )

    agents = service.agents()

    assert agents.meta["availability"] == "AVAILABLE"
    assert [agent["id"] for agent in agents] == [
        "runtime-extra",
        "sample-lead",
        "workspace-builder",
    ]
    assert len({agent["id"] for agent in agents}) == len(agents)
    sample = next(agent for agent in agents if agent["id"] == "sample-lead")
    assert sample["name"] == "Configured Lead"
    assert sample["role"] == "PROJECT_LEAD"
    assert sample["live_confirmed"] is True
    builder = next(agent for agent in agents if agent["id"] == "workspace-builder")
    assert builder["role"] == "BUILDER"
    assert builder["exists"] is False
    assert next(agent for agent in agents if agent["id"] == "runtime-extra")["role"] == "UNREGISTERED"


def test_live_timeout_preserves_expected_and_project_roster(tmp_path, sample_project) -> None:
    expected = (
        ExpectedAgentConfig("legacy-default", "Legacy Default", "LEGACY"),
        ExpectedAgentConfig("workspace-manager", "Workspace Manager", "MANAGER"),
        ExpectedAgentConfig("sample-lead", "Configured Lead", "PROJECT_LEAD"),
        ExpectedAgentConfig("workspace-builder", "Workspace Builder", "BUILDER"),
    )
    runner = MethodRunner(
        {
            "agents.list": CommandResult(1, "", "", timed_out=True),
            "sessions.list": CommandResult(1, "", "", timed_out=True),
        }
    )
    service = StatusService(
        _config(tmp_path, expected_agents=expected),
        registry_reader=StubRegistry(sample_project),
        openclaw_reader=OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0),
    )

    agents = service.agents()
    by_id = {agent["id"]: agent for agent in agents}

    assert agents.meta["availability"] == "UNAVAILABLE"
    assert agents.meta["stale"] is False
    assert agents.meta["message"]
    assert list(by_id) == [
        "legacy-default",
        "sample-lead",
        "workspace-builder",
        "workspace-manager",
    ]
    assert len(by_id) == len(agents)
    assert {agent["role"] for agent in agents} == {
        "LEGACY",
        "MANAGER",
        "PROJECT_LEAD",
        "BUILDER",
    }
    assert all(agent["exists"] is None for agent in agents)
    assert all(agent["live_confirmed"] is False for agent in agents)
    assert all(agent["recent_session_at"] is None for agent in agents)
    assert all(agent["session_source"]["availability"] == "UNAVAILABLE" for agent in agents)


def test_agent_roster_stale_cache_sets_meta_last_success_at(tmp_path, sample_project) -> None:
    runner = MethodRunner(
        {
            "agents.list": CommandResult(
                0,
                json.dumps({"agents": [{"id": "sample-lead", "name": "Sample Lead"}]}),
                "",
            ),
            "sessions.list": CommandResult(0, json.dumps({"sessions": []}), ""),
        }
    )
    reader = OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0)
    service = StatusService(
        _config(tmp_path),
        registry_reader=StubRegistry(sample_project),
        openclaw_reader=reader,
    )
    first = service.agents()

    runner.by_method = {
        "agents.list": CommandResult(1, "", "gateway unavailable"),
        "sessions.list": CommandResult(1, "", "gateway unavailable"),
    }
    second = service.agents()

    assert first.meta["availability"] == "AVAILABLE"
    assert second.meta["availability"] == "UNAVAILABLE"
    assert second.meta["stale"] is True
    assert second.meta["last_success_at"] == first.meta["observed_at"]
    assert [agent["id"] for agent in second] == ["sample-lead"]


def test_sessions_timeout_keeps_agent_list_and_marks_sessions_unavailable(tmp_path, sample_project) -> None:
    runner = MethodRunner(
        {
            "agents.list": CommandResult(
                0,
                json.dumps({"agents": [{"id": "sample-lead", "name": "Sample Lead"}]}),
                "",
            ),
            "sessions.list": CommandResult(1, "", "", timed_out=True),
        }
    )
    service = StatusService(
        _config(tmp_path),
        registry_reader=StubRegistry(sample_project),
        openclaw_reader=OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0),
    )

    agents = service.agents()

    assert agents[0]["id"] == "sample-lead"
    assert agents[0]["recent_session_at"] is None
    assert agents[0]["session_source"]["availability"] == "UNAVAILABLE"


def test_openclaw_last_success_cache_exposes_stale_timestamp() -> None:
    runner = FakeRunner(
        [
            CommandResult(
                0,
                json.dumps(
                    {
                        "sessions": [
                            {"agentRuntime": {"id": "sample-lead"}, "updatedAt": 1_893_456_000_000}
                        ]
                    }
                ),
                "",
            ),
            CommandResult(1, "", "gateway unavailable"),
        ]
    )
    reader = OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0)

    first = reader.list_agent_sessions()
    second = reader.list_agent_sessions()
    rendered = second.as_dict()

    assert first.availability == "AVAILABLE"
    assert second.availability == "UNAVAILABLE"
    assert second.stale is True
    assert rendered["observed_at"] == first.observed_at
    assert second.data == first.data


def test_malformed_session_items_do_not_fail_agent_endpoint(tmp_path, sample_project) -> None:
    runner = MethodRunner(
        {
            "agents.list": CommandResult(
                0,
                json.dumps(
                    {
                        "agents": [
                            {"id": "sample-lead", "name": "Sample Lead"},
                            {"id": "../bad", "name": "Bad"},
                        ]
                    }
                ),
                "",
            ),
            "sessions.list": CommandResult(
                0,
                json.dumps({"sessions": ["bad", {"agentRuntime": {"id": "../bad"}}]}),
                "",
            ),
        }
    )
    service = StatusService(
        _config(tmp_path),
        registry_reader=StubRegistry(sample_project),
        openclaw_reader=OpenClawReader(Path("/bin/true"), runner, cache_ttl_seconds=0),
    )

    agents = service.agents()

    assert [agent["id"] for agent in agents] == ["sample-lead"]
    assert agents[0]["recent_session_at"] is None
