# health-monitor

Resident platform service that watches the local node and the other services
running beside it. It is one of the services started by the platform
supervisor at boot and is expected to stay up for the life of the case.

The service owns three system functions:

| Function   | Module                              | What it does                                          |
|------------|-------------------------------------|-------------------------------------------------------|
| FN-HM-RAM  | `checks/memory_integrity.py`        | Sliced checksum sweep and ECC counter surveillance    |
| FN-HM-DISK | `checks/storage_health.py`          | Per-mount capacity, writability and latency probing   |
| FN-HM-HB   | `checks/heartbeat.py`               | Emits our heartbeat, tracks peer liveness and quorum  |

## Architecture

```
                 +---------------------------+
                 |    service.py (loop)      |
                 +------------+--------------+
                              |  service() once per iteration
        +---------------------+---------------------+
        |                     |                     |
+---------------+   +------------------+   +-----------------+
| memory_       |   | storage_health   |   | heartbeat       |
| integrity     |   |                  |   |                 |
| (FN-HM-RAM)   |   | (FN-HM-DISK)     |   | (FN-HM-HB)      |
+-------+-------+   +--------+---------+   +--------+--------+
        |                    |                      |
        +--------------------+----------------------+
                             |
                +------------v-------------+
                | common/interval_timer.py |
                +--------------------------+

              all results -> bus.py -> platform bus
```

Every check derives from `checks/base.py`. The base class polls the check's
timer once per control-loop iteration, hands the resulting `TimerTick` to the
check, and publishes whatever the check returns. The base class deliberately
does not interpret the tick: deciding whether to run, and acknowledging the
cadence slot, belong to the check, because the three checks consume different
parts of the tick.

### The shared timer

`common/interval_timer.py` provides `IntervalTimer`, which serves two separate
concerns off one monotonic clock read:

* **Cadence.** `poll().due` goes true every `period_s` seconds. The caller
  acknowledges the slot with `mark_serviced()`. Cadence is always active.
* **Expiry.** Only when `expiry_s` is supplied at construction. The timer then
  tracks the gap since the last `refresh()` and reports `poll().expired` once
  the gap passes the window. With `expiry_s` left at `None` the expiry fields
  are inert: `expiry_enabled` is `False`, `expired` is always `False`, and
  `time_since_refresh` is `None`.

A check that merely needs a heartbeat of its own scheduling constructs the
timer with a period alone. A check that waits on an event produced somewhere
else constructs it with a period and an expiry window, and reads the expiry
flag to decide that the event has not arrived.

## Control loop

`service.py` runs a single-threaded loop. Each iteration:

1. drains the message bus, delivering queued messages to subscribers;
2. calls `service()` on every enabled check;
3. sleeps until the nearest check is next due, capped at `loop_period_s`.

Checks are cooperative. None of them may block the loop for longer than its own
slice budget; the memory sweep in particular carries a cursor between
iterations so that a large region is walked over many slots rather than in one
long call.

## Message bus

`bus.py` is the local face of the platform bus client. Publishing is
non-blocking and queue-backed so that a slow subscriber cannot stall a
publisher inside the control loop. Topic patterns support a single trailing
wildcard segment, for example `peer.heartbeat.*`.

Topics published by this service:

* `health.ram` - one message per completed memory pass
* `health.disk.<mount-slug>` - one message per mount per scan
* `health.heartbeat.<peer>` - published on peer liveness transitions only
* `health.heartbeat.quorum` - published on quorum transitions only
* `health.monitor.status` - counters, published at shutdown
* `peer.heartbeat.health-monitor` - our own liveness beacon

## Configuration

JSON, layered over the built-in defaults, validated at load time. The service
refuses to start on an invalid configuration rather than silently falling back
to a default, so a mis-provisioned unit fails at commissioning rather than
mid-case. A sample lives at `config/health-monitor.json`.

Individual scalars can be overridden from the environment for bench work using
the `HM_` prefix and a double-underscore path separator:

```
HM_HEARTBEAT__PEER_TIMEOUT_S=4.0
HM_STORAGE__MOUNTS=/var/log,/var/lib/cases
```

Cross-field rules enforced at load:

* `heartbeat.peer_timeout_s` must exceed `heartbeat.emit_period_s`, so that a
  single late heartbeat cannot declare a peer lost.
* `storage.stall_threshold_s` must exceed `storage.scan_period_s`.
* `storage.free_fault_fraction` must sit below `storage.free_warn_fraction`.
* `heartbeat.quorum_min_live` cannot exceed the number of configured peers.

## Logging

One JSON object per line on stdout; the platform log shipper tails stdout per
service unit. Both wall clock and monotonic clock are recorded, because wall
clock can step during a case if the unit resynchronises with the site time
source. Set `logging.format` to `plain` for a human-readable bench format.

## Running

```
python -m health_monitor --config /etc/platform/health-monitor.json
```

Exit codes: `0` clean shutdown, `2` configuration error, `3` a check raised
during construction. `--max-iterations N` stops after N control-loop
iterations, which is how the bench harness drives it.

## Tests

```
python -m pytest
python -m pytest --cov
```

Every test drives an injected `FakeClock`, so the suite never sleeps and timing
behaviour is exercised deterministically. Checks take their platform
dependencies (memory source, storage probe) by injection for the same reason.
