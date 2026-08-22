from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Iterable

import pytest

from agent_workspace.models import (
    BeadsProjectConfig,
    CommandResult,
    GitProjectConfig,
    ProjectPolicies,
    ProjectRecord,
)


class FakeRunner:
    def __init__(self, results: Iterable[CommandResult]) -> None:
        self.results = deque(results)
        self.calls: list[tuple[list[str], Path]] = []
        self.timeout_seconds = 1.0
        self.stdout_limit_bytes = 65_536
        self.stderr_limit_bytes = 16_384

    def run(self, argv: list[str], *, cwd: Path) -> CommandResult:
        self.calls.append((list(argv), Path(cwd)))
        if not self.results:
            raise AssertionError("unexpected command invocation")
        return self.results.popleft()


@pytest.fixture
def sample_project(tmp_path: Path) -> ProjectRecord:
    workspace = tmp_path / "project"
    workspace.mkdir()
    beads = workspace / ".beads"
    beads.mkdir()
    (beads / "interactions.jsonl").write_text("", encoding="utf-8")
    binary = tmp_path / "bd"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    return ProjectRecord(
        id="sample-paper",
        name="Sample Paper",
        type="article",
        lifecycle="active",
        phase="WRITING",
        agent_id="sample-lead",
        workspace=workspace,
        legacy_workspace=None,
        git=GitProjectConfig(
            repository="example/sample",
            canonical_branch="agent/canonical",
            active_branch="research/draft",
            draft_pr=7,
        ),
        beads=BeadsProjectConfig(binary=binary, directory=beads),
        policies=ProjectPolicies(),
    )
