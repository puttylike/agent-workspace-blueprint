from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from agent_workspace.controller.git_reader import GitReader
from agent_workspace.models import BoundedCommandRunner, ReaderError


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [shutil.which("git") or "/usr/bin/git", *args],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def test_git_reader_reports_branch_head_and_clean_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Fixture Author")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "fixture")

    runner = BoundedCommandRunner(3, 65_536, 16_384)
    reader = GitReader(Path(shutil.which("git") or "/usr/bin/git"), runner, cache_ttl_seconds=0)
    clean = reader.read_workspace(repo)
    assert clean.availability == "AVAILABLE"
    assert clean.data is not None
    assert clean.data["branch"] == "main"
    assert clean.data["clean"] is True
    assert len(str(clean.data["head"])) == 40

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    dirty = GitReader(
        Path(shutil.which("git") or "/usr/bin/git"), runner, cache_ttl_seconds=0
    ).read_workspace(repo)
    assert dirty.data is not None
    assert dirty.data["clean"] is False
    assert dirty.data["status"] == "DIRTY"


def test_git_reader_rejects_write_command(tmp_path: Path) -> None:
    runner = BoundedCommandRunner(1, 4096, 4096)
    reader = GitReader(Path(shutil.which("git") or "/usr/bin/git"), runner)
    with pytest.raises(ReaderError):
        reader._run(tmp_path, ("commit", "-m", "forbidden"))
