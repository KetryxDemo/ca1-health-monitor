"""Message bus tests."""

from __future__ import annotations

from health_monitor.bus import Message, MessageBus


def test_exact_topic_delivery():
    bus = MessageBus()
    seen = []
    bus.subscribe("health.ram", seen.append)

    bus.publish("health.ram", {"state": "ok"})
    bus.publish("health.disk", {"state": "ok"})
    bus.drain()

    assert [m.topic for m in seen] == ["health.ram"]


def test_trailing_wildcard_consumes_one_segment():
    bus = MessageBus()
    seen = []
    bus.subscribe("peer.heartbeat.*", seen.append)

    bus.publish("peer.heartbeat.catheter-io", {})
    bus.publish("peer.heartbeat.catheter-io.extra", {})
    bus.publish("peer.state.catheter-io", {})
    bus.drain()

    assert [m.topic for m in seen] == ["peer.heartbeat.catheter-io"]


def test_overflow_is_counted_not_raised():
    bus = MessageBus(max_depth=2)
    for index in range(5):
        bus.publish("health.ram", {"index": index})
    assert bus.dropped == 3
    assert bus.published == 2


def test_failing_subscriber_does_not_block_others():
    bus = MessageBus()
    seen = []

    def explode(message: Message) -> None:
        raise RuntimeError("subscriber fault")

    bus.subscribe("health.ram", explode)
    bus.subscribe("health.ram", seen.append)

    bus.publish("health.ram", {})
    bus.drain()
    assert len(seen) == 1


def test_drain_respects_the_limit():
    bus = MessageBus()
    bus.subscribe("health.ram", lambda m: None)
    for _ in range(10):
        bus.publish("health.ram", {})
    assert bus.drain(limit=4) == 4
    assert bus.drain() == 6
