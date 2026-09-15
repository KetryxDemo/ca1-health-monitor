"""Unit tests for the shared interval timer."""

from __future__ import annotations

import pytest

from health_monitor.common.interval_timer import (
    IntervalTimer,
    TimerConfigurationError,
)


def test_rejects_non_positive_period(clock):
    with pytest.raises(TimerConfigurationError):
        IntervalTimer(period_s=0.0, clock=clock)


def test_rejects_non_positive_expiry(clock):
    with pytest.raises(TimerConfigurationError):
        IntervalTimer(period_s=1.0, expiry_s=-1.0, clock=clock)


def test_cadence_becomes_due_after_one_period(clock):
    timer = IntervalTimer(period_s=5.0, clock=clock)
    assert timer.poll().due is False

    clock.advance(4.999)
    assert timer.poll().due is False

    clock.advance(0.001)
    assert timer.poll().due is True


def test_start_due_fires_immediately(clock):
    timer = IntervalTimer(period_s=5.0, clock=clock, start_due=True)
    assert timer.poll().due is True


def test_mark_serviced_reanchors_cadence(clock):
    timer = IntervalTimer(period_s=5.0, clock=clock)
    clock.advance(6.0)
    tick = timer.poll()
    assert tick.due is True

    timer.mark_serviced(tick.now)
    assert timer.poll().due is False
    clock.advance(5.0)
    assert timer.poll().due is True


def test_overdue_and_missed_slots_reported(clock):
    timer = IntervalTimer(period_s=2.0, clock=clock)
    clock.advance(7.0)
    tick = timer.poll()
    assert tick.due is True
    assert tick.overdue_by == pytest.approx(5.0)
    assert tick.missed_slots == 2


def test_seconds_until_due(clock):
    timer = IntervalTimer(period_s=10.0, clock=clock)
    clock.advance(3.0)
    assert timer.seconds_until_due() == pytest.approx(7.0)
    clock.advance(20.0)
    assert timer.seconds_until_due() == 0.0


def test_expiry_disabled_by_default(clock):
    timer = IntervalTimer(period_s=1.0, clock=clock)
    clock.advance(10_000.0)
    tick = timer.poll()

    assert timer.expiry_enabled is False
    assert tick.expiry_enabled is False
    assert tick.expired is False
    assert tick.time_since_refresh is None


def test_expiry_fires_only_past_the_window(clock):
    timer = IntervalTimer(period_s=1.0, expiry_s=3.0, clock=clock)
    assert timer.expiry_enabled is True

    clock.advance(3.0)
    assert timer.poll().expired is False

    clock.advance(0.01)
    tick = timer.poll()
    assert tick.expired is True
    assert tick.time_since_refresh == pytest.approx(3.01)


def test_refresh_clears_expiry(clock):
    timer = IntervalTimer(period_s=1.0, expiry_s=3.0, clock=clock)
    clock.advance(5.0)
    assert timer.poll().expired is True

    timer.refresh()
    assert timer.poll().expired is False


def test_refresh_does_not_disturb_cadence(clock):
    timer = IntervalTimer(period_s=5.0, expiry_s=3.0, clock=clock)
    clock.advance(4.0)
    timer.refresh()
    assert timer.poll().due is False
    clock.advance(1.0)
    assert timer.poll().due is True


def test_mark_serviced_does_not_clear_expiry(clock):
    timer = IntervalTimer(period_s=1.0, expiry_s=3.0, clock=clock)
    clock.advance(5.0)
    timer.mark_serviced()
    assert timer.poll().expired is True


def test_reset_clears_both_anchors(clock):
    timer = IntervalTimer(period_s=5.0, expiry_s=3.0, clock=clock)
    clock.advance(10.0)
    assert timer.poll().due is True
    assert timer.poll().expired is True

    timer.reset()
    tick = timer.poll()
    assert tick.due is False
    assert tick.expired is False


def test_poll_is_side_effect_free(clock):
    timer = IntervalTimer(period_s=2.0, expiry_s=4.0, clock=clock)
    clock.advance(5.0)
    first = timer.poll()
    second = timer.poll()
    assert first == second
