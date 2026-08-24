"""Pure approval-bound project apply contract.

This module validates and describes an apply transaction.  It deliberately has
no mutation, subprocess, Git, GitHub, Beads, Hermes, runner, or network adapter.
Private executors must call this contract before considering any write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

APPLY_SCHEMA_VERSION = "agent-workspace-project-apply/v1"
MANIFEST_SCHEMA_VERSION = "agent-workspace-action-manifest/v1"
APPROVAL_GATES = (
    "external_publishing",
    "account_creation",
    "payment",
    "affiliate_application",
    "production_deployment",
)
ACTION_ORDER = (
    "PREFLIGHT",
    "CREATE_WORKSPACE",
    "INITIALIZE_GIT",
    "WRITE_MINIMAL_FILES",
    "INITIALIZE_BEADS",
    "CREATE_PRIVATE_GITHUB_REPOSITORY",
    "CONNECT_ORIGIN",
    "INITIAL_PUSH",
    "REGISTER_PROJECT",
    "CREATE_HERMES_PROFILE",
    "VERIFY_FINAL_STATE",
)
_ACTION_INPUTS: Mapping[str, tuple[str, ...]] = {
    "PREFLIGHT": ("plan_digest", "collision_observations"),
    "CREATE_WORKSPACE": ("project_id", "workspace"),
    "INITIALIZE_GIT": ("workspace",),
    "WRITE_MINIMAL_FILES": ("workspace", "planned_files"),
    "INITIALIZE_BEADS": ("workspace", "beads.enabled"),
    "CREATE_PRIVATE_GITHUB_REPOSITORY": ("repository", "visibility"),
    "CONNECT_ORIGIN": ("workspace", "repository"),
    "INITIAL_PUSH": ("workspace", "canonical_branch"),
    "REGISTER_PROJECT": ("project_id", "registry_entry"),
    "CREATE_HERMES_PROFILE": ("project_id", "lead.agent_id", "profile_policy"),
    "VERIFY_FINAL_STATE": ("plan_digest", "all_targets"),
}
_PROJECT_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9])?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_METRICS = {
    "actual_revenue",
    "conversion_rate",
    "success_rate",
    "progress",
    "progress_percent",
}
_PRIVATE_KEYS = {
    "workspace",
    "workspace_root",
    "proposed_workspace",
    "token",
    "oauth_token",
    "ssh_key",
    "session_key",
    "secret",
    "private_executor_config",
}


class ApplyContractError(ValueError):
    """The plan or approval does not satisfy the public apply contract."""


class IdempotencyDecision(StrEnum):
    READY_TO_APPLY = "READY_TO_APPLY"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    PARTIAL_COLLISION = "PARTIAL_COLLISION"
    CONFLICT = "CONFLICT"
    BLOCKED = "BLOCKED"


class TargetState(StrEnum):
    ABSENT = "ABSENT"
    MATCH = "MATCH"
    DIFFERENT = "DIFFERENT"


@dataclass(frozen=True)
class CollisionObservation:
    target: str
    state: TargetState
    detail: str | None = None


@dataclass(frozen=True)
class PreflightInput:
    observations: tuple[CollisionObservation, ...]
    policy_valid: bool = True
    digest_valid: bool = True


@dataclass(frozen=True)
class PreflightResult:
    decision: IdempotencyDecision
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"decision": self.decision.value, "reasons": list(self.reasons)}


def _reject_constant(value: str) -> None:
    raise ApplyContractError(f"non-finite JSON number is forbidden: {value}")


def parse_plan_file(path: Path) -> dict[str, Any]:
    """Parse one UTF-8 JSON object without accepting NaN or Infinity."""

    try:
        text = path.read_bytes().decode("utf-8", errors="strict")
        value = json.loads(text, parse_constant=_reject_constant)
    except ApplyContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApplyContractError("plan file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ApplyContractError("canonical plan must be a JSON object")
    return value


def canonical_json_bytes(plan: Mapping[str, Any]) -> bytes:
    """Serialize canonical UTF-8 JSON, excluding a trailing newline.

    Object keys are sorted, separators are compact, and non-finite numbers are
    rejected. Therefore semantically equal JSON objects produce equal bytes.
    """

    if not isinstance(plan, Mapping):
        raise ApplyContractError("canonical plan must be a JSON object")
    try:
        encoded = json.dumps(
            dict(plan),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApplyContractError("plan cannot be canonically serialized") from exc
    if encoded.endswith(b"\n"):
        raise AssertionError("canonical serializer emitted a trailing newline")
    return encoded


def plan_sha256(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(plan)).hexdigest()


def verify_approval_digest(plan: Mapping[str, Any], approval_sha256: str) -> str:
    if not isinstance(approval_sha256, str) or not _SHA256.fullmatch(approval_sha256):
        raise ApplyContractError("approval SHA-256 must be exactly 64 lowercase hex characters")
    digest = plan_sha256(plan)
    if not hmac.compare_digest(digest, approval_sha256):
        raise ApplyContractError("approval digest does not match the canonical plan")
    return digest


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApplyContractError(f"{label} must be an object")
    return value


def _forbidden_keys(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in _FORBIDDEN_METRICS:
                found.append(child_path)
            found.extend(_forbidden_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_keys(child, f"{path}[{index}]"))
    return found


def _validate_workspace(plan: Mapping[str, Any]) -> None:
    root_value = plan.get("workspace_root")
    workspace_value = plan.get("proposed_workspace")
    if not isinstance(root_value, str) or not isinstance(workspace_value, str):
        raise ApplyContractError("workspace_root and proposed_workspace are required")
    if "\x00" in root_value or "\x00" in workspace_value:
        raise ApplyContractError("workspace path contains an invalid character")
    root = Path(root_value)
    workspace = Path(workspace_value)
    if not root.is_absolute() or not workspace.is_absolute():
        raise ApplyContractError("workspace paths must be absolute")
    root_normal = Path(os.path.abspath(root))
    workspace_normal = Path(os.path.abspath(workspace))
    if root != root_normal or workspace != workspace_normal:
        raise ApplyContractError("workspace paths must be lexically normalized")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ApplyContractError("workspace root must be an existing directory") from exc
    if root_stat.st_mode & 0o170000 == 0o120000:
        raise ApplyContractError("workspace root cannot be a symlink")
    if not root.is_dir():
        raise ApplyContractError("workspace root must be an existing directory")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ApplyContractError("workspace root must be an existing directory") from exc
    project_id = str(plan.get("project_id", ""))
    if workspace.name != project_id:
        raise ApplyContractError("workspace name must match project_id")
    try:
        if os.path.commonpath((str(root), str(workspace))) != str(root):
            raise ApplyContractError("workspace escapes workspace_root")
    except ValueError as exc:
        raise ApplyContractError("workspace escapes workspace_root") from exc
    relative = workspace.relative_to(root)
    current = root
    deepest_existing = root
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ApplyContractError("workspace path cannot be inspected safely") from exc
        if current_stat.st_mode & 0o170000 == 0o120000:
            raise ApplyContractError("workspace path cannot contain a symlink")
        deepest_existing = current
    try:
        resolved_ancestor = deepest_existing.resolve(strict=True)
        if os.path.commonpath((str(resolved_root), str(resolved_ancestor))) != str(resolved_root):
            raise ApplyContractError("workspace escapes workspace root")
    except ValueError as exc:
        raise ApplyContractError("workspace escapes workspace root") from exc


def validate_plan(plan: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate schema/policy and return non-blocking warnings."""

    if plan.get("schema_version") != APPLY_SCHEMA_VERSION:
        raise ApplyContractError("apply schema version mismatch")
    if plan.get("executable") is not False:
        raise ApplyContractError("apply plan must declare executable=false")
    if plan.get("type") != "venture":
        raise ApplyContractError("only venture apply plans are supported")
    if plan.get("visibility") != "private":
        raise ApplyContractError("apply plan visibility must be private")

    project_id = plan.get("project_id")
    slug = plan.get("slug")
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise ApplyContractError("project_id violates slug policy")
    if slug != project_id:
        raise ApplyContractError("slug must exactly match project_id")
    _validate_workspace(plan)

    repository = _require_mapping(plan.get("proposed_github_repository"), "repository")
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not _REPOSITORY.fullmatch(full_name):
        raise ApplyContractError("repository full_name violates policy")
    owner, name = full_name.split("/", 1)
    if repository.get("owner") != owner or repository.get("name") != name or name != project_id:
        raise ApplyContractError("repository identifiers are inconsistent")
    if repository.get("visibility") != "private" or repository.get("private") is not True:
        raise ApplyContractError("repository must be private")
    if repository.get("create") is not False:
        raise ApplyContractError("canonical plan must not claim repository creation")

    venture = _require_mapping(plan.get("venture"), "venture")
    monetization = _require_mapping(venture.get("monetization"), "venture.monetization")
    if monetization.get("default_strategy") != "affiliate_first":
        raise ApplyContractError("venture monetization must default to affiliate_first")
    budget = _require_mapping(
        venture.get("monthly_incremental_budget"),
        "venture.monthly_incremental_budget",
    )
    if budget.get("currency") != "KRW" or budget.get("amount") != 0:
        raise ApplyContractError("monthly incremental budget must be KRW 0")
    lifecycle = _require_mapping(venture.get("lifecycle"), "venture.lifecycle")
    phase = lifecycle.get("current_phase")
    if (
        phase != "DISCOVER"
        or lifecycle.get("initial_phase") != "DISCOVER"
        or plan.get("phase") != "DISCOVER"
    ):
        raise ApplyContractError("apply plan must start in DISCOVER")
    runner = _require_mapping(venture.get("local_runner"), "venture.local_runner")
    if runner.get("required") is not True:
        raise ApplyContractError("local runner must be required")
    if runner.get("required_by_phase") != "LOCAL_PARITY":
        raise ApplyContractError("local runner must be required by LOCAL_PARITY")
    if runner.get("configured") is not False:
        raise ApplyContractError("approved plan requires local runner configured=false")

    gates = venture.get("approval_gates")
    if not isinstance(gates, list) or len(gates) != len(APPROVAL_GATES) or set(gates) != set(APPROVAL_GATES):
        raise ApplyContractError("all five approval gates must be present exactly once")
    if "live_trading" in gates:
        raise ApplyContractError("live_trading cannot be an approval gate")
    prohibited = venture.get("prohibited_actions")
    if not isinstance(prohibited, list) or "live_trading" not in prohibited:
        raise ApplyContractError("live_trading must remain prohibited")

    forbidden = _forbidden_keys(plan)
    if forbidden:
        raise ApplyContractError("forbidden actual metric key is present")

    lead = _require_mapping(plan.get("proposed_lead"), "proposed_lead")
    if lead.get("agent_id") != "venture" or lead.get("create") is not False:
        raise ApplyContractError("Venture Lead proposal is inconsistent")
    beads = _require_mapping(plan.get("beads"), "beads")
    if beads.get("enabled") is not True:
        raise ApplyContractError("Beads must be enabled")

    return ("Configure the local runner before entering LOCAL_PARITY.",)


def action_manifest(plan: Mapping[str, Any]) -> dict[str, Any]:
    validate_plan(plan)
    profile_policy = _require_mapping(plan.get("profile_policy"), "profile_policy")
    identity: dict[str, Any] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "plan_schema_version": plan["schema_version"],
        "plan_sha256": plan_sha256(plan),
        "project_id": plan["project_id"],
        "project_type": plan["type"],
        "visibility": plan["visibility"],
        "profile_policy_sha256": hashlib.sha256(canonical_json_bytes(profile_policy)).hexdigest(),
        "ordered_actions": [
            {"order": index, "action": action, "required_inputs": list(_ACTION_INPUTS[action])}
            for index, action in enumerate(ACTION_ORDER, start=1)
        ],
    }
    return {
        **identity,
        "manifest_sha256": hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
    }


def classify_preflight(value: PreflightInput) -> PreflightResult:
    if not value.policy_valid or not value.digest_valid:
        return PreflightResult(IdempotencyDecision.BLOCKED, ("policy or digest mismatch",))
    if not value.observations:
        return PreflightResult(IdempotencyDecision.BLOCKED, ("target observations are required",))
    states = {item.state for item in value.observations}
    if TargetState.DIFFERENT in states:
        differing = tuple(item.target for item in value.observations if item.state == TargetState.DIFFERENT)
        return PreflightResult(IdempotencyDecision.CONFLICT, differing)
    if states == {TargetState.ABSENT}:
        return PreflightResult(IdempotencyDecision.READY_TO_APPLY, ())
    if states == {TargetState.MATCH}:
        return PreflightResult(IdempotencyDecision.ALREADY_APPLIED, ())
    return PreflightResult(IdempotencyDecision.PARTIAL_COLLISION, ("only some targets exist",))


def redact_private_values(value: Any) -> Any:
    """Return a recursively redacted public representation."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _PRIVATE_KEYS else redact_private_values(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_private_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_private_values(item) for item in value)
    return value


def validate_approved_plan(path: Path, approval_sha256: str) -> dict[str, Any]:
    plan = parse_plan_file(path)
    warnings = validate_plan(plan)
    digest = verify_approval_digest(plan, approval_sha256)
    return {
        "status": "VALID",
        "schema_version": APPLY_SCHEMA_VERSION,
        "plan_sha256": digest,
        "warnings": list(warnings),
        "action_manifest": action_manifest(plan),
        "execution_performed": False,
    }
