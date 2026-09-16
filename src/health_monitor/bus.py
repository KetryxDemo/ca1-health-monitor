"""In-process shim over the platform message bus.

The production platform runs a broker on a unix domain socket; every service
links against the same client library. This module is the thin local face of
that client: publishers hand it a topic and a JSON-serialisable payload,
subscribers register a callback against a topic pattern. Delivery is
queue-backed so a slow subscriber cannot stall a publisher inside a control
loop.

Topic patterns support a single trailing wildcard segment, for example
``peer.heartbeat.*``. Exact topics are matched first.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

Handler = Callable[["Message"], None]

_MAX_QUEUE_DEPTH = 4096


@dataclass(frozen=True)
class Message:
    topic: str
    payload: Dict[str, Any]
    source: str = ""
    monotonic_ts: float = 0.0


class BusOverflow(RuntimeError):
    """Raised when the delivery queue is saturated and a message was dropped."""


def _matches(pattern: str, topic: str) -> bool:
    if pattern == topic:
        return True
    if not pattern.endswith(".*"):
        return False
    prefix = pattern[:-1]
    if not topic.startswith(prefix):
        return False
    # A trailing wildcard consumes exactly one segment.
    return "." not in topic[len(prefix):]


class MessageBus:
    """Queue-backed publish/subscribe fabric.

    Publishing is non-blocking. Delivery happens on :meth:`drain`, which the
    service loop calls once per iteration, or on a dedicated dispatch thread
    started by :meth:`start`.
    """

    def __init__(self, name: str = "bus", max_depth: int = _MAX_QUEUE_DEPTH) -> None:
        self._name = name
        self._queue: "queue.Queue[Message]" = queue.Queue(maxsize=max_depth)
        self._subscriptions: List[Tuple[str, Handler]] = []
        self._lock = threading.Lock()
        self._dropped = 0
        self._published = 0
        self._delivered = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def published(self) -> int:
        return self._published

    @property
    def delivered(self) -> int:
        return self._delivered

    def subscribe(self, pattern: str, handler: Handler) -> None:
        with self._lock:
            self._subscriptions.append((pattern, handler))

    def publish(
        self,
        topic: str,
        payload: Dict[str, Any],
        source: str = "",
        monotonic_ts: float = 0.0,
    ) -> None:
        message = Message(
            topic=topic, payload=payload, source=source, monotonic_ts=monotonic_ts
        )
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            try:
                evicted = self._queue.get_nowait()
            except queue.Empty:
                evicted = None
            self._dropped += 1
            LOGGER.error(
                "bus queue saturated, evicting oldest message",
                extra={
                    "topic": topic,
                    "evicted_topic": evicted.topic if evicted else None,
                    "dropped_total": self._dropped,
                },
            )
            try:
                self._queue.put_nowait(message)
            except queue.Full:
                return
        self._published += 1

    def drain(self, limit: int = 64) -> int:
        """Deliver up to ``limit`` queued messages. Returns the count delivered."""
        delivered = 0
        while delivered < limit:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(message)
            delivered += 1
        self._delivered += delivered
        return delivered

    def _dispatch(self, message: Message) -> None:
        with self._lock:
            handlers = [h for pattern, h in self._subscriptions if _matches(pattern, message.topic)]
        for handler in handlers:
            try:
                handler(message)
            except Exception:  # noqa: BLE001 - a bad subscriber must not kill the bus
                LOGGER.exception(
                    "subscriber raised while handling message",
                    extra={"topic": message.topic},
                )

    def start(self, poll_s: float = 0.05) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def _run() -> None:
            while not self._stop.is_set():
                if self.drain() == 0:
                    self._stop.wait(poll_s)

        self._thread = threading.Thread(target=_run, name=f"{self._name}-dispatch", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout_s)
        self.drain()
