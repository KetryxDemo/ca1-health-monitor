"""Service entrypoint and control loop for the health-monitor.

The service runs a single-threaded control loop. Each iteration drains the
message bus, services every enabled check, and then sleeps until the nearest
check is next due, bounded by ``loop_period_s``. Checks are cooperative: none
of them may block the loop for longer than its own slice budget.

Exit codes
----------
0  clean shutdown after SIGTERM or SIGINT
2  configuration error
3  a check raised during construction
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from types import FrameType
from typing import Any, Dict, List, Optional, Sequence

from health_monitor import __version__
from health_monitor.bus import MessageBus
from health_monitor.checks.base import PeriodicCheck
from health_monitor.checks.heartbeat import HeartbeatCheck
from health_monitor.checks.memory_integrity import MemoryIntegrityCheck
from health_monitor.checks.storage_health import StorageHealthCheck
from health_monitor.common.types import HealthState
from health_monitor.config import ConfigError, ServiceConfig, describe, load
from health_monitor.logging_setup import configure, log_fields

LOGGER = logging.getLogger("health-monitor")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CHECK_INIT = 3

STATUS_TOPIC = "health.monitor.status"


def build_checks(config: ServiceConfig, bus: MessageBus) -> List[PeriodicCheck]:
    """Construct every enabled check and wire its bus subscriptions."""
    checks: List[PeriodicCheck] = []

    if config.memory.enabled:
        checks.append(MemoryIntegrityCheck(config.memory, bus))
    if config.storage.enabled:
        checks.append(StorageHealthCheck(config.storage, bus))
    if config.heartbeat.enabled:
        checks.append(HeartbeatCheck(config.heartbeat, bus, config.instance_id))

    for check in checks:
        check.attach(bus)
    return checks


class HealthMonitorService:
    """Owns the bus, the checks and the control loop."""

    def __init__(
        self,
        config: ServiceConfig,
        bus: Optional[MessageBus] = None,
        checks: Optional[Sequence[PeriodicCheck]] = None,
    ) -> None:
        self._config = config
        self._bus = bus or MessageBus(name=config.service_name)
        self._checks = list(checks) if checks is not None else build_checks(config, self._bus)
        self._running = False
        self._iterations = 0
        self._started_at: Optional[float] = None

    @property
    def bus(self) -> MessageBus:
        return self._bus

    @property
    def checks(self) -> List[PeriodicCheck]:
        return self._checks

    @property
    def iterations(self) -> int:
        return self._iterations

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, frame: Optional[FrameType]) -> None:
            del frame
            LOGGER.info(
                "shutdown signal received",
                extra=log_fields(signal=signal.Signals(signum).name),
            )
            self.request_stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _handler)

    def request_stop(self) -> None:
        self._running = False

    def step(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Run one control-loop iteration. Returns a small telemetry dict."""
        self._bus.drain()
        emitted = 0
        worst = HealthState.OK
        for check in self._checks:
            for result in check.service(now):
                emitted += 1
                worst = max(worst, result.state)
        self._iterations += 1
        return {"results": emitted, "worst_state": worst}

    def _sleep_budget(self) -> float:
        """Sleep until the nearest check is due, capped at the loop period."""
        soonest = self._config.loop_period_s
        for check in self._checks:
            soonest = min(soonest, check.timer.seconds_until_due())
        return max(0.0, min(soonest, self._config.loop_period_s))

    def run(self, max_iterations: Optional[int] = None) -> int:
        self._running = True
        self._started_at = time.monotonic()
        LOGGER.info(
            "health-monitor starting",
            extra=log_fields(version=__version__, checks=[c.check_id for c in self._checks]),
        )

        try:
            while self._running:
                telemetry = self.step()
                if telemetry["results"]:
                    LOGGER.debug(
                        "control loop iteration",
                        extra=log_fields(
                            iteration=self._iterations,
                            results=telemetry["results"],
                            worst_state=telemetry["worst_state"].label,
                        ),
                    )
                if max_iterations is not None and self._iterations >= max_iterations:
                    break
                budget = self._sleep_budget()
                if budget > 0:
                    time.sleep(budget)
        finally:
            self._shutdown()
        return EXIT_OK

    def _shutdown(self) -> None:
        self._running = False
        uptime = 0.0 if self._started_at is None else time.monotonic() - self._started_at
        self.publish_status(uptime)
        self._bus.drain()
        self._bus.stop(timeout_s=self._config.shutdown_grace_s)
        LOGGER.info(
            "health-monitor stopped",
            extra=log_fields(uptime_s=round(uptime, 3), iterations=self._iterations),
        )

    def publish_status(self, uptime_s: float) -> None:
        self._bus.publish(
            STATUS_TOPIC,
            {
                "version": __version__,
                "instance": self._config.instance_id,
                "uptime_s": round(uptime_s, 3),
                "iterations": self._iterations,
                "checks": {c.check_id: c.stats.as_payload() for c in self._checks},
                "bus": {
                    "published": self._bus.published,
                    "delivered": self._bus.delivered,
                    "dropped": self._bus.dropped,
                },
            },
            source=self._config.service_name,
            monotonic_ts=time.monotonic(),
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="health-monitor")
    parser.add_argument(
        "--config",
        default="/etc/platform/health-monitor.json",
        help="path to the service configuration JSON",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="stop after this many control-loop iterations (bench use)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"health-monitor: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure(
        level=config.logging.level,
        fmt=config.logging.format,
        service_name=config.service_name,
        instance_id=config.instance_id,
        include_monotonic=config.logging.include_monotonic,
    )
    LOGGER.info("configuration loaded", extra=describe(config))

    try:
        service = HealthMonitorService(config)
    except Exception:  # noqa: BLE001 - report and exit with a distinct code
        LOGGER.exception("failed to construct checks")
        return EXIT_CHECK_INIT

    service.install_signal_handlers()
    service.bus.start()
    return service.run(max_iterations=args.max_iterations)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
