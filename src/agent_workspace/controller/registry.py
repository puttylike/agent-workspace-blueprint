"""Project registry parsing and workspace containment checks."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..models import (
    AppConfig,
    BeadsProjectConfig,
    ConfigurationError,
    GitProjectConfig,
    ProjectPolicies,
    ProjectRecord,
    ProjectRegistry,
    SecurityBoundaryError,
)

_MAX_REGISTRY_BYTES = 1_048_576
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be a mapping")
    return value


def _text(value: Any, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty string")
    result = value.strip()
    if "\x00" in result or (identifier and not _IDENTIFIER.fullmatch(result)):
        raise ConfigurationError(f"{label} has an invalid value")
    return result


def declared_absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ConfigurationError(f"{label} must be a normalized absolute path")
    return Path(os.path.abspath(path))


def secure_workspace_root(path: str | Path) -> Path:
    """Resolve an existing root while rejecting symlinked path components."""

    declared = Path(path)
    if not declared.is_absolute() or ".." in declared.parts:
        raise SecurityBoundaryError("workspace must be a normalized absolute path")
    lexical = Path(os.path.abspath(declared))
    try:
        resolved = declared.resolve(strict=True)
    except OSError as exc:
        raise SecurityBoundaryError("registered workspace is unavailable") from exc
    if lexical != resolved:
        raise SecurityBoundaryError("symlinked workspace roots are not allowed")
    if not resolved.is_dir():
        raise SecurityBoundaryError("registered workspace is not a directory")
    return resolved


def secure_path_within(
    workspace: str | Path,
    candidate: str | Path,
    *,
    must_exist: bool = True,
    require_directory: bool = False,
) -> Path:
    """Resolve a candidate and prove it cannot escape its registered root."""

    root = secure_workspace_root(workspace)
    raw = Path(candidate)
    if ".." in raw.parts:
        raise SecurityBoundaryError("path traversal is not allowed")
    combined = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(combined))
    try:
        if os.path.commonpath((str(root), str(lexical))) != str(root):
            raise SecurityBoundaryError("path is outside the registered workspace")
    except ValueError as exc:
        raise SecurityBoundaryError("path is outside the registered workspace") from exc

    if must_exist:
        try:
            resolved = combined.resolve(strict=True)
        except OSError as exc:
            raise SecurityBoundaryError("registered project path is unavailable") from exc
        if resolved != lexical:
            raise SecurityBoundaryError("symlink escapes are not allowed")
    else:
        parent = combined.parent
        try:
            resolved_parent = parent.resolve(strict=True)
        except OSError as exc:
            raise SecurityBoundaryError("project path parent is unavailable") from exc
        if resolved_parent != Path(os.path.abspath(parent)):
            raise SecurityBoundaryError("symlink escapes are not allowed")
        resolved = resolved_parent / combined.name
    if os.path.commonpath((str(root), str(resolved))) != str(root):
        raise SecurityBoundaryError("path is outside the registered workspace")
    if require_directory and not resolved.is_dir():
        raise SecurityBoundaryError("registered project path is not a directory")
    return resolved


class RegistryReader:
    def __init__(self, source: AppConfig | str | Path) -> None:
        self.path = source.project_registry if isinstance(source, AppConfig) else Path(source)

    def load(self) -> ProjectRegistry:
        path = self.path
        if not path.is_absolute() or ".." in path.parts:
            raise ConfigurationError("registry path must be a normalized absolute path")
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError("registry path must be a regular, non-symlink file")
        if path.stat().st_size > _MAX_REGISTRY_BYTES:
            raise ConfigurationError("registry file is too large")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"could not parse registry: {exc.__class__.__name__}") from exc
        root = _mapping(raw, "registry")
        version = root.get("version")
        if version != 1:
            raise ConfigurationError("registry version must be 1")
        entries = root.get("projects")
        if not isinstance(entries, list):
            raise ConfigurationError("projects must be a list")

        projects: list[ProjectRecord] = []
        seen: set[str] = set()
        for index, value in enumerate(entries):
            label = f"projects[{index}]"
            item = _mapping(value, label)
            project_id = _text(item.get("id"), f"{label}.id", identifier=True)
            if project_id in seen:
                raise ConfigurationError(f"duplicate project id: {project_id}")
            seen.add(project_id)
            agent_id = _text(item.get("agent_id"), f"{label}.agent_id", identifier=True)
            workspace = declared_absolute_path(item.get("workspace"), f"{label}.workspace")

            git_raw = _mapping(item.get("git"), f"{label}.git")
            repository = _text(git_raw.get("repository"), f"{label}.git.repository")
            if not _REPOSITORY.fullmatch(repository):
                raise ConfigurationError(f"{label}.git.repository must be an owner/name identifier")
            canonical = _text(git_raw.get("canonical_branch"), f"{label}.git.canonical_branch")
            active = _text(git_raw.get("active_branch"), f"{label}.git.active_branch")
            if not _BRANCH.fullmatch(canonical) or not _BRANCH.fullmatch(active):
                raise ConfigurationError(f"{label}.git branch has an invalid value")
            draft_pr_raw = git_raw.get("draft_pr")
            if draft_pr_raw is not None and (
                isinstance(draft_pr_raw, bool) or not isinstance(draft_pr_raw, int) or draft_pr_raw < 1
            ):
                raise ConfigurationError(f"{label}.git.draft_pr must be a positive integer")

            beads_raw = _mapping(item.get("beads"), f"{label}.beads")
            beads_binary = declared_absolute_path(beads_raw.get("binary"), f"{label}.beads.binary")
            beads_directory = declared_absolute_path(beads_raw.get("directory"), f"{label}.beads.directory")
            try:
                if os.path.commonpath((str(workspace), str(beads_directory))) != str(workspace):
                    raise ConfigurationError(f"{label}.beads.directory must be inside the workspace")
            except ValueError as exc:
                raise ConfigurationError(f"{label}.beads.directory must be inside the workspace") from exc

            policy_raw = _mapping(item.get("policies", {}), f"{label}.policies")
            manager_write = policy_raw.get("manager_write_access", False)
            direct_merge = policy_raw.get("direct_merge", False)
            if not isinstance(manager_write, bool) or not isinstance(direct_merge, bool):
                raise ConfigurationError(f"{label}.policies write flags must be booleans")
            if manager_write or direct_merge:
                raise ConfigurationError("the read-only MVP rejects write-enabled project policies")
            public_publish = _text(
                policy_raw.get("public_publish", "approval_required"),
                f"{label}.policies.public_publish",
            )

            legacy = item.get("legacy_workspace")
            if legacy is not None and not isinstance(legacy, str):
                raise ConfigurationError(f"{label}.legacy_workspace must be a string")
            projects.append(
                ProjectRecord(
                    id=project_id,
                    name=_text(item.get("name"), f"{label}.name"),
                    type=_text(item.get("type"), f"{label}.type", identifier=True),
                    lifecycle=_text(item.get("lifecycle"), f"{label}.lifecycle", identifier=True),
                    phase=_text(item.get("phase"), f"{label}.phase", identifier=True).upper(),
                    agent_id=agent_id,
                    workspace=workspace,
                    legacy_workspace=legacy,
                    git=GitProjectConfig(repository, canonical, active, draft_pr_raw),
                    beads=BeadsProjectConfig(beads_binary, beads_directory),
                    policies=ProjectPolicies(manager_write, direct_merge, public_publish),
                )
            )
        return ProjectRegistry(1, tuple(projects))


def load_registry(path: str | Path) -> ProjectRegistry:
    return RegistryReader(path).load()
