"""Unit tests for FN-HM-RAM."""

from __future__ import annotations

import zlib

import pytest

from health_monitor.checks.memory_integrity import (
    EccCounters,
    MemoryIntegrityCheck,
    PAGE_BYTES,
)
from health_monitor.common.types import HealthState


class StubMemorySource:
    """Memory source whose contents and ECC counters the test controls."""

    def __init__(self, region_bytes: int) -> None:
        self.pages = {
            offset: bytes([offset // PAGE_BYTES % 251]) * PAGE_BYTES
            for offset in range(0, region_bytes, PAGE_BYTES)
        }
        self.expected = {o: zlib.crc32(p) for o, p in self.pages.items()}
        self.counters = EccCounters(correctable=0, uncorrectable=0)

    def read_page(self, offset: int, length: int) -> bytes:
        return self.pages[offset][:length]

    def expected_checksum(self, offset: int, length: int) -> int:
        return self.expected[offset]

    def ecc_counters(self) -> EccCounters:
        return self.counters

    def corrupt(self, offset: int) -> None:
        self.pages[offset] = b"\xff" * PAGE_BYTES


def _run_full_pass(check, clock, period_s):
    """Service the check until it publishes a pass summary."""
    for _ in range(64):
        results = check.service()
        if results:
            return results
        clock.advance(period_s)
    raise AssertionError("pass never completed")


def _start_pass(check, clock, period_s):
    """Service one slice so the ECC baseline is captured, then step the clock."""
    assert check.service() == []
    clock.advance(period_s)


def test_sweep_is_scheduled_from_its_own_cadence(memory_config, bus, clock):
    """The sweep advances on its configured scan period and nothing else."""
    check = MemoryIntegrityCheck(memory_config, bus, clock=clock)
    assert check.timer.expiry_enabled is False
    assert check.timer.expiry_s is None


def test_does_nothing_before_the_cadence_slot(memory_config, bus, clock):
    check = MemoryIntegrityCheck(memory_config, bus, clock=clock)
    assert check.service() == []  # start_due consumes the first slot
    assert check.service() == []
    assert bus.topics() == []


def test_completes_a_pass_across_multiple_slots(memory_config, bus, clock):
    source = StubMemorySource(memory_config.region_bytes)
    check = MemoryIntegrityCheck(memory_config, bus, source=source, clock=clock)

    results = _run_full_pass(check, clock, memory_config.scan_period_s)
    assert len(results) == 1
    assert results[0].state is HealthState.OK
    assert results[0].metrics["pages_scanned"] == 4
    assert check.cursor == 0
    assert check.pass_index == 1


def test_checksum_mismatch_is_a_fault(memory_config, bus, clock):
    source = StubMemorySource(memory_config.region_bytes)
    source.corrupt(PAGE_BYTES)
    check = MemoryIntegrityCheck(memory_config, bus, source=source, clock=clock)

    results = _run_full_pass(check, clock, memory_config.scan_period_s)
    assert results[0].state is HealthState.FAULT
    assert results[0].metrics["checksum_mismatches"] == 1


def test_correctable_ecc_is_degraded(memory_config, bus, clock):
    source = StubMemorySource(memory_config.region_bytes)
    check = MemoryIntegrityCheck(memory_config, bus, source=source, clock=clock)
    _start_pass(check, clock, memory_config.scan_period_s)

    source.counters = EccCounters(correctable=3, uncorrectable=0)
    results = _run_full_pass(check, clock, memory_config.scan_period_s)
    assert results[0].state is HealthState.DEGRADED
    assert results[0].metrics["ecc_correctable"] == 3


def test_uncorrectable_ecc_is_a_fault(memory_config, bus, clock):
    source = StubMemorySource(memory_config.region_bytes)
    check = MemoryIntegrityCheck(memory_config, bus, source=source, clock=clock)
    _start_pass(check, clock, memory_config.scan_period_s)

    source.counters = EccCounters(correctable=0, uncorrectable=1)
    results = _run_full_pass(check, clock, memory_config.scan_period_s)
    assert results[0].state is HealthState.FAULT


def test_ecc_delta_resets_between_passes(memory_config, bus, clock):
    source = StubMemorySource(memory_config.region_bytes)
    check = MemoryIntegrityCheck(memory_config, bus, source=source, clock=clock)
    _start_pass(check, clock, memory_config.scan_period_s)

    source.counters = EccCounters(correctable=2, uncorrectable=0)
    first = _run_full_pass(check, clock, memory_config.scan_period_s)
    assert first[0].state is HealthState.DEGRADED

    clock.advance(memory_config.scan_period_s)
    second = _run_full_pass(check, clock, memory_config.scan_period_s)
    assert second[0].state is HealthState.OK
    assert second[0].metrics["ecc_correctable"] == 0


def test_result_is_published_on_the_bus(memory_config, bus, clock):
    source = StubMemorySource(memory_config.region_bytes)
    check = MemoryIntegrityCheck(memory_config, bus, source=source, clock=clock)

    _run_full_pass(check, clock, memory_config.scan_period_s)
    assert "health.ram" in bus.topics()


def test_pass_still_completes_after_scheduler_starvation(memory_config, bus, clock):
    """A badly delayed slot costs cadence, not correctness: the pass still reports."""
    source = StubMemorySource(memory_config.region_bytes)
    check = MemoryIntegrityCheck(memory_config, bus, source=source, clock=clock)

    for _ in range(8):
        clock.advance(3600.0)
        results = check.service()
        if results:
            assert results[0].state is HealthState.OK
            return
    raise AssertionError("pass never completed")
