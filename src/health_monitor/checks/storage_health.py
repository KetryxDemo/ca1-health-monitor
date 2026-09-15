"""FN-HM-DISK. Capacity, writability and latency surveillance of local storage.

Each cadence slot probes every configured mount point: free-space fraction from
the filesystem, then a small write-and-fsync round trip that is timed to detect
a device that is still mounted but no longer servicing IO.

Two failure shapes are distinguished. A probe that returns a bad number is a
device problem and is reported immediately. A probe that does not return at all
leaves the mount without a fresh observation; that condition is tracked here
against the last successful probe timestamp for the mount, because probes run
on a worker with its own deadline and can be abandoned independently of the
service control loop.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol

from health_monitor.bus import MessageBus
from health_monitor.checks.base import PeriodicCheck
from health_monitor.common.interval_timer import IntervalTimer, TimerTick
from health_monitor.common.types import CheckResult, HealthState
from health_monitor.config import StorageCheckConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MountProbe:
    """Outcome of one probe against one mount point."""

    mount: str
    ok: bool
    free_fraction: float = 0.0
    total_bytes: int = 0
    write_latency_ms: float = 0.0
    error: str = ""


class StorageProbe(Protocol):
    def probe(self, mount: str, write_bytes: int) -> MountProbe:
        """Probe ``mount`` and return the observation."""


class FilesystemProbe:
    """Default probe. Uses statvfs plus a write-and-fsync round trip."""

    def __init__(self, latency_clock: Callable[[], float] = time.perf_counter) -> None:
        self._latency_clock = latency_clock

    def probe(self, mount: str, write_bytes: int) -> MountProbe:
        try:
            usage = shutil.disk_usage(mount)
        except OSError as exc:
            return MountProbe(mount=mount, ok=False, error=f"statvfs: {exc.strerror}")

        total = usage.total or 1
        free_fraction = usage.free / total

        payload = b"\x5a" * max(1, write_bytes)
        started = self._latency_clock()
        handle = None
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix=".hm-probe-", dir=mount)
            handle = os.fdopen(fd, "wb")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            detail = os.strerror(exc.errno) if exc.errno else str(exc)
            if exc.errno == errno.EROFS:
                detail = "filesystem is read-only"
            elif exc.errno == errno.ENOSPC:
                detail = "no space left on device"
            return MountProbe(
                mount=mount,
                ok=False,
                free_fraction=free_fraction,
                total_bytes=usage.total,
                error=f"write probe: {detail}",
            )
        finally:
            if handle is not None:
                handle.close()
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    LOGGER.warning(
                        "could not remove storage probe file", extra={"path": path}
                    )

        latency_ms = (self._latency_clock() - started) * 1000.0
        return MountProbe(
            mount=mount,
            ok=True,
            free_fraction=free_fraction,
            total_bytes=usage.total,
            write_latency_ms=latency_ms,
        )


class StorageHealthCheck(PeriodicCheck):
    """Per-mount capacity, writability and latency surveillance."""

    check_id = "disk"

    def __init__(
        self,
        config: StorageCheckConfig,
        bus: MessageBus,
        probe: Optional[StorageProbe] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Cadence only. Nothing external drives this check, so the timer is
        # constructed without an expiry window; staleness of an individual
        # mount is tracked per mount below, against its own last good probe.
        timer = IntervalTimer(
            period_s=config.scan_period_s,
            clock=clock,
            name="disk-scan",
            start_due=True,
        )
        super().__init__(bus, timer)

        self._config = config
        self._probe = probe or FilesystemProbe()
        self._clock = clock
        self._last_good_probe: Dict[str, float] = {}
        self._consecutive_failures: Dict[str, int] = {m: 0 for m in config.mounts}

    def on_tick(self, tick: TimerTick) -> List[CheckResult]:
        if not tick.due:
            return []

        results: List[CheckResult] = []
        for mount in self._config.mounts:
            observation = self._probe.probe(mount, self._config.probe_write_bytes)
            if observation.ok:
                self._last_good_probe[mount] = tick.now
                self._consecutive_failures[mount] = 0
            else:
                self._consecutive_failures[mount] = (
                    self._consecutive_failures.get(mount, 0) + 1
                )
            results.append(self._evaluate(mount, observation, tick.now))

        self._timer.mark_serviced(tick.now)
        return results

    def _stale_for(self, mount: str, now: float) -> float:
        """Seconds since this mount last returned a usable observation."""
        last_good = self._last_good_probe.get(mount)
        if last_good is None:
            return 0.0
        return max(0.0, now - last_good)

    def _evaluate(self, mount: str, observation: MountProbe, now: float) -> CheckResult:
        stale_for = self._stale_for(mount, now)
        failures = self._consecutive_failures.get(mount, 0)

        metrics = {
            "free_fraction": round(observation.free_fraction, 4),
            "total_bytes": observation.total_bytes,
            "write_latency_ms": round(observation.write_latency_ms, 3),
            "consecutive_failures": failures,
            "stale_for_s": round(stale_for, 3),
        }

        if not observation.ok:
            # A mount that has been unusable for longer than the stall
            # threshold is a fault regardless of the reported error; before
            # that it is degraded, because a single failed probe can be a
            # transient during log rotation.
            state = (
                HealthState.FAULT
                if stale_for > self._config.stall_threshold_s or failures > 1
                else HealthState.DEGRADED
            )
            return self.result(
                state, observation.error, subject=_slug(mount), monotonic_ts=now, **metrics
            )

        if observation.free_fraction < self._config.free_fault_fraction:
            return self.result(
                HealthState.FAULT,
                f"free space {observation.free_fraction:.1%} below fault floor",
                subject=_slug(mount),
                monotonic_ts=now,
                **metrics,
            )
        if observation.write_latency_ms > self._config.probe_latency_fault_ms:
            return self.result(
                HealthState.FAULT,
                f"write probe took {observation.write_latency_ms:.1f} ms",
                subject=_slug(mount),
                monotonic_ts=now,
                **metrics,
            )
        if observation.free_fraction < self._config.free_warn_fraction:
            return self.result(
                HealthState.DEGRADED,
                f"free space {observation.free_fraction:.1%} below warning floor",
                subject=_slug(mount),
                monotonic_ts=now,
                **metrics,
            )
        if observation.write_latency_ms > self._config.probe_latency_warn_ms:
            return self.result(
                HealthState.DEGRADED,
                f"write probe took {observation.write_latency_ms:.1f} ms",
                subject=_slug(mount),
                monotonic_ts=now,
                **metrics,
            )

        return self.result(
            HealthState.OK,
            "mount healthy",
            subject=_slug(mount),
            monotonic_ts=now,
            **metrics,
        )


def _slug(mount: str) -> str:
    return mount.strip("/").replace("/", "-") or "root"
