"""Unit tests for FN-HM-DISK."""

from __future__ import annotations

import pytest

from health_monitor.checks.storage_health import MountProbe, StorageHealthCheck
from health_monitor.common.types import HealthState


class StubProbe:
    """Probe that returns whatever the test queues up."""

    def __init__(self, outcome: MountProbe) -> None:
        self.outcome = outcome
        self.calls = 0

    def probe(self, mount: str, write_bytes: int) -> MountProbe:
        self.calls += 1
        return MountProbe(
            mount=mount,
            ok=self.outcome.ok,
            free_fraction=self.outcome.free_fraction,
            total_bytes=self.outcome.total_bytes,
            write_latency_ms=self.outcome.write_latency_ms,
            error=self.outcome.error,
        )


def _healthy(mount="/m"):
    return MountProbe(mount=mount, ok=True, free_fraction=0.6, total_bytes=10**9,
                      write_latency_ms=2.0)


def _make(config, bus, clock, outcome):
    probe = StubProbe(outcome)
    return StorageHealthCheck(config, bus, probe=probe, clock=clock), probe


def test_scan_is_scheduled_from_its_own_cadence(storage_config, bus, clock):
    """Each mount is probed on the configured scan period and nothing else."""
    check, _ = _make(storage_config, bus, clock, _healthy())
    assert check.timer.expiry_enabled is False
    assert check.timer.expiry_s is None


def test_healthy_mount_reports_ok(storage_config, bus, clock):
    check, probe = _make(storage_config, bus, clock, _healthy())
    results = check.service()
    assert probe.calls == 1
    assert len(results) == 1
    assert results[0].state is HealthState.OK


def test_cadence_gates_the_probe(storage_config, bus, clock):
    check, probe = _make(storage_config, bus, clock, _healthy())
    check.service()
    check.service()
    assert probe.calls == 1

    clock.advance(storage_config.scan_period_s)
    check.service()
    assert probe.calls == 2


def test_low_free_space_is_degraded_then_fault(storage_config, bus, clock):
    check, probe = _make(
        storage_config, bus, clock,
        MountProbe(mount="/m", ok=True, free_fraction=0.15, total_bytes=10**9),
    )
    assert check.service()[0].state is HealthState.DEGRADED

    probe.outcome = MountProbe(mount="/m", ok=True, free_fraction=0.02, total_bytes=10**9)
    clock.advance(storage_config.scan_period_s)
    assert check.service()[0].state is HealthState.FAULT


def test_slow_write_probe_is_degraded_then_fault(storage_config, bus, clock):
    check, probe = _make(
        storage_config, bus, clock,
        MountProbe(mount="/m", ok=True, free_fraction=0.6, total_bytes=10**9,
                   write_latency_ms=80.0),
    )
    assert check.service()[0].state is HealthState.DEGRADED

    probe.outcome = MountProbe(mount="/m", ok=True, free_fraction=0.6,
                               total_bytes=10**9, write_latency_ms=400.0)
    clock.advance(storage_config.scan_period_s)
    assert check.service()[0].state is HealthState.FAULT


def test_first_probe_failure_is_degraded_second_is_fault(storage_config, bus, clock):
    check, _ = _make(
        storage_config, bus, clock,
        MountProbe(mount="/m", ok=False, error="write probe: no space left on device"),
    )
    assert check.service()[0].state is HealthState.DEGRADED

    clock.advance(storage_config.scan_period_s)
    assert check.service()[0].state is HealthState.FAULT


def test_outage_duration_is_reported_in_the_metrics(storage_config, bus, clock):
    """A mount that stops answering accumulates a reported outage duration."""
    check, probe = _make(storage_config, bus, clock, _healthy())
    first = check.service()[0]
    assert first.metrics["stale_for_s"] == 0.0

    probe.outcome = MountProbe(mount="/m", ok=False, error="statvfs: Input/output error")
    clock.advance(storage_config.scan_period_s)
    second = check.service()[0]
    assert second.metrics["stale_for_s"] == pytest.approx(storage_config.scan_period_s)


def test_prolonged_outage_escalates_to_fault(storage_config, bus, clock):
    """A mount unusable for long enough is a fault, not a transient."""
    check, probe = _make(storage_config, bus, clock, _healthy())
    check.service()

    probe.outcome = MountProbe(mount="/m", ok=False, error="statvfs: Input/output error")
    clock.advance(storage_config.stall_threshold_s + 1.0)
    result = check.service()[0]
    assert result.state is HealthState.FAULT
    assert result.metrics["stale_for_s"] > storage_config.stall_threshold_s


def test_recovery_clears_failure_counters(storage_config, bus, clock):
    check, probe = _make(
        storage_config, bus, clock,
        MountProbe(mount="/m", ok=False, error="write probe: filesystem is read-only"),
    )
    check.service()

    probe.outcome = _healthy()
    clock.advance(storage_config.scan_period_s)
    result = check.service()[0]
    assert result.state is HealthState.OK
    assert result.metrics["consecutive_failures"] == 0


def test_every_mount_produces_its_own_subject(storage_config, bus, clock):
    storage_config.mounts = ["/var/log", "/var/lib/cases"]
    check, _ = _make(storage_config, bus, clock, _healthy())
    results = check.service()
    assert {r.subject for r in results} == {"var-log", "var-lib-cases"}
