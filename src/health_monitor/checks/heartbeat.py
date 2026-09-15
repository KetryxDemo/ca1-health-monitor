"""FN-HM-HB. Inter-process liveness over the platform message bus.

This check has two halves.

Emission: once per ``emit_period_s`` the service publishes its own heartbeat so
that peers can observe it. That is a pure cadence obligation.

Observation: every configured peer publishes its heartbeat on
``peer.heartbeat.<peer>``. Each arriving message refreshes that peer's timer.
The peer is considered live for as long as its timer has not reported expiry;
the instant the timer reports expiry, the peer is declared lost and a fault is
published for it. There is no separate bookkeeping of peer arrival times in
this module: the expiry window carried by each peer timer is the sole
definition of how long a peer may stay silent.

When fewer than ``quorum_min_live`` peers are live, a quorum fault is published
in addition to the per-peer results, because the supervisor uses quorum loss to
drive the platform into its safe state.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from health_monitor.bus import Message, MessageBus
from health_monitor.checks.base import PeriodicCheck
from health_monitor.common.interval_timer import IntervalTimer, TimerTick
from health_monitor.common.types import CheckResult, HealthState
from health_monitor.config import HeartbeatCheckConfig

LOGGER = logging.getLogger(__name__)

PEER_TOPIC_PREFIX = "peer.heartbeat"
QUORUM_SUBJECT = "quorum"


@dataclass
class PeerRecord:
    """Per-peer liveness state.

    ``timer`` is constructed with an expiry window, so its tick carries a
    meaningful ``expired`` flag. That flag is what declares the peer lost.
    """

    name: str
    timer: IntervalTimer
    live: bool = True
    sequence: int = -1
    losses: int = 0
    refreshes: int = 0


class HeartbeatCheck(PeriodicCheck):
    """Emits this service's heartbeat and tracks peer liveness."""

    check_id = "heartbeat"

    def __init__(
        self,
        config: HeartbeatCheckConfig,
        bus: MessageBus,
        instance_id: str = "hm-0",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Emission cadence. This timer schedules our own outgoing heartbeat and
        # waits on nothing, so it carries no expiry window.
        emit_timer = IntervalTimer(
            period_s=config.emit_period_s,
            clock=clock,
            name="heartbeat-emit",
            start_due=True,
        )
        super().__init__(bus, emit_timer)

        self._config = config
        self._instance_id = instance_id
        self._clock = clock
        self._sequence = 0

        # Observation. Each peer timer is constructed with an expiry window;
        # an arriving heartbeat refreshes it and expiry declares the peer lost.
        self._peers: Dict[str, PeerRecord] = {
            peer: PeerRecord(
                name=peer,
                timer=IntervalTimer(
                    period_s=config.emit_period_s,
                    expiry_s=config.peer_timeout_s,
                    clock=clock,
                    name=f"peer-{peer}",
                ),
            )
            for peer in config.peers
        }

    @property
    def peers(self) -> Dict[str, PeerRecord]:
        return self._peers

    def attach(self, bus: MessageBus) -> None:
        bus.subscribe(f"{PEER_TOPIC_PREFIX}.*", self.on_peer_heartbeat)

    def on_peer_heartbeat(self, message: Message) -> None:
        """Refresh the expiry window for the peer that sent this message."""
        peer_name = message.topic.rsplit(".", 1)[-1]
        record = self._peers.get(peer_name)
        if record is None:
            LOGGER.debug(
                "heartbeat from unconfigured peer ignored",
                extra={"check": self.check_id, "peer": peer_name},
            )
            return

        sequence = int(message.payload.get("sequence", -1))
        if sequence >= 0 and sequence <= record.sequence:
            LOGGER.warning(
                "stale or replayed heartbeat discarded",
                extra={
                    "check": self.check_id,
                    "peer": peer_name,
                    "received_sequence": sequence,
                    "last_sequence": record.sequence,
                },
            )
            return

        record.sequence = sequence
        record.refreshes += 1
        record.timer.refresh()

    def on_tick(self, tick: TimerTick) -> List[CheckResult]:
        results: List[CheckResult] = []

        if tick.due:
            self._emit(tick.now)
            self._timer.mark_serviced(tick.now)

        for record in self._peers.values():
            peer_tick = record.timer.poll(tick.now)
            result = self._evaluate_peer(record, peer_tick)
            if result is not None:
                results.append(result)

        quorum = self._evaluate_quorum(tick.now)
        if quorum is not None:
            results.append(quorum)

        return results

    def _emit(self, now: float) -> None:
        self._sequence += 1
        self._bus.publish(
            f"{PEER_TOPIC_PREFIX}.{self._config_self_name()}",
            {
                "instance": self._instance_id,
                "sequence": self._sequence,
                "emit_period_s": self._config.emit_period_s,
            },
            source=self.check_id,
            monotonic_ts=now,
        )

    def _config_self_name(self) -> str:
        return "health-monitor"

    def _evaluate_peer(
        self, record: PeerRecord, peer_tick: TimerTick
    ) -> Optional[CheckResult]:
        """Translate a peer timer tick into a liveness result.

        A result is emitted only on a transition, so a stable platform does not
        publish one message per peer per control-loop iteration.
        """
        was_live = record.live
        record.live = not peer_tick.expired

        if record.live == was_live:
            return None

        silent_for = peer_tick.time_since_refresh or 0.0
        if record.live:
            return self.result(
                HealthState.OK,
                "peer heartbeat restored",
                subject=record.name,
                monotonic_ts=peer_tick.now,
                silent_for_s=round(silent_for, 3),
                losses=record.losses,
                refreshes=record.refreshes,
            )

        record.losses += 1
        LOGGER.error(
            "peer declared lost",
            extra={
                "check": self.check_id,
                "peer": record.name,
                "silent_for_s": round(silent_for, 3),
                "peer_timeout_s": self._config.peer_timeout_s,
            },
        )
        return self.result(
            HealthState.FAULT,
            f"no heartbeat for {silent_for:.2f} s",
            subject=record.name,
            monotonic_ts=peer_tick.now,
            silent_for_s=round(silent_for, 3),
            losses=record.losses,
            refreshes=record.refreshes,
        )

    def _evaluate_quorum(self, now: float) -> Optional[CheckResult]:
        live = sum(1 for record in self._peers.values() if record.live)
        required = self._config.quorum_min_live
        state = HealthState.OK if live >= required else HealthState.FAULT

        previous = self._last_published.get(QUORUM_SUBJECT)
        if previous is state:
            return None

        detail = (
            "peer quorum satisfied"
            if state is HealthState.OK
            else f"only {live} of {required} required peers live"
        )
        return self.result(
            state,
            detail,
            subject=QUORUM_SUBJECT,
            monotonic_ts=now,
            live_peers=live,
            required_peers=required,
            configured_peers=len(self._peers),
        )
