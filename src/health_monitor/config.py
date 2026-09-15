"""Configuration loading and validation for the health-monitor service.

Configuration is a JSON document applied on top of the built-in defaults.
Individual scalars may be overridden from the environment using the
``HM_`` prefix and a double-underscore path separator, for example
``HM_HEARTBEAT__PEER_TIMEOUT_S=4.0``. Environment overrides exist for bench
work; the deployed unit runs from the JSON file alone.

Every value is validated at load time. The service refuses to start on an
invalid configuration rather than falling back to a default, so that a
mis-provisioned unit fails loudly at commissioning.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

ENV_PREFIX = "HM_"


class ConfigError(ValueError):
    """Raised when the supplied configuration cannot be used."""


@dataclass
class MemoryCheckConfig:
    """FN-HM-RAM. Periodic integrity sweep over the protected memory regions."""

    enabled: bool = True
    scan_period_s: float = 5.0
    region_bytes: int = 1 << 20
    pages_per_slice: int = 64
    slice_budget_ms: float = 1.5
    correctable_warn_threshold: int = 1
    uncorrectable_fault_threshold: int = 1


@dataclass
class StorageCheckConfig:
    """FN-HM-DISK. Periodic capacity, latency and writability probe."""

    enabled: bool = True
    scan_period_s: float = 30.0
    mounts: List[str] = field(default_factory=lambda: ["/var/log", "/var/lib/cases"])
    free_warn_fraction: float = 0.20
    free_fault_fraction: float = 0.08
    probe_write_bytes: int = 4096
    probe_latency_warn_ms: float = 40.0
    probe_latency_fault_ms: float = 250.0
    stall_threshold_s: float = 120.0


@dataclass
class HeartbeatCheckConfig:
    """FN-HM-HB. Liveness of the peer services on the platform bus."""

    enabled: bool = True
    emit_period_s: float = 1.0
    peer_timeout_s: float = 3.0
    peers: List[str] = field(
        default_factory=lambda: [
            "generator-control",
            "mapping-engine",
            "catheter-io",
            "case-recorder",
        ]
    )
    quorum_min_live: int = 3


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"
    include_monotonic: bool = True


@dataclass
class ServiceConfig:
    service_name: str = "health-monitor"
    instance_id: str = "hm-0"
    loop_period_s: float = 0.25
    shutdown_grace_s: float = 5.0
    bus_socket: str = "/run/platform/bus.sock"
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    memory: MemoryCheckConfig = field(default_factory=MemoryCheckConfig)
    storage: StorageCheckConfig = field(default_factory=StorageCheckConfig)
    heartbeat: HeartbeatCheckConfig = field(default_factory=HeartbeatCheckConfig)


_SECTIONS: Dict[str, type] = {
    "logging": LoggingConfig,
    "memory": MemoryCheckConfig,
    "storage": StorageCheckConfig,
    "heartbeat": HeartbeatCheckConfig,
}


def _coerce(target_type: Any, raw: Any, path: str) -> Any:
    if target_type is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
        raise ConfigError(f"{path}: expected a boolean, got {raw!r}")
    if target_type is float:
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: expected a number, got {raw!r}") from exc
    if target_type is int:
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: expected an integer, got {raw!r}") from exc
    if target_type is str:
        return str(raw)
    # List[str]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(part) for part in raw]
    raise ConfigError(f"{path}: expected a list of strings, got {raw!r}")


def _apply_section(instance: Any, overrides: Mapping[str, Any], prefix: str) -> None:
    known = {f.name: f for f in fields(instance)}
    for key, value in overrides.items():
        if key not in known:
            raise ConfigError(f"{prefix}{key}: unknown configuration key")
        declared = known[key].type
        if declared in (bool, "bool"):
            coerced = _coerce(bool, value, f"{prefix}{key}")
        elif declared in (float, "float"):
            coerced = _coerce(float, value, f"{prefix}{key}")
        elif declared in (int, "int"):
            coerced = _coerce(int, value, f"{prefix}{key}")
        elif declared in (str, "str"):
            coerced = _coerce(str, value, f"{prefix}{key}")
        else:
            coerced = _coerce(list, value, f"{prefix}{key}")
        setattr(instance, key, coerced)


def _env_overrides(environ: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    collected: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_value in environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        remainder = raw_key[len(ENV_PREFIX):].lower()
        if "__" not in remainder:
            collected.setdefault("", {})[remainder] = raw_value
            continue
        section, _, leaf = remainder.partition("__")
        collected.setdefault(section, {})[leaf] = raw_value
    return collected


def from_mapping(document: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> ServiceConfig:
    """Build a :class:`ServiceConfig` from a mapping plus optional env overrides."""
    config = ServiceConfig()

    top_level = {k: v for k, v in document.items() if k not in _SECTIONS}
    _apply_section(config, top_level, "")
    for section_name, section_type in _SECTIONS.items():
        section_doc = document.get(section_name, {})
        if not isinstance(section_doc, Mapping):
            raise ConfigError(f"{section_name}: expected an object")
        section_obj = getattr(config, section_name)
        if not is_dataclass(section_obj):  # pragma: no cover - defensive
            raise ConfigError(f"{section_name}: not a configurable section")
        _apply_section(section_obj, section_doc, f"{section_name}.")

    for section_name, overrides in _env_overrides(environ or {}).items():
        if section_name == "":
            _apply_section(config, overrides, "")
        elif section_name in _SECTIONS:
            _apply_section(getattr(config, section_name), overrides, f"{section_name}.")
        else:
            raise ConfigError(f"{ENV_PREFIX}{section_name.upper()}__*: unknown section")

    validate(config)
    return config


def load(path: str | Path, environ: Mapping[str, str] | None = None) -> ServiceConfig:
    """Load and validate configuration from a JSON file on disk."""
    file_path = Path(path)
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file_path}: malformed JSON at line {exc.lineno}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{file_path}: top level must be an object")
    return from_mapping(document, environ if environ is not None else os.environ)


def _require_positive(value: float, path: str) -> None:
    if value <= 0:
        raise ConfigError(f"{path}: must be greater than zero, got {value!r}")


def _require_fraction(value: float, path: str) -> None:
    if not 0.0 < value < 1.0:
        raise ConfigError(f"{path}: must be strictly between 0 and 1, got {value!r}")


def validate(config: ServiceConfig) -> None:
    """Raise :class:`ConfigError` on any value the service cannot honour."""
    _require_positive(config.loop_period_s, "loop_period_s")
    _require_positive(config.shutdown_grace_s, "shutdown_grace_s")
    if not config.service_name:
        raise ConfigError("service_name: must not be empty")

    mem = config.memory
    _require_positive(mem.scan_period_s, "memory.scan_period_s")
    _require_positive(mem.slice_budget_ms, "memory.slice_budget_ms")
    if mem.pages_per_slice <= 0:
        raise ConfigError("memory.pages_per_slice: must be greater than zero")
    if mem.region_bytes <= 0:
        raise ConfigError("memory.region_bytes: must be greater than zero")

    sto = config.storage
    _require_positive(sto.scan_period_s, "storage.scan_period_s")
    _require_positive(sto.stall_threshold_s, "storage.stall_threshold_s")
    _require_fraction(sto.free_warn_fraction, "storage.free_warn_fraction")
    _require_fraction(sto.free_fault_fraction, "storage.free_fault_fraction")
    if sto.free_fault_fraction >= sto.free_warn_fraction:
        raise ConfigError(
            "storage.free_fault_fraction: must be below storage.free_warn_fraction"
        )
    if sto.probe_latency_fault_ms <= sto.probe_latency_warn_ms:
        raise ConfigError(
            "storage.probe_latency_fault_ms: must exceed storage.probe_latency_warn_ms"
        )
    if sto.enabled and not sto.mounts:
        raise ConfigError("storage.mounts: at least one mount point is required")
    if sto.stall_threshold_s <= sto.scan_period_s:
        raise ConfigError(
            "storage.stall_threshold_s: must exceed storage.scan_period_s"
        )

    hbt = config.heartbeat
    _require_positive(hbt.emit_period_s, "heartbeat.emit_period_s")
    _require_positive(hbt.peer_timeout_s, "heartbeat.peer_timeout_s")
    if hbt.peer_timeout_s <= hbt.emit_period_s:
        raise ConfigError(
            "heartbeat.peer_timeout_s: must exceed heartbeat.emit_period_s so that a "
            "single late heartbeat does not declare a peer lost"
        )
    if hbt.enabled and not hbt.peers:
        raise ConfigError("heartbeat.peers: at least one peer is required")
    if hbt.quorum_min_live > len(hbt.peers):
        raise ConfigError(
            "heartbeat.quorum_min_live: cannot exceed the number of configured peers"
        )
    if config.logging.level.upper() not in (
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    ):
        raise ConfigError(f"logging.level: unsupported level {config.logging.level!r}")


def describe(config: ServiceConfig) -> Dict[str, Any]:
    """Flatten the configuration for emission into the startup log record."""

    def _flatten(prefix: str, obj: Any) -> List[Tuple[str, Any]]:
        out: List[Tuple[str, Any]] = []
        for f in fields(obj):
            value = getattr(obj, f.name)
            if is_dataclass(value):
                out.extend(_flatten(f"{prefix}{f.name}.", value))
            else:
                out.append((f"{prefix}{f.name}", value))
        return out

    return dict(_flatten("", config))
