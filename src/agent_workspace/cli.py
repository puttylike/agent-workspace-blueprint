"""Command-line entry point for the read-only Workspace Controller."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Sequence


LOOPBACK_HOST = "127.0.0.1"
AGENT_OPS_PORT = 3001


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _text_plan(value: dict[str, Any]) -> None:
    print(f"Project: {value['display_name']} ({value['project_id']})")
    print(f"Type: {value['type']}")
    print(f"Visibility: {value['visibility']}")
    print(f"Workspace: {value['proposed_workspace']}")
    print(f"Repository: {value['proposed_github_repository']['full_name']}")
    print(
        "Lead: "
        f"{value['proposed_lead']['display_name']} "
        f"({value['proposed_lead']['agent_id']}, {value['proposed_lead']['role']})"
    )
    print(f"Beads: {value['beads']['enabled']} - {value['beads']['reason']}")
    print(f"Executable: {value['executable']}")


def _config_path(value: str | None) -> Path:
    candidate = value or os.environ.get("AWC_CONFIG") or os.environ.get(
        "AGENT_OPS_CONFIG"
    )
    if not candidate:
        raise ValueError(
            "A private config path is required via --config or AWC_CONFIG"
        )
    return Path(candidate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="awc", description="Read-only Agent Workspace Controller"
    )
    parser.add_argument("--config", help="path to the private agent-ops YAML")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="validate configuration and read sources")

    projects = commands.add_parser("projects", help="inspect registered projects")
    project_commands = projects.add_subparsers(dest="projects_command", required=True)
    project_commands.add_parser("list", help="list registered projects")
    show = project_commands.add_parser("show", help="show one registered project")
    show.add_argument("project_id")
    plan = project_commands.add_parser("plan", help="plan a project without creating it")
    plan.add_argument("--name", required=True, help="project display name")
    plan.add_argument(
        "--type",
        required=True,
        choices=("paper", "blog", "quant", "app", "contest", "misc", "venture"),
        dest="project_type",
        help="project type",
    )
    plan.add_argument("--goal", required=True, help="project goal")
    plan.add_argument(
        "--visibility",
        choices=("private", "public"),
        default="private",
        help="planned repository visibility",
    )
    plan.add_argument("--json", action="store_true", help="emit a stable JSON plan")
    apply = project_commands.add_parser(
        "apply",
        help="validate an approval-bound plan and emit its action manifest",
    )
    apply.add_argument("--plan-file", required=True, help="canonical JSON plan file")
    apply.add_argument(
        "--approval-sha256",
        required=True,
        help="exact SHA-256 of canonical plan bytes",
    )
    apply.add_argument("--json", action="store_true", help="emit JSON validation output")

    agents = commands.add_parser("agents", help="inspect configured agents")
    agent_commands = agents.add_subparsers(dest="agents_command", required=True)
    agent_commands.add_parser("list", help="list agents and last activity")

    wiki = commands.add_parser("wiki", help="operate the disposable Wiki index")
    wiki_commands = wiki.add_subparsers(dest="wiki_command", required=True)
    wiki_commands.add_parser("index", help="rebuild the SQLite FTS5 cache")

    serve = commands.add_parser("serve", help="serve the read-only Agent Ops UI")
    serve.add_argument("--host", default=LOOPBACK_HOST)
    serve.add_argument("--port", default=AGENT_OPS_PORT, type=int)
    return parser


def _load(path: Path) -> tuple[Any, Any]:
    from .config import load_app_config
    from .controller.status_service import StatusService

    config = load_app_config(path)
    return config, StatusService.from_config(config)


def _load_config(path: Path) -> Any:
    from .config import load_app_config

    return load_app_config(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "projects" and args.projects_command == "apply":
        from .controller.project_apply import ApplyContractError, validate_approved_plan

        try:
            result = validate_approved_plan(
                Path(args.plan_file),
                args.approval_sha256,
            )
        except ApplyContractError as exc:
            message = {"error": "invalid_project_apply", "reason": str(exc)}
            if args.json:
                _json(message)
            else:
                print(f"awc: {message['error']}: {message['reason']}")
            return 2
        if args.json:
            _json(result)
        else:
            manifest = result["action_manifest"]
            print(f"Validation: {result['status']}")
            print(f"Schema: {result['schema_version']}")
            print(f"Project: {manifest['project_id']}")
            print(f"Plan SHA-256: {result['plan_sha256']}")
            print(f"Manifest SHA-256: {manifest['manifest_sha256']}")
            print("Actions: " + ", ".join(item["action"] for item in manifest["ordered_actions"]))
            print("Execution performed: false")
        return 0
    try:
        config_path = _config_path(args.config)
        if args.command == "projects" and args.projects_command == "plan":
            config = _load_config(config_path)
            service = None
        else:
            config, service = _load(config_path)
    except Exception as exc:
        # Configuration errors are deliberately generic: private paths and
        # credentials from lower layers are never reflected to the terminal.
        parser.exit(2, f"awc: configuration unavailable ({type(exc).__name__})\n")

    if args.command == "doctor":
        result = service.doctor()
        _json(result)
        return 0 if result.get("status") == "PASS" else 1

    if args.command == "projects":
        if args.projects_command == "plan":
            from .controller.project_planner import (
                ProjectPlanError,
                ProjectPlanRequest,
                plan_project,
            )
            from .models import SecurityBoundaryError

            try:
                result = plan_project(
                    config,
                    ProjectPlanRequest(
                        name=args.name,
                        project_type=args.project_type,
                        goal=args.goal,
                        visibility=args.visibility,
                    ),
                )
            except (ProjectPlanError, SecurityBoundaryError) as exc:
                message = {"error": "invalid_project_plan", "reason": str(exc)}
                if args.json:
                    _json(message)
                else:
                    print(f"awc: {message['error']}: {message['reason']}")
                return 2
            if args.json:
                _json(result)
            else:
                _text_plan(result)
            return 0
        if args.projects_command == "list":
            assert service is not None
            _json(service.projects())
            return 0
        assert service is not None
        project = service.project(args.project_id)
        if project is None:
            _json({"error": "project_not_found", "project_id": args.project_id})
            return 2
        _json(project)
        return 0

    if args.command == "agents":
        _json(service.agents())
        return 0

    if args.command == "wiki":
        from .web.wiki import WikiIndex

        index = WikiIndex(config.knowledge_root, config.sqlite_cache)
        _json(index.index())
        return 0

    if args.command == "serve":
        if args.host != LOOPBACK_HOST or args.port != AGENT_OPS_PORT:
            parser.error("the MVP may bind only to 127.0.0.1:3001")
        import uvicorn

        from .web.app import create_app

        uvicorn.run(
            create_app(config, status_service=service),
            host=LOOPBACK_HOST,
            port=AGENT_OPS_PORT,
            reload=False,
            access_log=False,
        )
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
