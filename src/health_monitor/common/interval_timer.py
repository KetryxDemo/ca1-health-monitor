"""Monotonic interval timer shared by the health-monitor periodic checks.

The timer serves two independent concerns that happen to share the same
monotonic clock read:

1. Cadence. ``poll().due`` goes true once every ``period_s`` seconds, and the
   caller acknowledges the slot by calling :meth:`mark_serviced`. Cadence is
   always active.

2. Expiry. If, and only if, ``expiry_s`` is supplied at construction time, the
   timer additionally tracks how long it has been since the last call to
   :meth:`refresh` and reports ``poll().expired`` once that gap exceeds the
   configured expiry window. With ``expiry_s`` left at ``None`` the expiry
   fields are inert: ``expiry_enabled`` is ``False``, ``expired`` is always
   ``False`` and ``time_since_refresh`` is ``None``.

Callers that only need a cadence construct the timer with a period alone.
Callers that need to detect a missing external event construct it with both a
period and an expiry window.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

Clock = Callable[[], float]

DEFAULT_CLOCK: Clock = time.monotonic


class TimerConfigurationError(ValueError):
    """Raised when a timer is constructed with a nonsensical period or expiry."""


@dataclass(frozen=True)
class TimerTick:
    """Immutable snapshot of a timer taken at a single clock read.

    Attributes
    ----------
    now:
        The monotonic clock value the snapshot was taken at.
    due:
        True when at least ``period_s`` has elapsed since the last serviced
        cadence slot.
    elapsed_since_service:
        Seconds since :meth:`IntervalTimer.mark_serviced` was last called.
    overdue_by:
        Seconds past the scheduled due instant. Zero when not yet due. Used to
        detect scheduler starvation.
    missed_slots:
        Whole cadence periods that elapsed without a call to
        :meth:`IntervalTimer.mark_serviced`.
    expiry_enabled:
        True when the timer was constructed with an expiry window.
    expired:
        True when expiry is enabled and the refresh gap exceeds the window.
        Always False when expiry is disabled.
    time_since_refresh:
        Seconds since :meth:`IntervalTimer.refresh`, or None when expiry is
        disabled.
    """

    now: float
    due: bool
    elapsed_since_service: float
    overdue_by: float
    missed_slots: int
    expiry_enabled: bool
    expired: bool
    time_since_refresh: Optional[float]


class IntervalTimer:
    """Thread-safe periodic timer with an optional expiry window.

    Parameters
    ----------
    period_s:
        Cadence in seconds. Must be strictly positive.
    expiry_s:
        Optional expiry window in seconds. When omitted the timer never
        reports expiry. When supplied it must be strictly positive.
    clock:
        Monotonic clock source. Injected so tests can drive time directly.
    name:
        Diagnostic label carried into log records by the caller.
    start_due:
        When True the first :meth:`poll` after construction reports ``due``,
        so a check runs immediately at startup rather than after one period.
    """

    __slots__ = (
        "_expiry_intervals",
        "_period_s",
        "_expiry_s",
        "_clock",
        "_lock",
        "_name",
        "_last_service",
        "_last_refresh",
        "_created_at",
    )

    def __init__(
        self,
        period_s: float,
        expiry_s: Optional[float] = None,
        clock: Clock = DEFAULT_CLOCK,
        name: str = "interval-timer",
        start_due: bool = False,
    ) -> None:
        if period_s <= 0:
            raise TimerConfigurationError(
                f"{name}: period_s must be > 0, got {period_s!r}"
            )
        if expiry_s is not None and expiry_s <= 0:
            raise TimerConfigurationError(
                f"{name}: expiry_s must be > 0 when supplied, got {expiry_s!r}"
            )

        self._period_s = float(period_s)
        self._expiry_s = None if expiry_s is None else float(expiry_s)
        self._expiry_intervals = None if expiry_s is None else math.ceil(expiry_s / period_s)
        self._clock = clock
        self._name = name
        self._lock = threading.Lock()

        now = clock()
        self._created_at = now
        self._last_service = now - self._period_s if start_due else now
        self._last_refresh = now

    @property
    def name(self) -> str:
        return self._name

    @property
    def period_s(self) -> float:
        return self._period_s

    @property
    def expiry_s(self) -> Optional[float]:
        return self._expiry_s

    @property
    def expiry_enabled(self) -> bool:
        """True when this timer was given an expiry window to enforce."""
        return self._expiry_s is not None

    def poll(self, now: Optional[float] = None) -> TimerTick:
        """Take a snapshot of the timer without mutating cadence state."""
        with self._lock:
            reading = self._clock() if now is None else float(now)

            elapsed_since_service = reading - self._last_service
            due = elapsed_since_service >= self._period_s
            overdue_by = (
                max(0.0, elapsed_since_service - self._period_s) if due else 0.0
            )
            missed_slots = (
                max(0, int(elapsed_since_service // self._period_s) - 1) if due else 0
            )

            if self._expiry_s is None:
                return TimerTick(
                    now=reading,
                    due=due,
                    elapsed_since_service=elapsed_since_service,
                    overdue_by=overdue_by,
                    missed_slots=missed_slots,
                    expiry_enabled=False,
                    expired=False,
                    time_since_refresh=None,
                )

            time_since_refresh = reading - self._last_refresh
            expired = int(time_since_refresh // self._period_s) >= self._expiry_intervals

            return TimerTick(
                now=reading,
                due=due,
                elapsed_since_service=elapsed_since_service,
                overdue_by=overdue_by,
                missed_slots=missed_slots,
                expiry_enabled=True,
                expired=expired,
                time_since_refresh=time_since_refresh,
            )

    def mark_serviced(self, now: Optional[float] = None) -> None:
        """Acknowledge the current cadence slot.

        The next due instant is anchored to the acknowledgement, not to the
        original schedule, so a slow check degrades cadence rather than
        accumulating a backlog of catch-up runs.
        """
        with self._lock:
            self._last_service = self._clock() if now is None else float(now)

    def refresh(self, now: Optional[float] = None) -> None:
        """Record that the event this timer watches for has been observed.

        A no-op on timers constructed without an expiry window, other than
        recording the timestamp.
        """
        with self._lock:
            self._last_refresh = self._clock() if now is None else float(now)

    def reset(self, now: Optional[float] = None) -> None:
        """Reset both cadence and expiry anchors, as on reconnect or restart."""
        with self._lock:
            reading = self._clock() if now is None else float(now)
            self._last_service = reading
            self._last_refresh = reading

    def seconds_until_due(self, now: Optional[float] = None) -> float:
        """Seconds remaining before the next cadence slot. Zero when due."""
        with self._lock:
            reading = self._clock() if now is None else float(now)
            return max(0.0, self._period_s - (reading - self._last_service))

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"IntervalTimer(name={self._name!r}, period_s={self._period_s}, "
            f"expiry_s={self._expiry_s})"
        )
