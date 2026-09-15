"""Value types shared across the health-monitor checks."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class HealthState(enum.IntEnum):
    """Severity ladder. Ordered so aggregation can take the maximum."""

    OK = 0
    DEGRADED = 1
    FAULT = 2
    UNKNOWN = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class CheckResult:
    """A single observation emitted by a periodic check.

    ``subject`` distinguishes multiple monitored entities inside one check,
    for example a specific mount point or a specific peer process. It is the
    empty string for checks that report a single aggregate value.
    """

    check: str
    state: HealthState
    subject: str = ""
    detail: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    monotonic_ts: float = 0.0

    def topic(self) -> str:
        if self.subject:
            return f"health.{self.check}.{self.subject}"
        return f"health.{self.check}"

    def as_payload(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "subject": self.subject,
            "state": self.state.label,
            "severity": int(self.state),
            "detail": self.detail,
            "metrics": dict(self.metrics),
            "monotonic_ts": self.monotonic_ts,
        }


@dataclass
class CheckStats:
    """Rolling counters kept per check for the service status endpoint."""

    runs: int = 0
    faults: int = 0
    degraded: int = 0
    skipped_slots: int = 0
    last_state: Optional[HealthState] = None
    last_run_monotonic: Optional[float] = None

    def record(self, result: Optional[CheckResult], missed_slots: int = 0) -> None:
        self.skipped_slots += missed_slots
        if result is None:
            return
        self.runs += 1
        self.last_state = result.state
        self.last_run_monotonic = result.monotonic_ts
        if result.state is HealthState.FAULT:
            self.faults += 1
        elif result.state is HealthState.DEGRADED:
            self.degraded += 1

    def as_payload(self) -> Dict[str, Any]:
        return {
            "runs": self.runs,
            "faults": self.faults,
            "degraded": self.degraded,
            "skipped_slots": self.skipped_slots,
            "last_state": self.last_state.label if self.last_state else None,
            "last_run_monotonic": self.last_run_monotonic,
        }
