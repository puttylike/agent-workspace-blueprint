from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path

import pytest

from agent_workspace.cli import main
from agent_workspace.controller.project_apply import (
    ACTION_ORDER,
    APPLY_SCHEMA_VERSION,
    ApplyContractError,
    CollisionObservation,
    IdempotencyDecision,
    PreflightInput,
    TargetState,
    action_manifest,
    canonical_json_bytes,
    classify_preflight,
    parse_plan_file,
    plan_sha256,
    redact_private_values,
    validate_plan,
    verify_approval_digest,
)


def approved_plan(tmp_path: Path) -> dict:
    workspace_root = tmp_path / "projects"
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "project_id": "sample-venture",
        "slug": "sample-venture",
        "display_name": "Sample Venture",
        "type": "venture",
        "goal": "Discover and validate local-first revenue opportunities.",
        "visibility": "private",
        "workspace_root": str(workspace_root),
        "proposed_workspace": str(workspace_root / "sample-venture"),
        "proposed_github_repository": {
            "owner": "example",
            "name": "sample-venture",
            "full_name": "example/sample-venture",
            "visibility": "private",
            "private": True,
            "create": False,
        },
        "proposed_lead": {
            "agent_id": "venture",
            "display_name": "Venture Lead",
            "role": "PROJECT_LEAD",
            "create": False,
        },
        "beads": {"enabled": True},
        "phase": "DISCOVER",
        "venture": {
            "monetization": {"default_strategy": "affiliate_first"},
            "monthly_incremental_budget": {"currency": "KRW", "amount": 0},
            "lifecycle": {
                "initial_phase": "DISCOVER",
                "current_phase": "DISCOVER",
                "stages": ["DISCOVER", "VALIDATE", "LOCAL_PARITY"],
            },
            "local_runner": {
                "required": True,
                "required_by_phase": "LOCAL_PARITY",
                "configured": False,
            },
            "approval_gates": [
                "external_publishing",
                "account_creation",
                "payment",
                "affiliate_application",
                "production_deployment",
            ],
            "prohibited_actions": ["live_trading"],
        },
        "planned_files": ["README.md", "PROJECT.md", ".gitignore"],
        "registry_entry": {"id": "sample-venture"},
        "profile_policy": {
            "terminal.backend": "local",
            "model.openai_runtime": "auto",
            "auxiliary.background_review.enabled": False,
            "curator.enabled": False,
            "lsp.enabled": False,
            "memory.write_approval": True,
            "skills.write_approval": True,
            "gateway": "stopped",
            "cron_jobs": 0,
            "oauth": "AUTH_REQUIRED",
        },
        "executable": False,
    }


def write_canonical(path: Path, plan: dict) -> None:
    path.write_bytes(canonical_json_bytes(plan))


def test_canonical_serialization_is_deterministic_and_has_no_newline(tmp_path: Path) -> None:
    plan = approved_plan(tmp_path)
    reordered = dict(reversed(list(plan.items())))

    assert canonical_json_bytes(plan) == canonical_json_bytes(plan)
    assert canonical_json_bytes(plan) == canonical_json_bytes(reordered)
    assert plan_sha256(plan) == plan_sha256(reordered)
    assert not canonical_json_bytes(plan).endswith(b"\n")


def test_one_field_change_invalidates_digest(tmp_path: Path) -> None:
    plan = approved_plan(tmp_path)
    changed = copy.deepcopy(plan)
    changed["display_name"] = "Changed"

    assert plan_sha256(plan) != plan_sha256(changed)
    with pytest.raises(ApplyContractError, match="does not match"):
        verify_approval_digest(changed, plan_sha256(plan))


@pytest.mark.parametrize("payload", [b"{", b"[]", b'{"value": NaN}', b'{"value": Infinity}'])
def test_invalid_json_is_rejected(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "plan.json"
    path.write_bytes(payload)

    with pytest.raises(ApplyContractError):
        parse_plan_file(path)


def test_non_finite_values_cannot_be_serialized() -> None:
    with pytest.raises(ApplyContractError):
        canonical_json_bytes({"value": float("nan")})


def test_schema_version_mismatch_is_rejected(tmp_path: Path) -> None:
    plan = approved_plan(tmp_path)
    plan["schema_version"] = "old"
    with pytest.raises(ApplyContractError, match="version"):
        validate_plan(plan)


def test_bad_approval_digest_is_rejected(tmp_path: Path) -> None:
    plan = approved_plan(tmp_path)
    with pytest.raises(ApplyContractError):
        verify_approval_digest(plan, "0" * 64)
    with pytest.raises(ApplyContractError):
        verify_approval_digest(plan, "A" * 64)


@pytest.mark.parametrize(
    ("field", "value"),
    [("executable", True), ("visibility", "public")],
)
def test_apply_policy_rejections(tmp_path: Path, field: str, value: object) -> None:
    plan = approved_plan(tmp_path)
    plan[field] = value
    with pytest.raises(ApplyContractError):
        validate_plan(plan)


@pytest.mark.parametrize(
    "workspace",
    ["../escape", "/", "/tmp/../escape", "relative/path"],
)
def test_path_traversal_and_root_escape_are_rejected(tmp_path: Path, workspace: str) -> None:
    plan = approved_plan(tmp_path)
    plan["proposed_workspace"] = workspace
    with pytest.raises(ApplyContractError):
        validate_plan(plan)


def test_symlink_collision_is_rejected(tmp_path: Path) -> None:
    plan = approved_plan(tmp_path)
    root = Path(plan["workspace_root"])
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    Path(plan["proposed_workspace"]).symlink_to(target, target_is_directory=True)

    with pytest.raises(ApplyContractError, match="symlink"):
        validate_plan(plan)


def test_discover_allows_unconfigured_runner_and_warns_for_local_parity(tmp_path: Path) -> None:
    warnings = validate_plan(approved_plan(tmp_path))
    assert warnings == ("Configure the local runner before entering LOCAL_PARITY.",)


def test_missing_approval_gate_is_rejected(tmp_path: Path) -> None:
    plan = approved_plan(tmp_path)
    plan["venture"]["approval_gates"].pop()
    with pytest.raises(ApplyContractError, match="five"):
        validate_plan(plan)


def test_live_trading_cannot_be_approved(tmp_path: Path) -> None:
    plan = approved_plan(tmp_path)
    plan["venture"]["approval_gates"][-1] = "live_trading"
    with pytest.raises(ApplyContractError):
        validate_plan(plan)


@pytest.mark.parametrize(
    "metric",
    ["actual_revenue", "conversion_rate", "success_rate", "progress", "progress_percent"],
)
def test_forbidden_metric_keys_are_rejected(tmp_path: Path, metric: str) -> None:
    plan = approved_plan(tmp_path)
    plan["venture"][metric] = 0
    with pytest.raises(ApplyContractError, match="metric"):
        validate_plan(plan)


def test_action_manifest_is_ordered_and_contains_inputs_only(tmp_path: Path) -> None:
    manifest = action_manifest(approved_plan(tmp_path))
    assert [item["action"] for item in manifest] == list(ACTION_ORDER)
    assert all(set(item) == {"order", "action", "required_inputs"} for item in manifest)
    assert str(tmp_path) not in json.dumps(manifest)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([TargetState.ABSENT] * 3, IdempotencyDecision.READY_TO_APPLY),
        ([TargetState.MATCH] * 3, IdempotencyDecision.ALREADY_APPLIED),
        ([TargetState.ABSENT, TargetState.MATCH], IdempotencyDecision.PARTIAL_COLLISION),
        ([TargetState.ABSENT, TargetState.DIFFERENT], IdempotencyDecision.CONFLICT),
    ],
)
def test_idempotency_model(states: list[TargetState], expected: IdempotencyDecision) -> None:
    observations = tuple(CollisionObservation(str(index), state) for index, state in enumerate(states))
    assert classify_preflight(PreflightInput(observations)).decision == expected


def test_policy_or_digest_failure_is_blocked() -> None:
    observations = (CollisionObservation("workspace", TargetState.ABSENT),)
    assert classify_preflight(PreflightInput(observations, policy_valid=False)).decision == IdempotencyDecision.BLOCKED
    assert classify_preflight(PreflightInput(observations, digest_valid=False)).decision == IdempotencyDecision.BLOCKED


def test_private_values_are_redacted() -> None:
    value = {"workspace": "/private/path", "nested": {"oauth_token": "secret-value"}, "safe": "ok"}
    redacted = redact_private_values(value)
    assert redacted == {"workspace": "[REDACTED]", "nested": {"oauth_token": "[REDACTED]"}, "safe": "ok"}
    assert "/private/path" not in json.dumps(redacted)
    assert "secret-value" not in json.dumps(redacted)


def test_public_cli_is_validation_only_and_filesystem_invariant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = approved_plan(tmp_path)
    plan_path = tmp_path / "plan.json"
    write_canonical(plan_path, plan)
    digest = plan_sha256(plan)
    before = {path.relative_to(tmp_path): (path.stat().st_mode, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append("mutation")
        raise AssertionError("public apply attempted an external call")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)

    code = main(["projects", "apply", "--plan-file", str(plan_path), "--approval-sha256", digest, "--json"])
    output = json.loads(capsys.readouterr().out)
    after = {path.relative_to(tmp_path): (path.stat().st_mode, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}

    assert code == 0
    assert output["execution_performed"] is False
    assert [item["action"] for item in output["action_manifest"]] == list(ACTION_ORDER)
    assert calls == []
    assert after == before
