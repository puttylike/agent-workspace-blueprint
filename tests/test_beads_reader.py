from __future__ import annotations

import json

from agent_workspace.controller.beads_reader import BeadsReader
from agent_workspace.models import CommandResult

from conftest import FakeRunner


def _result(value: object) -> CommandResult:
    return CommandResult(0, json.dumps(value), "")


def test_beads_parses_list_ready_and_status_counts(sample_project) -> None:
    tasks = [
        {"id": "task-1", "status": "open"},
        {"id": "task-2", "status": "open"},
        {"id": "task-3", "status": "closed"},
    ]
    ready = [{"id": "task-1", "status": "open"}]
    runner = FakeRunner(
        [CommandResult(0, "bd version 1.2.2", ""), _result(tasks), _result(ready)]
    )

    observation = BeadsReader(runner, cache_ttl_seconds=0).read(sample_project)

    assert observation.availability == "AVAILABLE"
    assert observation.data is not None
    assert observation.data["list_count"] == 3
    assert observation.data["ready_count"] == 1
    assert observation.data["counts"]["blocked"] == 1
    assert observation.data["completed_count"] == 1
    assert observation.data["progress_percent"] == 33.3
    assert [call[0][1:] for call in runner.calls] == [
        ["--version"],
        ["list", "--json"],
        ["ready", "--json"],
    ]


def test_zero_tasks_has_no_fake_progress(sample_project) -> None:
    runner = FakeRunner(
        [CommandResult(0, "bd version 1.2.2", ""), _result([]), _result([])]
    )
    observation = BeadsReader(runner, cache_ttl_seconds=0).read(sample_project)
    assert observation.data is not None
    assert observation.data["list_count"] == 0
    assert observation.data["progress_percent"] is None


def test_raw_dolt_metadata_is_ignored(sample_project) -> None:
    noms = sample_project.beads.directory / "noms"
    noms.mkdir()
    manifest = noms / "manifest"
    manifest.write_text("before", encoding="utf-8")

    def read_once():
        runner = FakeRunner(
            [CommandResult(0, "bd version 1.2.2", ""), _result([]), _result([])]
        )
        return BeadsReader(runner, cache_ttl_seconds=0).read(sample_project).data

    before = read_once()
    manifest.write_text("after", encoding="utf-8")
    after = read_once()
    assert before == after
    assert "manifest" not in json.dumps(after)
