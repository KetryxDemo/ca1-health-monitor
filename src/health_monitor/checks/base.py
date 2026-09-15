"""Common scaffolding for the periodic checks owned by this service.

Every check owns exactly one cadence timer, which it constructs itself, and
returns zero or more :class:`CheckResult` values from :meth:`on_tick`. The base
class polls the timer, hands the resulting :class:`TimerTick` to the check,
publishes whatever comes back, and maintains the per-check counters exposed on
the service status topic.

The base class does not interpret the tick. It does not decide whether a check
runs, and it does not acknowledge the cadence slot. Both of those are the
check's own business, because the three checks in this service consume
different parts of the tick.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import ClassVar, List, Optional

from health_monitor.bus import MessageBus
from health_monitor.common.interval_timer import IntervalTimer, TimerTick
from health_monitor.common.types import CheckResult, CheckStats, HealthState

LOGGER = logging.getLogger(__name__)


class PeriodicCheck(ABC):
    """Base class for a check driven from the service control loop."""

    #: Stable identifier used in topics, metrics and log records.
    check_id: ClassVar[str] = "unnamed"

    def __init__(self, bus: MessageBus, timer: IntervalTimer) -> None:
        self._bus = bus
        self._timer = timer
        self._stats = CheckStats()
        self._last_published: dict[str, HealthState] = {}

    @property
    def timer(self) -> IntervalTimer:
        return self._timer

    @property
    def stats(self) -> CheckStats:
        return self._stats

    def attach(self, bus: MessageBus) -> None:
        """Hook for checks that need to consume bus traffic. No-op by default."""

    def service(self, now: Optional[float] = None) -> List[CheckResult]:
        """Advance the check by one control-loop iteration."""
        tick = self._timer.poll(now)
        try:
            results = self.on_tick(tick)
        except Exception:  # noqa: BLE001 - one failing check must not stop the loop
            LOGGER.exception(
                "check raised during service", extra={"check": self.check_id}
            )
            self._timer.mark_serviced(tick.now)
            return []

        for result in results:
            self._publish(result)
            self._stats.record(result, missed_slots=0)
        if not results:
            self._stats.record(None, missed_slots=tick.missed_slots)
        return results

    def _publish(self, result: CheckResult) -> None:
        previous = self._last_published.get(result.subject)
        self._last_published[result.subject] = result.state
        self._bus.publish(
            result.topic(),
            result.as_payload(),
            source=self.check_id,
            monotonic_ts=result.monotonic_ts,
        )
        if previous is not None and previous is not result.state:
            LOGGER.info(
                "health state transition",
                extra={
                    "check": self.check_id,
                    "subject": result.subject or "-",
                    "from_state": previous.label,
                    "to_state": result.state.label,
                    "detail": result.detail,
                },
            )

    def result(
        self,
        state: HealthState,
        detail: str = "",
        subject: str = "",
        monotonic_ts: Optional[float] = None,
        **metrics: object,
    ) -> CheckResult:
        return CheckResult(
            check=self.check_id,
            state=state,
            subject=subject,
            detail=detail,
            metrics=dict(metrics),
            monotonic_ts=time.monotonic() if monotonic_ts is None else monotonic_ts,
        )

    @abstractmethod
    def on_tick(self, tick: TimerTick) -> List[CheckResult]:
        """Do whatever this check needs to do for the supplied tick."""
        raise NotImplementedError
