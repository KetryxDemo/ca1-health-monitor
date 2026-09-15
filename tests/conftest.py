"""Shared fixtures. Every test drives a fake clock so nothing sleeps."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from health_monitor.bus import Message, MessageBus  # noqa: E402
from health_monitor.config import (  # noqa: E402
    HeartbeatCheckConfig,
    MemoryCheckConfig,
    StorageCheckConfig,
)


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class RecordingBus(MessageBus):
    """Message bus that keeps every published message for assertions."""

    def __init__(self) -> None:
        super().__init__(name="test-bus")
        self.sent: List[Message] = []

    def publish(self, topic, payload, source="", monotonic_ts=0.0):  # type: ignore[override]
        self.sent.append(
            Message(topic=topic, payload=payload, source=source, monotonic_ts=monotonic_ts)
        )
        super().publish(topic, payload, source=source, monotonic_ts=monotonic_ts)

    def topics(self) -> List[str]:
        return [m.topic for m in self.sent]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def memory_config() -> MemoryCheckConfig:
    return MemoryCheckConfig(
        scan_period_s=5.0,
        region_bytes=4096 * 4,
        pages_per_slice=2,
        slice_budget_ms=1000.0,
    )


@pytest.fixture
def storage_config(tmp_path) -> StorageCheckConfig:
    return StorageCheckConfig(
        scan_period_s=10.0,
        mounts=[str(tmp_path)],
        stall_threshold_s=60.0,
    )


@pytest.fixture
def heartbeat_config() -> HeartbeatCheckConfig:
    return HeartbeatCheckConfig(
        emit_period_s=1.0,
        peer_timeout_s=3.0,
        peers=["generator-control", "mapping-engine", "catheter-io"],
        quorum_min_live=3,
    )
