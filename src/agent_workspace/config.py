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
    ExpectedAgentConfig,
    OpenClawConfig,
    RuntimeConfig,
    VentureBudgetConfig,
    VentureDefaultsConfig,
    VentureLifecycleConfig,
    VentureLocalRunnerConfig,
    VentureMonetizationConfig,
)

_MAX_CONFIG_BYTES = 1_048_576
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_AGENT_ROLE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{30,}\b", re.I),
    re.compile(r"\b(password|passwd|secret|token|credential|api[_-]?key|session[_-]?key)\b", re.I),
)
_VENTURE_STAGES = (
    "DISCOVER",
    "VALIDATE",
    "FRONTIER_PROTOTYPE",
    "NORMALIZE",
    "LOCAL_PARITY",
    "SHADOW_RUN",
    "AUTOMATED",
    "MONITORING",
    "SCALE",
    "RETIRE",
)
_VENTURE_APPROVALS = (
    "external_publishing",
    "account_creation",
    "payment",
    "affiliate_application",
    "production_deployment",
)
_VENTURE_PROHIBITED = ("live_trading",)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be a mapping")
    return value


def _known_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConfigurationError(f"{label} contains unknown fields")


def _contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _reject_secret_like(value: Any, label: str) -> None:
    if isinstance(value, str):
        if _contains_secret(value):
            raise ConfigurationError(f"{label} contains secret-like text")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_secret_like(item, f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_like(item, f"{label}[{index}]")


def _integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigurationError(f"{label} must be a non-empty string")
    result = value.strip()
    if _contains_secret(result):
        raise ConfigurationError(f"{label} contains secret-like text")
    return result


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label} must be a string list")
    result = tuple(_string(item, f"{label}[]") for item in value)
    return result


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


def _expected_agents(value: Any) -> tuple[ExpectedAgentConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigurationError("openclaw.expected_agents must be a list")
    agents: list[ExpectedAgentConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        agent = _mapping(item, f"openclaw.expected_agents[{index}]")
        agent_id = agent.get("id")
        if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
            raise ConfigurationError("openclaw.expected_agents.id must be a valid agent id")
        if agent_id in seen:
            raise ConfigurationError("openclaw.expected_agents contains duplicate ids")
        seen.add(agent_id)

        display_name = agent.get("display_name", agent.get("name"))
        if display_name is not None and (
            not isinstance(display_name, str) or not display_name.strip()
        ):
            raise ConfigurationError(
                "openclaw.expected_agents.display_name must be a non-empty string"
            )

        role = agent.get("role")
        if role is not None:
            if not isinstance(role, str) or not _AGENT_ROLE.fullmatch(role):
                raise ConfigurationError("openclaw.expected_agents.role must be an uppercase role")
            role = role.upper()

        agents.append(
            ExpectedAgentConfig(
                id=agent_id,
                display_name=display_name.strip() if isinstance(display_name, str) else None,
                role=role,
            )
        )
    return tuple(agents)


def _local_runner_executable(value: Any) -> Path | None:
    if value is None:
        return None
    path = _absolute_path(value, "venture_defaults.local_runner.executable")
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigurationError(
            "venture_defaults.local_runner.executable must be an executable regular, non-symlink file"
        )
    return path


def _venture_defaults(value: Any) -> VentureDefaultsConfig | None:
    if value is None:
        return None
    defaults_path = _absolute_path(value, "venture_defaults")
    document = _load_yaml(defaults_path)
    _known_keys(document, {"version", "venture_defaults"}, "venture defaults document")
    _integer(document.get("version"), "venture defaults version", minimum=1, maximum=1)
    root = _mapping(document.get("venture_defaults"), "venture_defaults")
    _known_keys(
        root,
        {
            "monetization",
            "monthly_incremental_budget",
            "lifecycle",
            "initial_phase",
            "local_runner",
            "approval_gates",
            "prohibited_actions",
            "evidence",
        },
        "venture_defaults",
    )
    _reject_secret_like(root, "venture_defaults")

    monetization_raw = _mapping(root.get("monetization"), "venture_defaults.monetization")
    _known_keys(monetization_raw, {"default_strategy"}, "venture_defaults.monetization")
    default_strategy = _string(
        monetization_raw.get("default_strategy"),
        "venture_defaults.monetization.default_strategy",
    )
    if default_strategy != "affiliate_first":
        raise ConfigurationError("venture default monetization strategy must be affiliate_first")

    budget_raw = _mapping(
        root.get("monthly_incremental_budget"),
        "venture_defaults.monthly_incremental_budget",
    )
    _known_keys(budget_raw, {"currency", "amount"}, "venture_defaults.monthly_incremental_budget")
    currency = _string(budget_raw.get("currency"), "venture_defaults.monthly_incremental_budget.currency")
    if currency != "KRW":
        raise ConfigurationError("venture monthly incremental budget currency must be KRW")
    amount = _integer(
        budget_raw.get("amount"),
        "venture_defaults.monthly_incremental_budget.amount",
        minimum=0,
        maximum=1_000_000_000,
    )

    stages = _string_list(root.get("lifecycle"), "venture_defaults.lifecycle")
    if stages != _VENTURE_STAGES:
        raise ConfigurationError("venture lifecycle must contain the required stages in order")
    initial_phase = _string(root.get("initial_phase"), "venture_defaults.initial_phase")
    if initial_phase != "DISCOVER":
        raise ConfigurationError("venture initial phase must be DISCOVER")

    runner_raw = _mapping(root.get("local_runner"), "venture_defaults.local_runner")
    _known_keys(
        runner_raw,
        {"required", "required_by_phase", "executable"},
        "venture_defaults.local_runner",
    )
    if runner_raw.get("required") is not True:
        raise ConfigurationError("venture local runner must be required")
    required_by_phase = _string(
        runner_raw.get("required_by_phase"),
        "venture_defaults.local_runner.required_by_phase",
    )
    if required_by_phase != "LOCAL_PARITY":
        raise ConfigurationError("venture local runner must be required by LOCAL_PARITY")

    approval_gates = _string_list(root.get("approval_gates"), "venture_defaults.approval_gates")
    if approval_gates != _VENTURE_APPROVALS:
        raise ConfigurationError("venture approval gates must match the required gates")
    prohibited_actions = _string_list(
        root.get("prohibited_actions"),
        "venture_defaults.prohibited_actions",
    )
    if prohibited_actions != _VENTURE_PROHIBITED:
        raise ConfigurationError("venture prohibited actions must contain only live_trading")

    evidence_raw = _mapping(root.get("evidence"), "venture_defaults.evidence")
    _known_keys(evidence_raw, {"actual_metrics"}, "venture_defaults.evidence")
    actual_metrics_policy = _string(
        evidence_raw.get("actual_metrics"),
        "venture_defaults.evidence.actual_metrics",
    )
    if actual_metrics_policy != "observed_only":
        raise ConfigurationError("venture actual metrics policy must be observed_only")

    return VentureDefaultsConfig(
        monetization=VentureMonetizationConfig(default_strategy=default_strategy),
        monthly_incremental_budget=VentureBudgetConfig(currency=currency, amount=amount),
        lifecycle=VentureLifecycleConfig(stages=stages, initial_phase=initial_phase),
        local_runner=VentureLocalRunnerConfig(
            required=True,
            required_by_phase=required_by_phase,
            executable=_local_runner_executable(runner_raw.get("executable")),
        ),
        approval_gates=approval_gates,
        prohibited_actions=prohibited_actions,
        actual_metrics_policy=actual_metrics_policy,
    )


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
    expected_raw = openclaw_map.get("expected_agents", raw.get("expected_agents"))
    openclaw = OpenClawConfig(
        str(access_method),
        tuple(legacy_raw),
        manager_agent_id,
        _expected_agents(expected_raw),
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
        venture_defaults=_venture_defaults(raw.get("venture_defaults")),
    )
