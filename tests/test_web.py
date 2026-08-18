from __future__ import annotations

from pathlib import Path
import shutil

from fastapi.testclient import TestClient

from agent_workspace.web.app import create_app
from agent_workspace.web.wiki import WikiIndex, render_markdown


class FakeStatusService:
    def __init__(self) -> None:
        self._project = {
            "id": "sample-paper",
            "name": "Sample Paper",
            "type": "paper",
            "lifecycle": "active",
            "phase": "WRITING",
            "agent_id": "sample-lead",
            "git": {
                "branch": "research/draft",
                "head": "a" * 40,
                "status": "CLEAN",
                "clean": True,
                "ahead": 0,
                "behind": 0,
                "latest_commit": {
                    "sha": "a" * 40,
                    "committed_at": "2030-01-01T00:00:00Z",
                    "subject": "fixture",
                },
            },
            "beads": {
                "version": "1.2.2",
                "list": [],
                "ready": [],
                "counts": {"open": 0, "ready": 0, "blocked": 0},
                "completed_count": 0,
                "progress_percent": None,
            },
            "github": {
                "number": 7,
                "state": "OPEN",
                "is_draft": True,
                "display_state": "DRAFT",
            },
            "user_confirmation_required": [],
        }

    def projects(self):
        return [self._project]

    def project(self, project_id: str):
        return self._project if project_id == "sample-paper" else None

    def agents(self):
        synthetic_session = ":".join(
            ("agent", "sample-lead", "dashboard", "must-not-render")
        )
        return [
            {
                "id": "sample-lead",
                "status": "OPERATIONAL",
                "recent_session_at": "2030-01-01T00:00:00Z",
                "session_key": synthetic_session,
            },
            {"id": "legacy-default", "status": "LEGACY"},
        ]

    def activity(self):
        return [
            {
                "type": "git_commit",
                "title": "fixture",
                "project_id": "sample-paper",
                "timestamp": "2030-01-01T00:00:00Z",
            }
        ]


def _client(tmp_path: Path) -> tuple[TestClient, WikiIndex]:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "wiki" / "sample.md"
    shutil.copyfile(fixture, knowledge / "sample.md")
    wiki = WikiIndex(knowledge, tmp_path / "runtime" / "wiki.db")
    wiki.index()
    app = create_app(status_service=FakeStatusService(), wiki_service=wiki)
    return TestClient(app), wiki


def test_health_api_schema_and_mobile_html(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "mode": "read-only"}

    api = client.get("/api/v1/projects")
    assert api.status_code == 200
    payload = api.json()
    assert set(payload) == {"items", "meta"}
    assert len(payload["items"]) == 1
    project = payload["items"][0]
    assert project["phase"] == "WRITING"
    assert project["branch"] == "research/draft"
    assert project["git_clean"] is True
    assert project["tasks"]["ready"] == 0
    assert project["progress_percent"] is None

    page = client.get("/")
    assert page.status_code == 200
    assert 'name="viewport"' in page.text
    assert "Read-only" in page.text
    assert "Sample Paper" in page.text
    assert "must-not-render" not in page.text


def test_all_required_read_routes_and_no_write_routes(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    for path in (
        "/",
        "/projects",
        "/projects/sample-paper",
        "/agents",
        "/activity",
        "/wiki",
        "/wiki/sample.md",
        "/api/v1/projects",
        "/api/v1/projects/sample-paper",
        "/api/v1/agents",
        "/api/v1/activity",
        "/api/v1/wiki/search?q=searchable",
    ):
        assert client.get(path).status_code == 200, path

    route_methods = {
        method
        for route in client.app.routes
        for method in (getattr(route, "methods", None) or set())
    }
    assert route_methods <= {"GET", "HEAD"}
    for forbidden in (
        "/api/v1/projects",
        "/api/v1/projects/sample-paper",
        "/api/v1/agents",
    ):
        assert client.post(forbidden).status_code == 405
        assert client.put(forbidden).status_code == 405
        assert client.patch(forbidden).status_code == 405
        assert client.delete(forbidden).status_code == 405


def test_markdown_is_sanitized_and_wiki_searches_fts5(tmp_path: Path) -> None:
    client, wiki = _client(tmp_path)
    document = wiki.read("sample.md")
    assert "<script" not in document.html.lower()
    assert "javascript:" not in document.html.lower()
    assert "<script" not in render_markdown("<script>alert(1)</script>").lower()

    results = client.get("/api/v1/wiki/search", params={"q": "searchable"})
    assert results.status_code == 200
    assert results.json()["items"][0]["path"] == "sample.md"


def test_wiki_blocks_traversal_and_symlink_escape(tmp_path: Path) -> None:
    client, wiki = _client(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (wiki.knowledge_root / "escape.md").symlink_to(outside)
    assert client.get("/wiki/../outside.md").status_code in {404, 307}
    assert client.get("/wiki/escape.md").status_code == 404
