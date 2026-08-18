"""Private Agent Ops configuration loader."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import (
    AppConfig,
    CommandConfig,
    ConfigurationError,
    OpenClawConfig,
    RuntimeConfig,
)

_MAX_CONFIG_BYTES = 1_048_576
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be a mapping")
    return value


def _integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(f"{label} must be a normalized absolute path")
    return Path(os.path.abspath(path))


def _executable(value: Any, default_name: str, label: str) -> Path:
    if value is None:
        resolved = shutil.which(default_name)
        if not resolved:
            raise ConfigurationError(f"{label} executable is unavailable")
        return Path(resolved).resolve()
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{label} executable must be a path or command name")
    if Path(value).is_absolute():
        return Path(value)
    resolved = shutil.which(value)
    if not resolved:
        raise ConfigurationError(f"{label} executable is unavailable")
    return Path(resolved).resolve()


def _load_yaml(path: Path) -> Mapping[str, Any]:
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigurationError("configuration path must be a normalized absolute path")
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("configuration path must be a regular, non-symlink file")
    if path.stat().st_size > _MAX_CONFIG_BYTES:
        raise ConfigurationError("configuration file is too large")
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"could not parse configuration: {exc.__class__.__name__}") from exc
    return _mapping(parsed, "configuration")


def load_app_config(path: str | Path) -> AppConfig:
    source_path = Path(path)
    raw = _load_yaml(source_path)

    version = _integer(raw.get("version", 1), "version", minimum=1, maximum=1)
    mode = str(raw.get("mode", "read_only")).lower()
    if mode not in {"read_only", "readonly"}:
        raise ConfigurationError("only read_only mode is supported")

    listen_host = raw.get("listen_host", "127.0.0.1")
    listen_port = _integer(raw.get("listen_port", 3001), "listen_port", minimum=1, maximum=65_535)
    if listen_host != "127.0.0.1" or listen_port != 3001:
        raise ConfigurationError("the MVP may listen only on 127.0.0.1:3001")

    project_registry = _absolute_path(raw.get("project_registry"), "project_registry")
    knowledge_root = _absolute_path(raw.get("knowledge_root"), "knowledge_root")
    blueprint_root = _absolute_path(
        raw.get("public_blueprint_path", raw.get("blueprint_root")), "public_blueprint_path"
    )

    runtime_raw = raw.get("runtime", {})
    if runtime_raw is None:
        runtime_raw = {}
    runtime_map = _mapping(runtime_raw, "runtime")
    sqlite_value = raw.get("runtime_sqlite_cache", runtime_map.get("sqlite_cache"))
    runtime = RuntimeConfig(_absolute_path(sqlite_value, "runtime_sqlite_cache"))

    commands_raw = raw.get("commands", {})
    if commands_raw is None:
        commands_raw = {}
    commands_map = _mapping(commands_raw, "commands")
    timeout_value = raw.get("command_timeout_seconds", commands_map.get("timeout_seconds", 10))
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise ConfigurationError("command_timeout_seconds must be numeric")
    timeout = float(timeout_value)
    if not 0.1 <= timeout <= 60:
        raise ConfigurationError("command_timeout_seconds must be from 0.1 to 60")
    stdout_limit = _integer(
        commands_map.get("stdout_limit_bytes", 1_048_576),
        "stdout_limit_bytes",
        minimum=1_024,
        maximum=16_777_216,
    )
    stderr_limit = _integer(
        commands_map.get("stderr_limit_bytes", 65_536),
        "stderr_limit_bytes",
        minimum=1_024,
        maximum=1_048_576,
    )
    commands = CommandConfig(
        git=_executable(commands_map.get("git"), "git", "git"),
        gh=_executable(commands_map.get("gh"), "gh", "gh"),
        openclaw=_executable(commands_map.get("openclaw"), "openclaw", "openclaw"),
        timeout_seconds=timeout,
        stdout_limit_bytes=stdout_limit,
        stderr_limit_bytes=stderr_limit,
    )

    cache_ttl = _integer(raw.get("cache_ttl_seconds", 30), "cache_ttl_seconds", minimum=0, maximum=86_400)
    openclaw_raw = raw.get("openclaw", {})
    if isinstance(openclaw_raw, str):
        openclaw_map: Mapping[str, Any] = {"access_method": openclaw_raw}
    else:
        openclaw_map = _mapping(openclaw_raw or {}, "openclaw")
    access_method = raw.get(
        "openclaw_gateway_access_identifier", openclaw_map.get("access_method", "local_gateway_rpc")
    )
    if access_method != "local_gateway_rpc":
        raise ConfigurationError("only local_gateway_rpc OpenClaw access is supported")
    legacy_raw = openclaw_map.get("legacy_agents", [])
    if not isinstance(legacy_raw, list) or any(not isinstance(item, str) for item in legacy_raw):
        raise ConfigurationError("openclaw.legacy_agents must be a string list")
    manager_agent_id = openclaw_map.get("manager_agent_id")
    if manager_agent_id is not None and (
        not isinstance(manager_agent_id, str) or not _AGENT_ID.fullmatch(manager_agent_id)
    ):
        raise ConfigurationError("openclaw.manager_agent_id must be a valid agent id")
    openclaw = OpenClawConfig(
        str(access_method), tuple(legacy_raw), manager_agent_id
    )

    return AppConfig(
        version=version,
        mode="read_only",
        listen_host=listen_host,
        listen_port=listen_port,
        project_registry=project_registry,
        knowledge_root=knowledge_root,
        blueprint_root=blueprint_root,
        runtime=runtime,
        commands=commands,
        cache_ttl_seconds=cache_ttl,
        openclaw=openclaw,
        source_path=source_path,
    )
