from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from agent_workspace.cli import main
from agent_workspace.config import load_app_config
from agent_workspace.controller.project_planner import (
    PROJECT_TYPES,
    ProjectPlanError,
    ProjectPlanRequest,
    plan_project,
    slugify,
)
from agent_workspace.models import ConfigurationError


def _venture_defaults_yaml(*, executable: str = "null") -> str:
    return f"""version: 1
venture_defaults:
  monetization:
    default_strategy: affiliate_first
  monthly_incremental_budget:
    currency: KRW
    amount: 0
  lifecycle:
    - DISCOVER
    - VALIDATE
    - FRONTIER_PROTOTYPE
    - NORMALIZE
    - LOCAL_PARITY
    - SHADOW_RUN
    - AUTOMATED
    - MONITORING
    - SCALE
    - RETIRE
  initial_phase: DISCOVER
  local_runner:
    required: true
    required_by_phase: LOCAL_PARITY
    executable: {executable}
  approval_gates:
    - external_publishing
    - account_creation
    - payment
    - affiliate_application
    - production_deployment
  prohibited_actions:
    - live_trading
  evidence:
    actual_metrics: observed_only
"""


def _write_config(tmp_path: Path) -> Path:
    workspace = tmp_path / "schema-paper"
    workspace.mkdir()
    (workspace / ".beads").mkdir()
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    runtime = tmp_path / "runtime.db"
    beads = tmp_path / "bd"
    beads.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    beads.chmod(0o700)
    venture_defaults = tmp_path / "venture-defaults.yaml"
    venture_defaults.write_text(_venture_defaults_yaml(), encoding="utf-8")
    registry = tmp_path / "projects.yaml"
    registry.write_text(
        f"""version: 1
projects:
  - id: paper-schema-virtualization
    name: Schema Virtualization Paper
    type: paper
    lifecycle: active
    phase: WRITING
    agent_id: paper
    workspace: {workspace}
    git:
      repository: example/schema-paper
      canonical_branch: main
      active_branch: research/draft
      draft_pr: 17
    beads:
      binary: {beads}
      directory: {workspace / '.beads'}
    policies:
      manager_write_access: false
      direct_merge: false
      public_publish: approval_required
""",
        encoding="utf-8",
    )
    config = tmp_path / "agent-ops.yaml"
    config.write_text(
        f"""version: 1
mode: read_only
listen_host: 127.0.0.1
listen_port: 3001
project_registry: {registry}
knowledge_root: {knowledge}
public_blueprint_path: {tmp_path}
runtime_sqlite_cache: {runtime}
commands:
  git: /bin/true
  gh: /bin/true
  openclaw: /bin/true
openclaw:
  access_method: local_gateway_rpc
  manager_agent_id: manager
  legacy_agents:
    - main
  expected_agents:
    - id: paper
      display_name: Paper Lead
      role: PROJECT_LEAD
venture_defaults: {venture_defaults}
""",
        encoding="utf-8",
    )
    return config


def _plan(
    tmp_path: Path,
    *,
    name: str = "Blog Automation",
    project_type: str = "blog",
    goal: str = "Automate topic research and content production.",
    visibility: str = "private",
) -> dict:
    config = load_app_config(_write_config(tmp_path))
    return plan_project(
        config,
        ProjectPlanRequest(
            name=name,
            project_type=project_type,
            goal=goal,
            visibility=visibility,
        ),
    )


@pytest.mark.parametrize("project_type", PROJECT_TYPES)
def test_project_types_build_normal_plan(tmp_path: Path, project_type: str) -> None:
    goal = "Build a prototype" if project_type == "contest" else "Long-running project research"
    result = _plan(tmp_path, project_type=project_type, goal=goal)

    assert result["type"] == project_type
    assert result["executable"] is False
    assert result["proposed_lead"]["role"] == "PROJECT_LEAD"
    assert result["duplicate_decision"]["create_new"] is True


def test_default_visibility_is_private(tmp_path: Path) -> None:
    config = load_app_config(_write_config(tmp_path))

    result = plan_project(
        config,
        ProjectPlanRequest(
            name="Private App",
            project_type="app",
            goal="Build a small application.",
        ),
    )

    assert result["visibility"] == "private"
    assert result["proposed_github_repository"]["private"] is True


def test_public_visibility_requires_approval(tmp_path: Path) -> None:
    result = _plan(tmp_path, visibility="public")

    assert result["visibility"] == "public"
    assert any("Public visibility" in item for item in result["warnings"])
    assert any("public" in item.lower() for item in result["approvals_required"])


def test_slug_normalization() -> None:
    assert slugify(" Blog Automation!! ") == "blog-automation"
    assert slugify("QUANT 2026 Alpha") == "quant-2026-alpha"


def test_invalid_type_is_rejected(tmp_path: Path) -> None:
    config = load_app_config(_write_config(tmp_path))

    with pytest.raises(ProjectPlanError):
        plan_project(
            config,
            ProjectPlanRequest(
                name="Unsupported",
                project_type="invalid",
                goal="Do something.",
            ),
        )


def test_path_traversal_input_is_rejected(tmp_path: Path) -> None:
    config = load_app_config(_write_config(tmp_path))

    with pytest.raises(ProjectPlanError):
        plan_project(
            config,
            ProjectPlanRequest(
                name="../escape",
                project_type="misc",
                goal="Do something.",
            ),
        )


def test_duplicate_project_detection(tmp_path: Path) -> None:
    result = _plan(
        tmp_path,
        name="schema-paper",
        project_type="paper",
        goal="Continue the current paper.",
    )

    assert result["duplicate_decision"]["create_new"] is False
    assert result["duplicate_decision"]["matched_project_id"] == "paper-schema-virtualization"


def test_existing_paper_similarity_is_reported(tmp_path: Path) -> None:
    result = _plan(
        tmp_path,
        name="Schema Virtualization Study",
        project_type="paper",
        goal="Write another schema virtualization paper.",
    )

    assert result["duplicate_decision"]["create_new"] is False
    assert result["duplicate_candidates"][0]["project_id"] == "paper-schema-virtualization"


def test_secret_like_input_is_rejected(tmp_path: Path) -> None:
    config = load_app_config(_write_config(tmp_path))

    with pytest.raises(ProjectPlanError):
        plan_project(
            config,
            ProjectPlanRequest(
                name="Secret Blog",
                project_type="blog",
                goal="Use token abc123 in the plan.",
            ),
        )


def test_quant_policy_forbids_live_trading_without_approval(tmp_path: Path) -> None:
    result = _plan(tmp_path, project_type="quant", goal="Research a strategy.")

    assert result["beads"]["enabled"] is True
    assert any("Live trading" in item for item in result["approvals_required"])
    assert any("broker accounts" in item for item in result["warnings"])


def test_blog_policy_requires_external_publishing_approval(tmp_path: Path) -> None:
    result = _plan(
        tmp_path,
        project_type="blog",
        goal="Automate a long-running content pipeline.",
    )

    assert result["beads"]["enabled"] is True
    assert any("external publishing" in item for item in result["approvals_required"])


def test_venture_defaults_parse_private_config(tmp_path: Path) -> None:
    config = load_app_config(_write_config(tmp_path))
    defaults = config.venture_defaults

    assert defaults is not None
    assert defaults.lifecycle.initial_phase == "DISCOVER"
    assert defaults.monetization.default_strategy == "affiliate_first"
    assert defaults.monthly_incremental_budget.currency == "KRW"
    assert defaults.monthly_incremental_budget.amount == 0
    assert defaults.local_runner.required is True
    assert defaults.local_runner.required_by_phase == "LOCAL_PARITY"
    assert defaults.local_runner.executable is None
    assert defaults.prohibited_actions == ("live_trading",)


def test_venture_lifecycle_stage_validation(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    defaults_path = tmp_path / "venture-defaults.yaml"
    defaults_path.write_text(
        defaults_path.read_text(encoding="utf-8").replace("    - VALIDATE\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="lifecycle"):
        load_app_config(config_path)


def test_venture_negative_budget_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    defaults_path = tmp_path / "venture-defaults.yaml"
    defaults_path.write_text(
        defaults_path.read_text(encoding="utf-8").replace("amount: 0", "amount: -1"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="amount"):
        load_app_config(config_path)


def test_venture_bad_approval_value_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    defaults_path = tmp_path / "venture-defaults.yaml"
    defaults_path.write_text(
        defaults_path.read_text(encoding="utf-8").replace("external_publishing", "unreviewed_launch"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="approval gates"):
        load_app_config(config_path)


def test_venture_unknown_field_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    defaults_path = tmp_path / "venture-defaults.yaml"
    defaults_path.write_text(
        defaults_path.read_text(encoding="utf-8").replace(
            "venture_defaults:\n",
            "venture_defaults:\n  unexpected: true\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unknown fields"):
        load_app_config(config_path)


def test_venture_type_builds_local_first_plan(tmp_path: Path) -> None:
    result = _plan(
        tmp_path,
        name="Sample Local Venture",
        project_type="venture",
        goal="Find and validate local-first monetization opportunities.",
    )
    venture = result["venture"]

    assert result["project_id"] == "sample-local-venture"
    assert result["phase"] == "DISCOVER"
    assert result["registry_entry"]["phase"] == "DISCOVER"
    assert result["registry_entry"]["lifecycle"] == "planned"
    assert result["proposed_lead"]["role"] == "PROJECT_LEAD"
    assert result["proposed_lead"]["display_name"].endswith("Venture Lead")
    assert result["proposed_github_repository"]["create"] is False
    assert result["proposed_lead"]["create"] is False
    assert result["beads"]["enabled"] is True
    assert venture["monetization"] == {"default_strategy": "affiliate_first"}
    assert venture["monthly_incremental_budget"] == {"currency": "KRW", "amount": 0}
    assert venture["lifecycle"]["initial_phase"] == "DISCOVER"
    assert venture["lifecycle"]["current_phase"] == "DISCOVER"
    assert venture["lifecycle"]["stages"] == [
        "DISCOVER",
        "VALIDATE",
        "FRONTIER_PROTOTYPE",
        "NORMALIZE",
        "LOCAL_PARITY",
        "SHADOW_RUN",
        "AUTOMATED",
        "MONITORING",
        "SCALE",
        "RETIRE",
    ]
    assert venture["local_runner"] == {
        "required": True,
        "required_by_phase": "LOCAL_PARITY",
        "configured": False,
    }
    assert venture["approval_gates"] == [
        "external_publishing",
        "account_creation",
        "payment",
        "affiliate_application",
        "production_deployment",
    ]
    assert venture["prohibited_actions"] == ["live_trading"]
    assert venture["evidence_policy"] == {"actual_metrics": "observed_only"}
    assert result["resource_policy"]["execution_mode"] == "sequential"
    assert result["resource_policy"]["max_heavy_agents"] == 1
    assert result["executable"] is False


def test_venture_unconfigured_runner_warns_before_local_parity(tmp_path: Path) -> None:
    result = _plan(tmp_path, name="Local Venture", project_type="venture")

    assert result["venture"]["local_runner"]["configured"] is False
    assert any("before entering LOCAL_PARITY" in item for item in result["warnings"])


def test_configured_runner_is_validated_but_not_disclosed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    private_runner = tmp_path / "private-local-runner"
    private_runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    private_runner.chmod(0o700)
    defaults_path = tmp_path / "venture-defaults.yaml"
    defaults_path.write_text(
        _venture_defaults_yaml(executable=str(private_runner)),
        encoding="utf-8",
    )

    config = load_app_config(config_path)
    result = plan_project(
        config,
        ProjectPlanRequest(name="Configured Venture", project_type="venture", goal="Plan only."),
    )
    encoded = json.dumps(result)

    assert result["venture"]["local_runner"]["configured"] is True
    assert str(private_runner) not in encoded

    for extra_args in ([], ["--json"]):
        code = main(
            [
                "--config",
                str(config_path),
                "projects",
                "plan",
                "--name",
                "Configured Venture",
                "--type",
                "venture",
                "--goal",
                "Plan only.",
                *extra_args,
            ]
        )
        output = capsys.readouterr().out
        assert code == 0
        assert str(private_runner) not in output


@pytest.mark.parametrize("executable", ["relative-runner", "/missing/private-runner"])
def test_invalid_runner_path_is_rejected_without_disclosure(tmp_path: Path, executable: str) -> None:
    config_path = _write_config(tmp_path)
    defaults_path = tmp_path / "venture-defaults.yaml"
    defaults_path.write_text(_venture_defaults_yaml(executable=executable), encoding="utf-8")

    with pytest.raises(ConfigurationError) as captured:
        load_app_config(config_path)

    assert executable not in str(captured.value)


def test_symlink_runner_is_rejected_without_disclosure(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    target = tmp_path / "runner-target"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    symlink = tmp_path / "private-runner-link"
    symlink.symlink_to(target)
    (tmp_path / "venture-defaults.yaml").write_text(
        _venture_defaults_yaml(executable=str(symlink)),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_app_config(config_path)

    assert str(symlink) not in str(captured.value)


def test_non_executable_runner_is_rejected_without_disclosure(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    runner = tmp_path / "private-non-executable-runner"
    runner.write_text("not executable\n", encoding="utf-8")
    runner.chmod(0o600)
    (tmp_path / "venture-defaults.yaml").write_text(
        _venture_defaults_yaml(executable=str(runner)),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_app_config(config_path)

    assert str(runner) not in str(captured.value)


def test_venture_required_approvals_are_listed(tmp_path: Path) -> None:
    result = _plan(tmp_path, name="Local Venture", project_type="venture")
    text = " ".join(result["approvals_required"]).lower()

    for phrase in (
        "external publishing",
        "account creation",
        "payment",
        "affiliate application",
        "production deployment",
    ):
        assert phrase in text
    assert "live trading" not in text
    assert "live_trading" in result["venture"]["prohibited_actions"]


def test_venture_plan_does_not_invent_business_metrics(tmp_path: Path) -> None:
    result = _plan(tmp_path, name="Local Venture", project_type="venture")
    forbidden = {
        "actual_revenue",
        "conversion_rate",
        "success_rate",
        "progress",
        "progress_percent",
    }

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()

    assert not forbidden & keys(result)
    assert any("not a success claim" in item for item in result["warnings"])


@pytest.mark.parametrize("project_type", PROJECT_TYPES[:-1])
def test_existing_project_type_schemas_have_no_venture_fields(tmp_path: Path, project_type: str) -> None:
    result = _plan(tmp_path, project_type=project_type)

    assert "venture" not in result
    assert "phase" not in result


def test_plan_does_not_create_progress(tmp_path: Path) -> None:
    result = _plan(tmp_path)
    encoded = json.dumps(result)

    assert "progress_percent" not in encoded
    assert '"progress"' not in encoded


def test_plan_has_no_side_effects(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    config = load_app_config(config_path)

    result = plan_project(
        config,
        ProjectPlanRequest(
            name="Side Effect Check",
            project_type="app",
            goal="Plan only.",
        ),
    )
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    assert before == after
    assert result["registry_entry"]["lifecycle"] == "planned"
    assert result["proposed_github_repository"]["create"] is False
    assert result["proposed_lead"]["create"] is False


def test_venture_plan_has_no_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_config(tmp_path)
    registry_path = tmp_path / "projects.yaml"
    registry_before = registry_path.read_bytes()
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    def forbidden_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("plan-only venture must not invoke subprocess or network")

    monkeypatch.setattr(subprocess, "Popen", forbidden_call)
    monkeypatch.setattr(socket, "create_connection", forbidden_call)
    config = load_app_config(config_path)

    result = plan_project(
        config,
        ProjectPlanRequest(
            name="Local Venture",
            project_type="venture",
            goal="Plan only.",
        ),
    )
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    assert before == after
    assert registry_path.read_bytes() == registry_before
    assert not (tmp_path / "local-venture").exists()
    assert not (tmp_path / "knowledge" / "local-venture.md").exists()
    assert result["executable"] is False
    assert result["proposed_github_repository"]["create"] is False
    assert result["proposed_lead"]["create"] is False


def test_json_output_schema_is_stable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path)

    code = main(
        [
            "--config",
            str(config_path),
            "projects",
            "plan",
            "--name",
            "Blog Automation",
            "--type",
            "blog",
            "--goal",
            "Automate topic research and content production.",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert list(payload) == [
        "approvals_required",
        "beads",
        "display_name",
        "duplicate_candidates",
        "duplicate_decision",
        "executable",
        "goal",
        "knowledge_path",
        "planned_files",
        "project_id",
        "proposed_github_repository",
        "proposed_lead",
        "proposed_workspace",
        "registry_entry",
        "resource_policy",
        "slug",
        "type",
        "visibility",
        "warnings",
    ]
    assert payload["resource_policy"]["execution_mode"] == "sequential"
    assert payload["resource_policy"]["max_heavy_agents"] == 1
    assert payload["resource_policy"]["subagents"] == "disabled_by_default"


def test_public_fixtures_do_not_contain_private_identifiers() -> None:
    root = Path(__file__).parent / "fixtures"
    forbidden = tuple(value for value in (os.environ.get("HOME"),) if value)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".yaml", ".yml", ".json"}:
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                assert value not in text
            assert "agent:sample-lead:" not in text
