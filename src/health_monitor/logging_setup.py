"""Structured logging for the health-monitor service.

Records are emitted as one JSON object per line on stdout. The platform log
shipper tails stdout per service unit, so anything written here is captured in
the case record without further plumbing. Wall-clock time is recorded for human
correlation; the monotonic clock is recorded alongside it because wall clock
can step during a case if the unit resynchronises with the site time source.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, Iterable, Optional

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def __init__(
        self,
        service_name: str,
        instance_id: str,
        include_monotonic: bool = True,
    ) -> None:
        super().__init__()
        self._service_name = service_name
        self._instance_id = instance_id
        self._include_monotonic = include_monotonic

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self._service_name,
            "instance": self._instance_id,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if self._include_monotonic:
            payload["monotonic_s"] = round(time.monotonic(), 6)

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _safe(value)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), sort_keys=False)


class PlainFormatter(logging.Formatter):
    """Human-readable fallback used on the bench."""

    def __init__(self, service_name: str, instance_id: str) -> None:
        super().__init__(
            fmt=f"%(asctime)s %(levelname)-7s [{service_name}/{instance_id}] "
            "%(name)s: %(message)s"
        )


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value]
    return repr(value)


def configure(
    level: str = "INFO",
    fmt: str = "json",
    service_name: str = "health-monitor",
    instance_id: str = "hm-0",
    include_monotonic: bool = True,
    stream: Optional[Any] = None,
) -> logging.Logger:
    """Install the root handler and return the service logger."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    if fmt == "json":
        handler.setFormatter(
            JsonFormatter(service_name, instance_id, include_monotonic)
        )
    else:
        handler.setFormatter(PlainFormatter(service_name, instance_id))

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logging.getLogger(service_name)


def log_fields(**fields: Any) -> Dict[str, Any]:
    """Convenience wrapper so call sites read ``extra=log_fields(...)``."""
    return dict(fields)


def suppress_noisy(loggers: Iterable[str], level: int = logging.WARNING) -> None:
    for name in loggers:
        logging.getLogger(name).setLevel(level)
