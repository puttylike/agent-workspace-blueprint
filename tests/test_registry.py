from __future__ import annotations

from pathlib import Path

import pytest

from agent_workspace.controller.registry import (
    RegistryReader,
    secure_path_within,
)
from agent_workspace.models import ConfigurationError, SecurityBoundaryError


def _registry(path: Path, workspace: Path, beads_binary: Path) -> None:
    path.write_text(
        f"""version: 1
projects:
  - id: sample-paper
    name: Sample Paper
    type: paper
    lifecycle: active
    phase: WRITING
    agent_id: sample-lead
    workspace: {workspace}
    git:
      repository: example/sample
      canonical_branch: agent/canonical
      active_branch: research/draft
      draft_pr: 7
    beads:
      binary: {beads_binary}
      directory: {workspace / '.beads'}
    policies:
      manager_write_access: false
      direct_merge: false
      public_publish: approval_required
""",
        encoding="utf-8",
    )


def test_registry_parses_declared_project(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".beads").mkdir()
    beads_binary = tmp_path / "bd"
    beads_binary.touch()
    source = tmp_path / "projects.yaml"
    _registry(source, workspace, beads_binary)

    registry = RegistryReader(source).load()

    assert registry.version == 1
    assert len(registry.projects) == 1
    project = registry.projects[0]
    assert project.id == "sample-paper"
    assert project.phase == "WRITING"
    assert project.git.repository == "example/sample"
    assert project.policies.manager_write_access is False


def test_registry_rejects_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "projects.yaml"
    source.write_text(
        "version: 1\nprojects:\n  - id: bad\n    workspace: /srv/../escape\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        RegistryReader(source).load()


def test_secure_path_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_text("outside", encoding="utf-8")
    (workspace / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SecurityBoundaryError):
        secure_path_within(workspace, workspace / "link" / "payload")


def test_secure_path_rejects_parent_segments(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    with pytest.raises(SecurityBoundaryError):
        secure_path_within(workspace, Path("..") / "outside")
