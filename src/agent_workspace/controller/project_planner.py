"""Plan-only project creation proposals.

The planner never creates files, repositories, Beads state, or agents. It only
combines user intent with the private registry to produce an auditable plan.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from ..models import AppConfig, ProjectRecord, SecurityBoundaryError, VentureDefaultsConfig
from .registry import RegistryReader

PROJECT_TYPES = ("paper", "blog", "quant", "app", "contest", "misc", "venture")
VISIBILITIES = ("private", "public")
_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{30,}\b", re.I),
    re.compile(r"\b(password|passwd|secret|token|credential|api[_-]?key|session[_-]?key)\b", re.I),
    re.compile(r"\b(password|passwd|secret|token|credential|api[_-]?key|session[_-]?key)\s*[:=]", re.I),
)
_GENERIC_TOKENS = {
    "a",
    "an",
    "and",
    "app",
    "blog",
    "for",
    "paper",
    "project",
    "research",
    "study",
    "the",
}


class ProjectPlanError(ValueError):
    """User-supplied planning input is unsafe or unsupported."""


@dataclass(frozen=True)
class ProjectPlanRequest:
    name: str
    project_type: str
    goal: str
    visibility: str = "private"


def _contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = _SLUG_CHARS.sub("-", lowered).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", slug):
        raise ProjectPlanError("project name must produce an ascii slug")
    return slug


def _validate_text(value: str, label: str, *, path_like: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectPlanError(f"{label} is required")
    cleaned = value.strip()
    if "\x00" in cleaned:
        raise ProjectPlanError(f"{label} contains an invalid character")
    if _contains_secret(cleaned):
        raise ProjectPlanError(f"{label} contains secret-like text")
    if path_like:
        candidate = Path(cleaned)
        if candidate.is_absolute() or ".." in candidate.parts or "/" in cleaned or "\\" in cleaned:
            raise ProjectPlanError(f"{label} must not be a path")
    return cleaned


def _tokens(value: str) -> set[str]:
    return {token for token in slugify(value).split("-") if token and token not in _GENERIC_TOKENS}


def _similarity(left: str, right: str) -> float:
    left_slug = slugify(left)
    right_slug = slugify(right)
    ratio = SequenceMatcher(None, left_slug, right_slug).ratio()
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return ratio
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return max(ratio, jaccard)


def _workspace_root(projects: Iterable[ProjectRecord], config: AppConfig) -> Path:
    for project in projects:
        return project.workspace.parent
    return config.blueprint_root.parent


def _repository_owner(projects: Iterable[ProjectRecord]) -> str:
    for project in projects:
        owner, _name = project.git.repository.split("/", 1)
        return owner
    return "example"


def _safe_child(root: Path, child_name: str) -> Path:
    root_abs = Path(os.path.abspath(root))
    candidate = root_abs / child_name
    if candidate.is_absolute() is False or ".." in candidate.parts:
        raise SecurityBoundaryError("planned path must be normalized")
    try:
        if os.path.commonpath((str(root_abs), str(candidate))) != str(root_abs):
            raise SecurityBoundaryError("planned path is outside the workspace root")
    except ValueError as exc:
        raise SecurityBoundaryError("planned path is outside the workspace root") from exc
    if candidate.exists() and candidate.is_symlink():
        raise SecurityBoundaryError("planned path collides with a symlink")
    return candidate


def _beads_policy(project_type: str, name: str, goal: str) -> dict[str, Any]:
    text = f"{name} {goal}".lower()
    long_running = any(
        word in text
        for word in (
            "automation",
            "benchmark",
            "content pipeline",
            "experiment",
            "pipeline",
            "prototype",
            "research",
            "study",
            "자동화",
            "장기",
            "연구",
            "실험",
            "프로토타입",
        )
    )
    if project_type == "paper":
        return {
            "enabled": long_running,
            "reason": (
                "Use Beads for iterative experiments or long-running research."
                if long_running
                else "Not required unless the paper becomes iterative or long running."
            ),
        }
    if project_type == "blog":
        return {
            "enabled": long_running,
            "reason": (
                "Use Beads because the plan mentions automation or a long-running content pipeline."
                if long_running
                else "Not required for a one-off blog draft."
            ),
        }
    if project_type == "quant":
        return {"enabled": True, "reason": "Quant work uses Beads by default for auditability."}
    if project_type == "app":
        return {"enabled": True, "reason": "App work uses Beads by default for implementation tasks."}
    if project_type == "contest":
        enabled = any(word in text for word in ("build", "code", "prototype", "개발", "프로토타입"))
        return {
            "enabled": enabled,
            "reason": (
                "Use Beads because the contest plan includes prototype development."
                if enabled
                else "Not required unless prototype development is included."
            ),
        }
    if project_type == "venture":
        return {"enabled": True, "reason": "Venture planning uses Beads by default for validation evidence."}
    return {
        "enabled": long_running,
        "reason": (
            "Use Beads only because the misc plan appears long running."
            if long_running
            else "Misc plans do not use Beads by default."
        ),
    }


def _approvals(project_type: str, visibility: str) -> list[str]:
    approvals = [
        "Create the workspace directory.",
        "Create or connect the GitHub repository.",
        "Register the project in Agent Ops.",
        "Create or assign the project Lead agent.",
    ]
    if visibility == "public":
        approvals.append("Publish public project metadata and repository visibility.")
    if project_type == "blog":
        approvals.append("Approve any external publishing destination.")
    if project_type == "quant":
        approvals.append("Live trading, order placement, and account connections remain forbidden without separate approval.")
    if project_type == "app":
        approvals.append("Approve any production deployment.")
    if project_type == "contest":
        approvals.append("Approve any external contest submission.")
    if project_type == "venture":
        approvals.append("Approve external publishing before any public release.")
        approvals.append("Approve account creation before signing up for any service.")
        approvals.append("Approve payment or subscription changes before spending cash.")
        approvals.append("Approve affiliate applications before applying.")
        approvals.append("Approve production deployment before release.")
        approvals.append("Approve personal data collection before collecting user data.")
    return approvals


def _warnings(project_type: str, visibility: str) -> list[str]:
    warnings = [
        "This is a plan only; it cannot create files, repositories, Beads state, or agents.",
        "Long-running work must be split into small Lead-owned tasks.",
        "Paper Lead and the new project Lead must not run heavy work concurrently.",
    ]
    if visibility == "public":
        warnings.append("Public visibility requires explicit user approval before execution.")
    if project_type == "quant":
        warnings.append("Do not connect broker accounts, place orders, or run live trading from this plan.")
    if project_type == "venture":
        warnings.append("Do not run live trading from a venture plan.")
    return warnings


_APPROVAL_LABELS = {
    "external_publish": "external publishing",
    "account_creation": "account creation",
    "payment": "payment",
    "affiliate_application": "affiliate application",
    "production_deploy": "production deployment",
    "personal_data_collection": "personal data collection",
    "live_trading": "live trading",
}


def _missing_user_inputs(defaults: VentureDefaultsConfig) -> list[str]:
    missing: list[str] = []
    if defaults.local.runner is None:
        missing.append("local_runner")
    if defaults.local.model is None:
        missing.append("local_model")
    if not defaults.quality_gate.metrics:
        missing.append("quality_gate.metrics")
    if defaults.shadow_run_days is None:
        missing.append("shadow_run_days")
    if defaults.schedule is None:
        missing.append("schedule")
    if not defaults.success_metrics:
        missing.append("success_metrics")
    if not defaults.kill_criteria:
        missing.append("kill_criteria")
    return missing


def _venture_plan(defaults: VentureDefaultsConfig) -> dict[str, Any]:
    return {
        "strategy": defaults.strategy,
        "lifecycle_stage": defaults.lifecycle.initial_stage,
        "lifecycle_stages": list(defaults.lifecycle.stages),
        "monetization_model": defaults.monetization.preferred,
        "free_user_access_preferred": defaults.monetization.free_user_access_preferred,
        "frontier_policy": {
            "allowed_uses": list(defaults.frontier.allowed_uses),
            "subscription": defaults.frontier.subscription,
            "additional_monthly_cash_budget_krw": defaults.frontier.monthly_cash_budget_krw,
        },
        "local_runner_required": defaults.local.runner_required,
        "local_runner": defaults.local.runner,
        "local_model": defaults.local.model,
        "quality_gate": {
            "required": defaults.quality_gate.required,
            "metrics": list(defaults.quality_gate.metrics),
        },
        "shadow_run_days": defaults.shadow_run_days,
        "schedule": defaults.schedule,
        "success_metrics": list(defaults.success_metrics),
        "kill_criteria": list(defaults.kill_criteria),
        "platform_risks": list(defaults.risks.platform),
        "licensing_risks": list(defaults.risks.licensing),
        "approvals": {
            "required": list(defaults.approvals.required),
            "prohibited": list(defaults.approvals.prohibited),
        },
        "missing_user_inputs": _missing_user_inputs(defaults),
    }


def _venture_approvals(defaults: VentureDefaultsConfig) -> list[str]:
    required = [
        f"Approve {_APPROVAL_LABELS.get(item, item.replace('_', ' '))}."
        for item in defaults.approvals.required
    ]
    prohibited = [
        f"{_APPROVAL_LABELS.get(item, item.replace('_', ' ')).title()} is prohibited."
        for item in defaults.approvals.prohibited
    ]
    return required + prohibited


def _duplicate_candidates(
    *,
    projects: Iterable[ProjectRecord],
    slug: str,
    display_name: str,
    project_type: str,
    proposed_workspace: Path,
    proposed_repository: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for project in projects:
        repo_name = project.git.repository.split("/", 1)[1]
        checks = {
            "id": slugify(project.id),
            "name": slugify(project.name),
            "workspace": slugify(project.workspace.name),
            "repository": slugify(repo_name),
        }
        exact_fields = [field for field, value in checks.items() if value == slug]
        reasons: list[str] = []
        if exact_fields:
            reasons.append("normalized " + ", ".join(exact_fields) + " match")
        if proposed_workspace == project.workspace:
            reasons.append("workspace path collision")
        if proposed_repository == project.git.repository:
            reasons.append("repository collision")
        similarity = _similarity(display_name, project.name)
        if project_type == project.type and similarity >= 0.62:
            reasons.append(f"similar {project.type} project name")
        if reasons:
            results.append(
                {
                    "project_id": project.id,
                    "display_name": project.name,
                    "type": project.type,
                    "repository": project.git.repository,
                    "reason": "; ".join(reasons),
                    "similarity": round(similarity, 3),
                }
            )
    return results


def plan_project(config: AppConfig, request: ProjectPlanRequest) -> dict[str, Any]:
    display_name = _validate_text(request.name, "project name", path_like=True)
    goal = _validate_text(request.goal, "goal")
    project_type = request.project_type.strip().lower()
    if project_type not in PROJECT_TYPES:
        raise ProjectPlanError("project type is unsupported")
    if project_type == "venture" and config.venture_defaults is None:
        raise ProjectPlanError("venture defaults are not configured")
    visibility = (request.visibility or "private").strip().lower()
    if visibility not in VISIBILITIES:
        raise ProjectPlanError("visibility is unsupported")

    slug = slugify(display_name)
    registry = RegistryReader(config).load()
    projects = tuple(registry.projects)
    root = _workspace_root(projects, config)
    proposed_workspace = _safe_child(root, slug)
    repository_owner = _repository_owner(projects)
    repository_name = slug
    repository_full_name = f"{repository_owner}/{repository_name}"
    knowledge_path = _safe_child(config.knowledge_root, f"{slug}.md")
    lead_agent_id = f"{slug}-lead"
    lead_display_name = f"{display_name} Lead"
    if project_type == "venture":
        lead_display_name = f"{display_name} Venture Lead"
    beads = _beads_policy(project_type, display_name, goal)
    duplicate_candidates = _duplicate_candidates(
        projects=projects,
        slug=slug,
        display_name=display_name,
        project_type=project_type,
        proposed_workspace=proposed_workspace,
        proposed_repository=repository_full_name,
    )
    create_new = not duplicate_candidates
    matched = duplicate_candidates[0] if duplicate_candidates else None

    registry_entry = {
        "id": slug,
        "name": display_name,
        "type": project_type,
        "lifecycle": "planned",
        "phase": "PLANNING",
        "agent_id": lead_agent_id,
        "workspace": str(proposed_workspace),
        "git": {
            "repository": repository_full_name,
            "canonical_branch": "main",
            "active_branch": "main",
            "draft_pr": None,
        },
        "beads": {"enabled": beads["enabled"], "directory": str(proposed_workspace / ".beads")},
        "policies": {
            "manager_write_access": False,
            "direct_merge": False,
            "public_publish": "approval_required",
        },
    }
    venture = _venture_plan(config.venture_defaults) if project_type == "venture" else None
    if venture is not None:
        registry_entry["phase"] = venture["lifecycle_stage"]
        registry_entry["venture"] = venture

    result = {
        "project_id": slug,
        "slug": slug,
        "display_name": display_name,
        "type": project_type,
        "goal": goal,
        "visibility": visibility,
        "proposed_workspace": str(proposed_workspace),
        "proposed_github_repository": {
            "owner": repository_owner,
            "name": repository_name,
            "full_name": repository_full_name,
            "visibility": visibility,
            "private": visibility == "private",
            "create": False,
        },
        "proposed_lead": {
            "agent_id": lead_agent_id,
            "display_name": lead_display_name,
            "role": "PROJECT_LEAD",
            "create": False,
        },
        "beads": beads,
        "knowledge_path": str(knowledge_path),
        "registry_entry": registry_entry,
        "planned_files": [
            {"path": "README.md", "template": f"{project_type}-readme"},
            {"path": "PROJECT.md", "template": "project-plan"},
            {"path": ".gitignore", "template": "standard"},
        ],
        "approvals_required": _approvals(project_type, visibility),
        "duplicate_candidates": duplicate_candidates,
        "duplicate_decision": {
            "create_new": create_new,
            "matched_project_id": matched["project_id"] if matched else None,
            "reason": matched["reason"] if matched else None,
        },
        "warnings": _warnings(project_type, visibility),
        "resource_policy": {
            "execution_mode": "sequential",
            "max_heavy_agents": 1,
            "subagents": "disabled_by_default",
            "long_running_work": "Lead must split work into small tasks.",
            "always_on_services": ["Agent Ops", "OpenClaw Gateway"],
            "concurrency_limit": "Paper Lead and the new project Lead must not run heavy work at the same time.",
        },
        "executable": False,
    }
    if venture is not None:
        result["phase"] = venture["lifecycle_stage"]
        result["proposed_lead"]["specialization"] = "VENTURE"
        result["venture"] = venture
        result["missing_user_inputs"] = venture["missing_user_inputs"]
        result["approvals_required"] = _approvals(project_type, visibility) + _venture_approvals(
            config.venture_defaults
        )
    return result
