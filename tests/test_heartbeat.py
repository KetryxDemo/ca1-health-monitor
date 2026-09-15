"""Unit tests for FN-HM-HB."""

from __future__ import annotations

import pytest

from health_monitor.bus import Message
from health_monitor.checks.heartbeat import HeartbeatCheck
from health_monitor.common.types import HealthState


def _beat(check, peer, sequence):
    check.on_peer_heartbeat(
        Message(topic=f"peer.heartbeat.{peer}", payload={"sequence": sequence})
    )


def _beat_all(check, sequence):
    for peer in check.peers:
        _beat(check, peer, sequence)


def test_liveness_is_tracked_independently_per_peer(heartbeat_config, bus, clock):
    """Every configured peer carries its own liveness state and silence budget."""
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    assert check.timer.expiry_enabled is False
    for record in check.peers.values():
        assert record.timer.expiry_enabled is True
        assert record.timer.expiry_s == pytest.approx(heartbeat_config.peer_timeout_s)


def test_emits_own_heartbeat_on_cadence(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    check.service()
    assert bus.topics().count("peer.heartbeat.health-monitor") == 1

    check.service()
    assert bus.topics().count("peer.heartbeat.health-monitor") == 1

    clock.advance(heartbeat_config.emit_period_s)
    check.service()
    assert bus.topics().count("peer.heartbeat.health-monitor") == 2


def test_emitted_sequence_increments(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    check.service()
    clock.advance(heartbeat_config.emit_period_s)
    check.service()

    emitted = [m for m in bus.sent if m.topic == "peer.heartbeat.health-monitor"]
    assert [m.payload["sequence"] for m in emitted] == [1, 2]


def test_peer_stays_live_while_beating(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    for sequence in range(1, 10):
        _beat_all(check, sequence)
        clock.advance(heartbeat_config.emit_period_s)
        results = check.service()
        assert all(r.state is not HealthState.FAULT for r in results)
    assert all(record.live for record in check.peers.values())


def test_peer_is_declared_lost_once_past_the_timeout(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    _beat_all(check, 1)
    check.service()

    clock.advance(heartbeat_config.peer_timeout_s)
    assert [r for r in check.service() if r.state is HealthState.FAULT] == []

    clock.advance(0.01)
    faults = [r for r in check.service() if r.state is HealthState.FAULT]
    lost = {r.subject for r in faults}
    assert "generator-control" in lost


def test_only_the_silent_peer_is_declared_lost(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    _beat_all(check, 1)
    check.service()

    for step in range(2, 8):
        clock.advance(heartbeat_config.emit_period_s)
        _beat(check, "mapping-engine", step)
        _beat(check, "catheter-io", step)
        check.service()

    assert check.peers["generator-control"].live is False
    assert check.peers["mapping-engine"].live is True
    assert check.peers["catheter-io"].live is True


def test_recovery_is_reported_once(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    _beat_all(check, 1)
    check.service()

    clock.advance(heartbeat_config.peer_timeout_s + 0.5)
    check.service()
    assert check.peers["catheter-io"].live is False

    _beat_all(check, 2)
    recoveries = [
        r for r in check.service()
        if r.state is HealthState.OK and r.subject == "catheter-io"
    ]
    assert len(recoveries) == 1
    assert check.peers["catheter-io"].live is True

    clock.advance(0.1)
    assert [r for r in check.service() if r.subject == "catheter-io"] == []


def test_transitions_only_no_repeat_publishing(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    _beat_all(check, 1)
    check.service()

    clock.advance(heartbeat_config.peer_timeout_s + 0.5)
    first = [r for r in check.service() if r.state is HealthState.FAULT]
    assert len(first) == len(check.peers) + 1  # every peer plus quorum

    clock.advance(1.0)
    assert check.service() == []


def test_replayed_heartbeat_is_discarded(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    _beat(check, "mapping-engine", 5)
    refreshes = check.peers["mapping-engine"].refreshes

    _beat(check, "mapping-engine", 5)
    _beat(check, "mapping-engine", 3)
    assert check.peers["mapping-engine"].refreshes == refreshes


def test_heartbeat_from_unknown_peer_is_ignored(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    _beat(check, "not-a-configured-peer", 1)
    assert "not-a-configured-peer" not in check.peers


def test_quorum_fault_when_too_few_peers_live(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    _beat_all(check, 1)
    check.service()

    collected = []
    for step in range(2, 8):
        clock.advance(heartbeat_config.emit_period_s)
        _beat(check, "mapping-engine", step)
        _beat(check, "catheter-io", step)
        collected.extend(check.service())

    quorum = [r for r in collected if r.subject == "quorum"]
    assert quorum and quorum[0].state is HealthState.FAULT
    assert quorum[0].metrics["live_peers"] == 2


def test_attach_subscribes_to_the_peer_topic(heartbeat_config, bus, clock):
    check = HeartbeatCheck(heartbeat_config, bus, clock=clock)
    check.attach(bus)

    bus.publish("peer.heartbeat.catheter-io", {"sequence": 11})
    bus.drain()
    assert check.peers["catheter-io"].sequence == 11
