from __future__ import annotations

import json
import os
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


def _venture_defaults_yaml() -> str:
    return """
venture_defaults:
  strategy: local_first
  lifecycle:
    stages:
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
    initial_stage: DISCOVER
  monetization:
    preferred: affiliate
    free_user_access_preferred: true
  frontier:
    allowed_uses:
      - prototype
      - quality_baseline
    subscription: existing_only
    monthly_cash_budget_krw: 0
  local:
    runner_required: true
    runner: null
    model: null
  quality_gate:
    required: true
    metrics: []
  shadow_run_days: null
  schedule: null
  success_metrics: []
  kill_criteria: []
  risks:
    platform: []
    licensing: []
  approvals:
    required:
      - external_publish
      - account_creation
      - payment
      - affiliate_application
      - production_deploy
      - personal_data_collection
    prohibited:
      - live_trading
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
{_venture_defaults_yaml()}
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
    assert defaults.strategy == "local_first"
    assert defaults.lifecycle.initial_stage == "DISCOVER"
    assert defaults.monetization.preferred == "affiliate"
    assert defaults.frontier.monthly_cash_budget_krw == 0
    assert defaults.local.runner_required is True
    assert defaults.quality_gate.required is True
    assert defaults.shadow_run_days is None
    assert defaults.approvals.prohibited == ("live_trading",)


def test_venture_lifecycle_stage_validation(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("initial_stage: DISCOVER", "initial_stage: IDEA"),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="initial_stage"):
        load_app_config(config_path)


def test_venture_negative_budget_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("monthly_cash_budget_krw: 0", "monthly_cash_budget_krw: -1"),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="monthly_cash_budget_krw"):
        load_app_config(config_path)


def test_venture_bad_approval_value_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("external_publish", "unreviewed_launch"),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="approvals.required"):
        load_app_config(config_path)


def test_venture_unknown_field_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  strategy: local_first",
            "  strategy: local_first\n  unexpected: true",
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="unknown fields"):
        load_app_config(config_path)


def test_venture_type_builds_local_first_plan(tmp_path: Path) -> None:
    result = _plan(
        tmp_path,
        name="Local-First Revenue Lab",
        project_type="venture",
        goal="Find and validate local-first monetization opportunities.",
    )
    venture = result["venture"]

    assert result["project_id"] == "local-first-revenue-lab"
    assert result["phase"] == "DISCOVER"
    assert result["registry_entry"]["phase"] == "DISCOVER"
    assert result["registry_entry"]["venture"]["strategy"] == "local_first"
    assert result["proposed_lead"]["role"] == "PROJECT_LEAD"
    assert result["proposed_lead"]["specialization"] == "VENTURE"
    assert result["proposed_lead"]["display_name"].endswith("Venture Lead")
    assert result["beads"]["enabled"] is True
    assert venture["strategy"] == "local_first"
    assert venture["lifecycle_stage"] == "DISCOVER"
    assert venture["monetization_model"] == "affiliate"
    assert venture["frontier_policy"]["allowed_uses"] == ["prototype", "quality_baseline"]
    assert venture["frontier_policy"]["subscription"] == "existing_only"
    assert venture["frontier_policy"]["additional_monthly_cash_budget_krw"] == 0
    assert venture["local_runner_required"] is True
    assert venture["local_model"] is None
    assert venture["quality_gate"] == {"required": True, "metrics": []}
    assert venture["shadow_run_days"] is None
    assert venture["success_metrics"] == []
    assert venture["kill_criteria"] == []
    assert "live_trading" in venture["approvals"]["prohibited"]
    assert result["resource_policy"]["execution_mode"] == "sequential"
    assert result["resource_policy"]["max_heavy_agents"] == 1
    assert result["executable"] is False


def test_venture_missing_user_inputs_are_reported(tmp_path: Path) -> None:
    result = _plan(tmp_path, name="Local Venture", project_type="venture")

    assert set(result["missing_user_inputs"]) == {
        "local_runner",
        "local_model",
        "quality_gate.metrics",
        "shadow_run_days",
        "schedule",
        "success_metrics",
        "kill_criteria",
    }


def test_venture_required_approvals_are_listed(tmp_path: Path) -> None:
    result = _plan(tmp_path, name="Local Venture", project_type="venture")
    text = " ".join(result["approvals_required"]).lower()

    for phrase in (
        "external publishing",
        "account creation",
        "payment",
        "affiliate application",
        "production deployment",
        "personal data collection",
    ):
        assert phrase in text
    assert "live trading is prohibited" in text


def test_venture_plan_does_not_invent_business_metrics(tmp_path: Path) -> None:
    result = _plan(tmp_path, name="Local Venture", project_type="venture")
    encoded = json.dumps(result)

    for forbidden in (
        "progress_percent",
        '"progress"',
        "success_probability",
        "traffic_projection",
        "revenue_projection",
        "expected_revenue",
    ):
        assert forbidden not in encoded


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


def test_venture_plan_has_no_side_effects(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
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
    assert result["executable"] is False


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
