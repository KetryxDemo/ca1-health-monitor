"""FN-HM-RAM. Background integrity sweep over the protected memory regions.

The sweep walks the configured region in slices so that no single control-loop
iteration spends more than ``slice_budget_ms`` inside the check. A cursor is
carried between slices; a full pass completes over as many cadence slots as the
region size requires. Completing a pass publishes a result; partial slices do
not publish, so a slow pass does not flood the bus.

Correctable and uncorrectable error counts come from the platform ECC counter
source. Counters are monotonic since boot, so the check reports the delta
observed across one completed pass.
"""

from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol

from health_monitor.bus import MessageBus
from health_monitor.checks.base import PeriodicCheck
from health_monitor.common.interval_timer import IntervalTimer, TimerTick
from health_monitor.common.types import CheckResult, HealthState
from health_monitor.config import MemoryCheckConfig

LOGGER = logging.getLogger(__name__)

PAGE_BYTES = 4096


@dataclass(frozen=True)
class EccCounters:
    """Snapshot of the platform ECC counters, monotonic since boot."""

    correctable: int
    uncorrectable: int


class MemorySource(Protocol):
    """Everything the sweep needs from the platform underneath it."""

    def read_page(self, offset: int, length: int) -> bytes:
        """Return the bytes currently held at ``offset``."""

    def expected_checksum(self, offset: int, length: int) -> int:
        """Return the checksum recorded for ``offset`` at region commit time."""

    def ecc_counters(self) -> EccCounters:
        """Return the current ECC counters."""


class NullMemorySource:
    """Stand-in source used when the platform region is not mapped.

    Reports a stable region and zeroed counters so the service can start on a
    development host without the protected region present.
    """

    def __init__(self, region_bytes: int) -> None:
        self._region_bytes = region_bytes
        self._filler = bytes(PAGE_BYTES)

    def read_page(self, offset: int, length: int) -> bytes:
        del offset
        return self._filler[:length]

    def expected_checksum(self, offset: int, length: int) -> int:
        del offset
        return zlib.crc32(self._filler[:length])

    def ecc_counters(self) -> EccCounters:
        return EccCounters(correctable=0, uncorrectable=0)


class MemoryIntegrityCheck(PeriodicCheck):
    """Sliced checksum sweep plus ECC counter surveillance."""

    check_id = "ram"

    def __init__(
        self,
        config: MemoryCheckConfig,
        bus: MessageBus,
        source: Optional[MemorySource] = None,
        clock: Callable[[], float] = time.monotonic,
        budget_clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        # Cadence only. The sweep is self-paced and has no external event to
        # wait for, so no expiry window is configured on this timer.
        timer = IntervalTimer(
            period_s=config.scan_period_s,
            clock=clock,
            name="ram-sweep",
            start_due=True,
        )
        super().__init__(bus, timer)

        self._config = config
        self._source = source or NullMemorySource(config.region_bytes)
        self._budget_clock = budget_clock

        self._cursor = 0
        self._pass_index = 0
        self._mismatches_this_pass = 0
        self._pages_this_pass = 0
        self._baseline: Optional[EccCounters] = None

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def pass_index(self) -> int:
        return self._pass_index

    def on_tick(self, tick: TimerTick) -> List[CheckResult]:
        if not tick.due:
            return []

        if tick.overdue_by > self._config.scan_period_s:
            LOGGER.warning(
                "memory sweep cadence slipped",
                extra={
                    "check": self.check_id,
                    "overdue_by_s": round(tick.overdue_by, 3),
                    "missed_slots": tick.missed_slots,
                },
            )

        completed = self._advance_slice()
        self._timer.mark_serviced(tick.now)

        if not completed:
            return []
        return [self._summarise_pass(tick.now)]

    def _advance_slice(self) -> bool:
        """Scan pages until the slice budget or the region end is reached."""
        started = self._budget_clock()
        budget_s = self._config.slice_budget_ms / 1000.0
        pages_scanned = 0

        if self._baseline is None:
            self._baseline = self._source.ecc_counters()

        while pages_scanned < self._config.pages_per_slice:
            if self._cursor >= self._config.region_bytes:
                break
            if pages_scanned and (self._budget_clock() - started) >= budget_s:
                break

            length = min(PAGE_BYTES, self._config.region_bytes - self._cursor)
            observed = zlib.crc32(self._source.read_page(self._cursor, length))
            expected = self._source.expected_checksum(self._cursor, length)
            if observed != expected:
                self._mismatches_this_pass += 1
                LOGGER.error(
                    "memory checksum mismatch",
                    extra={
                        "check": self.check_id,
                        "offset": self._cursor,
                        "length": length,
                        "expected_crc": expected,
                        "observed_crc": observed,
                    },
                )

            self._cursor += length
            pages_scanned += 1
            self._pages_this_pass += 1

        return self._cursor >= self._config.region_bytes

    def _summarise_pass(self, now: float) -> CheckResult:
        counters = self._source.ecc_counters()
        baseline = self._baseline or counters
        correctable = max(0, counters.correctable - baseline.correctable)
        uncorrectable = max(0, counters.uncorrectable - baseline.uncorrectable)

        if uncorrectable >= self._config.uncorrectable_fault_threshold:
            state = HealthState.FAULT
            detail = f"{uncorrectable} uncorrectable ECC event(s) during pass"
        elif self._mismatches_this_pass:
            state = HealthState.FAULT
            detail = f"{self._mismatches_this_pass} checksum mismatch(es) during pass"
        elif correctable >= self._config.correctable_warn_threshold:
            state = HealthState.DEGRADED
            detail = f"{correctable} correctable ECC event(s) during pass"
        else:
            state = HealthState.OK
            detail = "region verified"

        result = self.result(
            state,
            detail,
            monotonic_ts=now,
            pass_index=self._pass_index,
            pages_scanned=self._pages_this_pass,
            region_bytes=self._config.region_bytes,
            checksum_mismatches=self._mismatches_this_pass,
            ecc_correctable=correctable,
            ecc_uncorrectable=uncorrectable,
        )

        self._pass_index += 1
        self._cursor = 0
        self._mismatches_this_pass = 0
        self._pages_this_pass = 0
        self._baseline = counters
        return result
