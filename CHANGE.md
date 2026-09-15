# Demo change (operator note, not part of the product)
File: `src/health_monitor/common/interval_timer.py`
Functions: `IntervalTimer.__init__` and `IntervalTimer.poll`

Change: expiry stops being an absolute wall measurement and becomes a count of
whole cadence periods. Add `import math`, add `"_expiry_intervals"` to
`__slots__`, and in `__init__` set `self._expiry_intervals = None if expiry_s
is None else math.ceil(expiry_s / period_s)`. The None guard is required: RAM,
DISK and the heartbeat emit timer all construct with `expiry_s=None`; unguarded
it raises TypeError and nothing starts. In `poll`, below the early return
already taken when `self._expiry_s is None`, replace
`expired = time_since_refresh > self._expiry_s` with
`expired = int(time_since_refresh // self._period_s) >= self._expiry_intervals`.

Naive blast radius: all three - every check imports the timer and polls it.
Verified: exactly 2 tests move - the timer's own `test_expiry_fires_only_past_the_window`
and heartbeat's `test_peer_is_declared_lost_once_past_the_timeout`. Real radius
is FN-HM-HB only: only `heartbeat.py` builds expiry-bearing timers (one per
peer) and `expired` alone declares a peer lost. RAM and DISK never reach the
branch; DISK's decoy `stall_threshold_s` comes from its own `_stale_for()`.
