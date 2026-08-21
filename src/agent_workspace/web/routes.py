"""HTML and JSON routes for the read-only Agent Ops dashboard."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
import inspect
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .wiki import UnsafeWikiPath, WikiDocumentNotFound, WikiError


class SourceMeta(BaseModel):
    availability: str
    queried_at: str
    observed_at: str | None = None
    last_success_at: str | None = None
    stale: bool = False
    message: str | None = None


class CollectionEnvelope(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    meta: SourceMeta


class ProjectEnvelope(BaseModel):
    project: dict[str, Any] | None = None
    meta: SourceMeta


class WikiSearchEnvelope(CollectionEnvelope):
    query: str


@dataclass(slots=True)
class _CachedValue:
    value: Any
    observed_at: str


@dataclass(slots=True)
class _Observation:
    value: Any
    availability: str
    queried_at: str
    observed_at: str | None
    stale: bool = False
    message: str | None = None

    def meta(self) -> dict[str, Any]:
        return {
            "availability": self.availability,
            "queried_at": self.queried_at,
            "observed_at": self.observed_at,
            "last_success_at": self.observed_at if self.stale else None,
            "stale": self.stale,
            "message": self.message,
        }


class SourceCache:
    """Process-local last-success cache used only for graceful degradation."""

    def __init__(self) -> None:
        self._values: dict[str, _CachedValue] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: Any, observed_at: str) -> None:
        async with self._lock:
            self._values[key] = _CachedValue(value=value, observed_at=observed_at)

    async def get(self, key: str) -> _CachedValue | None:
        async with self._lock:
            return self._values.get(key)


_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "oauth_profile",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session_key",
    "ssh_key",
    "token",
}
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL | re.IGNORECASE,
)
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
_TOKEN_SHAPE = re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SESSION_KEY = re.compile(r"\bagent:[A-Za-z0-9_-]+(?::[^\s\"'<>/:]+){2,}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _redact_text(value: str) -> str:
    value = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    value = _URL_CREDENTIALS.sub(r"\g<scheme>[REDACTED]@", value)
    value = _TOKEN_SHAPE.sub("[REDACTED_TOKEN]", value)
    return _SESSION_KEY.sub("[REDACTED_SESSION_KEY]", value)


def _public_value(value: Any, *, key: str | None = None) -> Any:
    """Convert controller values to JSON-safe, display-safe structures."""

    normalized_key = key.casefold().replace("-", "_") if key else None
    if normalized_key in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (Path, date, datetime)):
        return _redact_text(str(value))
    if isinstance(value, BaseModel):
        return _public_value(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _public_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _public_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_public_value(item) for item in value]
    try:
        return _public_value(jsonable_encoder(value))
    except Exception:
        return "UNAVAILABLE"


async def _invoke(method: Callable[..., Any], *args: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args)
    result = await run_in_threadpool(method, *args)
    if isinstance(result, Awaitable) or inspect.isawaitable(result):
        return await result
    return result


async def _observe(
    request: Request,
    *,
    key: str,
    method_name: str,
    args: tuple[Any, ...] = (),
    empty: Any,
) -> _Observation:
    queried_at = _utc_now()
    service = getattr(request.app.state, "status_service", None)
    cache: SourceCache = request.app.state.source_cache
    method = getattr(service, method_name, None) if service is not None else None
    if method is not None:
        try:
            source_timeout = getattr(request.app.state, "source_timeout_seconds", 8.0)
            invocation = _invoke(method, *args)
            value = _public_value(
                await asyncio.wait_for(invocation, timeout=source_timeout)
                if source_timeout
                else await invocation
            )
            observed_at = _utc_now()
            await cache.put(key, value, observed_at)
            return _Observation(value, "AVAILABLE", queried_at, observed_at)
        except (TimeoutError, asyncio.TimeoutError, Exception):
            # Raw provider/command errors must not be reflected into logs or HTML.
            pass
    cached = await cache.get(key)
    if cached is not None:
        return _Observation(
            cached.value,
            "UNAVAILABLE",
            queried_at,
            cached.observed_at,
            stale=True,
            message="Live source unavailable; showing the last successful observation.",
        )
    return _Observation(
        empty,
        "UNAVAILABLE",
        queried_at,
        None,
        message="Source temporarily unavailable.",
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: Any, *keys: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (list, tuple)):
                return list(candidate)
        candidate = value.get("items")
        if isinstance(candidate, (list, tuple)):
            return list(candidate)
    return []


def _first(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is not None:
            return value
    return default


def _count_by_status(tasks: list[Any], status: str) -> int:
    return sum(
        1
        for item in tasks
        if isinstance(item, Mapping) and str(item.get("status", "")).casefold() == status.casefold()
    )


def _project_view(value: Any, *, queried_at: str | None = None) -> dict[str, Any]:
    project = _mapping(value)
    registry = _mapping(project.get("registry")) or project
    git = _mapping(project.get("git"))
    beads = _mapping(project.get("beads")) or _mapping(project.get("tasks"))
    github = _mapping(project.get("github"))
    sources = _mapping(project.get("sources"))
    raw_task_list = _first(beads.get("list"), project.get("beads_list"), default=[])
    task_list = _items(raw_task_list, "tasks") if isinstance(raw_task_list, Mapping) else (
        list(raw_task_list) if isinstance(raw_task_list, (list, tuple)) else []
    )
    raw_ready_list = _first(beads.get("ready"), project.get("beads_ready"), default=[])
    ready_list = list(raw_ready_list) if isinstance(raw_ready_list, (list, tuple)) else []
    counts = _mapping(_first(beads.get("counts"), project.get("task_counts"), default={}))
    service_progress = _mapping(project.get("progress"))

    open_count = _first(counts.get("open"), beads.get("open_count"))
    ready_count = _first(counts.get("ready"), beads.get("ready_count"))
    blocked_count = _first(counts.get("blocked"), beads.get("blocked_count"))
    completed_count = _first(
        counts.get("completed"),
        counts.get("closed"),
        beads.get("completed_count"),
        service_progress.get("completed"),
    )
    total_count = _first(counts.get("total"), beads.get("total_count"))
    if open_count is None and task_list:
        open_count = _count_by_status(task_list, "open")
    if ready_count is None:
        ready_count = len(ready_list)
    if blocked_count is None and task_list:
        blocked_count = _count_by_status(task_list, "blocked")
    if completed_count is None and task_list:
        completed_count = _count_by_status(task_list, "closed") + _count_by_status(task_list, "completed")
    if total_count is None and task_list:
        total_count = len(task_list)

    progress: int | None = None
    if isinstance(total_count, int) and total_count > 0 and isinstance(completed_count, int):
        progress = max(0, min(100, round(completed_count * 100 / total_count)))
    elif (
        isinstance(service_progress.get("percent"), int)
        and isinstance(service_progress.get("total"), int)
        and service_progress["total"] > 0
    ):
        progress = max(0, min(100, service_progress["percent"]))

    git_status = _first(git.get("status"), project.get("git_status"), default="UNKNOWN")
    clean = _first(git.get("clean"), project.get("git_clean"))
    if clean is None and isinstance(git_status, str):
        if git_status.casefold() == "clean":
            clean = True
        elif git_status.casefold() == "dirty":
            clean = False

    pull_request = _mapping(
        _first(
            github.get("pull_request"),
            github.get("pr"),
            project.get("pull_request"),
            project.get("pr"),
            default={},
        )
    )
    if pull_request:
        pull_request = dict(pull_request)
        pull_request["state"] = _first(
            pull_request.get("state"), pull_request.get("display_state"), default="UNKNOWN"
        )
        pull_request["is_draft"] = bool(
            _first(pull_request.get("is_draft"), pull_request.get("draft"), default=False)
        )
    if not pull_request and registry.get("draft_pr") is not None:
        pull_request = {"number": registry.get("draft_pr"), "state": "UNKNOWN"}
    github_source = _mapping(sources.get("github"))
    if not pull_request and github_source.get("availability") in {"UNKNOWN", "UNAVAILABLE"}:
        pull_request = {
            "number": None,
            "state": github_source.get("availability"),
            "is_draft": False,
        }

    source_alerts = [
        {"source": source_name, "availability": source.get("availability", "UNKNOWN")}
        for source_name, raw_source in sources.items()
        for source in [_mapping(raw_source)]
        if source.get("availability") not in {None, "AVAILABLE"}
    ]

    needs_user = _first(
        project.get("user_confirmation_required"),
        project.get("needs_user"),
        project.get("user_action_required"),
        default=[],
    )
    if needs_user is True:
        needs_user = ["User confirmation required"]
    elif needs_user in (False, None):
        needs_user = []
    elif isinstance(needs_user, str):
        needs_user = [needs_user]

    recent_commit_raw = _first(
        git.get("recent_commit"), git.get("latest_commit"), project.get("recent_commit")
    )
    if isinstance(recent_commit_raw, Mapping):
        recent_commit: dict[str, Any] | None = dict(recent_commit_raw)
    elif isinstance(recent_commit_raw, str) and recent_commit_raw:
        recent_commit = {"subject": recent_commit_raw}
    else:
        recent_commit = None

    return {
        "id": _first(project.get("id"), registry.get("id"), default="unknown"),
        "name": _first(project.get("name"), registry.get("name"), default="Unnamed project"),
        "type": _first(project.get("type"), registry.get("type"), default="unknown"),
        "lifecycle": _first(project.get("lifecycle"), registry.get("lifecycle"), default="UNKNOWN"),
        "phase": _first(project.get("phase"), registry.get("phase"), default="UNKNOWN"),
        "agent_id": _first(project.get("agent_id"), registry.get("agent_id"), default="UNKNOWN"),
        "branch": _first(git.get("branch"), git.get("current_branch"), project.get("branch"), default="UNKNOWN"),
        "head": _first(git.get("head"), git.get("head_sha"), project.get("head"), default="UNKNOWN"),
        "git_status": git_status,
        "git_clean": clean,
        "ahead": _first(git.get("ahead"), git.get("upstream_ahead"), project.get("ahead")),
        "behind": _first(git.get("behind"), git.get("upstream_behind"), project.get("behind")),
        "tasks": {
            "open": open_count,
            "ready": ready_count,
            "blocked": blocked_count,
            "completed": completed_count,
            "total": total_count,
        },
        "progress_percent": progress,
        "beads_version": _first(beads.get("version"), project.get("beads_version")),
        "beads_list": task_list,
        "beads_ready": ready_list,
        "recent_commit": recent_commit,
        "pull_request": pull_request,
        "needs_user": needs_user,
        "last_queried_at": queried_at,
        "sources": sources,
        "source_alerts": source_alerts,
    }


def _agent_view(value: Any) -> dict[str, Any]:
    agent = _mapping(value)
    return {
        "id": _first(agent.get("id"), agent.get("agent_id"), default="unknown"),
        "name": _first(agent.get("name"), agent.get("id"), agent.get("agent_id"), default="Unknown agent"),
        "exists": _first(agent.get("exists"), agent.get("available")),
        "status": _first(
            agent.get("status"), agent.get("lifecycle"), agent.get("availability"), default="UNKNOWN"
        ),
        "last_activity_at": _first(agent.get("last_activity_at"), agent.get("recent_session_at")),
        "role": agent.get("role"),
    }


def _activity_view(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    return {
        "type": _first(item.get("type"), item.get("kind"), default="activity"),
        "title": _first(item.get("title"), item.get("summary"), item.get("message"), default="Activity"),
        "project_id": item.get("project_id"),
        "timestamp": _first(
            item.get("timestamp"), item.get("occurred_at"), item.get("created_at"), item.get("at")
        ),
        "url": item.get("url"),
    }


def _base_context(request: Request, *, title: str, active: str) -> dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "active": active,
        "read_only": True,
    }


async def _wiki_documents(request: Request) -> _Observation:
    queried_at = _utc_now()
    cache: SourceCache = request.app.state.source_cache
    wiki = getattr(request.app.state, "wiki_service", None)
    if wiki is not None:
        try:
            documents = await run_in_threadpool(wiki.documents)
            value = [_public_value(document.to_dict(include_body=False)) for document in documents]
            observed_at = _utc_now()
            await cache.put("wiki:documents", value, observed_at)
            return _Observation(value, "AVAILABLE", queried_at, observed_at)
        except Exception:
            pass
    cached = await cache.get("wiki:documents")
    if cached:
        return _Observation(
            cached.value,
            "UNAVAILABLE",
            queried_at,
            cached.observed_at,
            stale=True,
            message="Wiki source unavailable; showing the last successful index.",
        )
    return _Observation([], "UNAVAILABLE", queried_at, None, message="Wiki source unavailable.")


async def _wiki_search(request: Request, query: str) -> _Observation:
    queried_at = _utc_now()
    wiki = getattr(request.app.state, "wiki_service", None)
    if wiki is None:
        return _Observation([], "UNAVAILABLE", queried_at, None, message="Wiki search unavailable.")
    try:
        results = await run_in_threadpool(wiki.search, query)
        value = [_public_value(result.to_dict()) for result in results]
        return _Observation(value, "AVAILABLE", queried_at, _utc_now())
    except Exception:
        return _Observation([], "UNAVAILABLE", queried_at, None, message="Wiki search unavailable.")


def create_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", name="healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": "read-only"}

    @router.get("/api/v1/projects", response_model=CollectionEnvelope)
    async def api_projects(request: Request) -> dict[str, Any]:
        observation = await _observe(
            request, key="projects", method_name="projects", empty=[]
        )
        projects = [
            _project_view(project, queried_at=observation.queried_at)
            for project in _items(observation.value, "projects")
        ]
        return {"items": projects, "meta": observation.meta()}

    @router.get("/api/v1/projects/{project_id}", response_model=ProjectEnvelope)
    async def api_project(project_id: str, request: Request) -> dict[str, Any]:
        observation = await _observe(
            request,
            key=f"project:{project_id}",
            method_name="project",
            args=(project_id,),
            empty=None,
        )
        if observation.value is None and observation.availability == "AVAILABLE":
            raise HTTPException(status_code=404, detail="Project not found")
        project = (
            _project_view(observation.value, queried_at=observation.queried_at)
            if observation.value is not None
            else None
        )
        return {"project": project, "meta": observation.meta()}

    @router.get("/api/v1/agents", response_model=CollectionEnvelope)
    async def api_agents(request: Request) -> dict[str, Any]:
        observation = await _observe(request, key="agents", method_name="agents", empty=[])
        agents = [_agent_view(agent) for agent in _items(observation.value, "agents")]
        return {"items": agents, "meta": observation.meta()}

    @router.get("/api/v1/activity", response_model=CollectionEnvelope)
    async def api_activity(request: Request) -> dict[str, Any]:
        observation = await _observe(request, key="activity", method_name="activity", empty=[])
        activity = [_activity_view(item) for item in _items(observation.value, "activity", "events")]
        return {"items": activity, "meta": observation.meta()}

    @router.get("/api/v1/wiki/search", response_model=WikiSearchEnvelope)
    async def api_wiki_search(
        request: Request,
        q: str = Query(default="", max_length=200),
    ) -> dict[str, Any]:
        observation = await _wiki_search(request, q)
        return {"items": observation.value, "meta": observation.meta(), "query": q}

    @router.get("/", response_class=HTMLResponse, name="dashboard")
    async def dashboard(request: Request) -> HTMLResponse:
        observation = await _observe(request, key="projects", method_name="projects", empty=[])
        projects = [
            _project_view(project, queried_at=observation.queried_at)
            for project in _items(observation.value, "projects")
        ]
        context = _base_context(request, title="Agent Ops", active="dashboard")
        context.update({"projects": projects, "source": observation.meta()})
        return templates.TemplateResponse(request=request, name="dashboard.html", context=context)

    @router.get("/projects", response_class=HTMLResponse, name="projects")
    async def projects_page(request: Request) -> HTMLResponse:
        observation = await _observe(request, key="projects", method_name="projects", empty=[])
        projects = [
            _project_view(project, queried_at=observation.queried_at)
            for project in _items(observation.value, "projects")
        ]
        context = _base_context(request, title="Projects", active="projects")
        context.update({"projects": projects, "source": observation.meta()})
        return templates.TemplateResponse(request=request, name="projects.html", context=context)

    @router.get("/projects/{project_id}", response_class=HTMLResponse, name="project_detail")
    async def project_page(project_id: str, request: Request) -> HTMLResponse:
        observation = await _observe(
            request,
            key=f"project:{project_id}",
            method_name="project",
            args=(project_id,),
            empty=None,
        )
        if observation.value is None and observation.availability == "AVAILABLE":
            raise HTTPException(status_code=404, detail="Project not found")
        project = (
            _project_view(observation.value, queried_at=observation.queried_at)
            if observation.value is not None
            else None
        )
        context = _base_context(
            request,
            title=project["name"] if project else "Project unavailable",
            active="projects",
        )
        context.update({"project": project, "source": observation.meta()})
        return templates.TemplateResponse(request=request, name="project_detail.html", context=context)

    @router.get("/agents", response_class=HTMLResponse, name="agents")
    async def agents_page(request: Request) -> HTMLResponse:
        observation = await _observe(request, key="agents", method_name="agents", empty=[])
        agents = [_agent_view(agent) for agent in _items(observation.value, "agents")]
        context = _base_context(request, title="Agents", active="agents")
        context.update({"agents": agents, "source": observation.meta()})
        return templates.TemplateResponse(request=request, name="agents.html", context=context)

    @router.get("/activity", response_class=HTMLResponse, name="activity")
    async def activity_page(request: Request) -> HTMLResponse:
        observation = await _observe(request, key="activity", method_name="activity", empty=[])
        activity = [_activity_view(item) for item in _items(observation.value, "activity", "events")]
        context = _base_context(request, title="Activity", active="activity")
        context.update({"activity": activity, "source": observation.meta()})
        return templates.TemplateResponse(request=request, name="activity.html", context=context)

    @router.get("/wiki", response_class=HTMLResponse, name="wiki")
    async def wiki_page(
        request: Request,
        q: str = Query(default="", max_length=200),
    ) -> HTMLResponse:
        documents = await _wiki_documents(request)
        search = await _wiki_search(request, q) if q else None
        context = _base_context(request, title="Wiki", active="wiki")
        context.update(
            {
                "documents": documents.value,
                "source": documents.meta(),
                "query": q,
                "search_results": search.value if search else None,
                "search_source": search.meta() if search else None,
            }
        )
        return templates.TemplateResponse(request=request, name="wiki_index.html", context=context)

    @router.get("/wiki/{path:path}", response_class=HTMLResponse, name="wiki_document")
    async def wiki_document(path: str, request: Request) -> HTMLResponse:
        wiki = getattr(request.app.state, "wiki_service", None)
        if wiki is None:
            raise HTTPException(status_code=503, detail="Wiki unavailable")
        try:
            document = await run_in_threadpool(wiki.read, path)
        except (UnsafeWikiPath, WikiDocumentNotFound):
            raise HTTPException(status_code=404, detail="Wiki document not found") from None
        except WikiError:
            raise HTTPException(status_code=503, detail="Wiki unavailable") from None
        context = _base_context(request, title=document.title, active="wiki")
        context.update({"document": _public_value(document.to_dict())})
        return templates.TemplateResponse(request=request, name="wiki_document.html", context=context)

    return router


__all__ = [
    "CollectionEnvelope",
    "ProjectEnvelope",
    "SourceMeta",
    "WikiSearchEnvelope",
    "create_router",
]
