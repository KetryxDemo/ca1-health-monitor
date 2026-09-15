"""Control-loop and wiring tests."""

from __future__ import annotations

import json

import pytest

from health_monitor.config import ServiceConfig, from_mapping
from health_monitor.service import STATUS_TOPIC, HealthMonitorService, build_checks, parse_args


def _config(tmp_path) -> ServiceConfig:
    return from_mapping(
        {
            "loop_period_s": 0.01,
            "memory": {"region_bytes": 4096, "pages_per_slice": 1},
            "storage": {"mounts": [str(tmp_path)], "scan_period_s": 1.0,
                        "stall_threshold_s": 30.0},
        },
        environ={},
    )


def test_build_checks_covers_all_three_functions(tmp_path, bus):
    checks = build_checks(_config(tmp_path), bus)
    assert {c.check_id for c in checks} == {"ram", "disk", "heartbeat"}


def test_disabled_checks_are_not_built(tmp_path, bus):
    config = from_mapping({"memory": {"enabled": False}}, environ={})
    checks = build_checks(config, bus)
    assert "ram" not in {c.check_id for c in checks}


def test_step_publishes_and_counts(tmp_path, bus):
    service = HealthMonitorService(_config(tmp_path), bus=bus)
    telemetry = service.step()
    assert service.iterations == 1
    assert telemetry["results"] >= 1


def test_run_stops_at_max_iterations(tmp_path, bus):
    service = HealthMonitorService(_config(tmp_path), bus=bus)
    assert service.run(max_iterations=3) == 0
    assert service.iterations == 3


def test_shutdown_publishes_status(tmp_path, bus):
    service = HealthMonitorService(_config(tmp_path), bus=bus)
    service.run(max_iterations=2)
    status = [m for m in bus.sent if m.topic == STATUS_TOPIC]
    assert status
    assert set(status[-1].payload["checks"]) == {"ram", "disk", "heartbeat"}


def test_sleep_budget_never_exceeds_loop_period(tmp_path, bus):
    config = _config(tmp_path)
    service = HealthMonitorService(config, bus=bus)
    service.step()
    assert 0.0 <= service._sleep_budget() <= config.loop_period_s


def test_a_raising_check_does_not_stop_the_loop(tmp_path, bus):
    config = _config(tmp_path)
    service = HealthMonitorService(config, bus=bus)

    exploding = service.checks[0]
    exploding.on_tick = lambda tick: (_ for _ in ()).throw(RuntimeError("boom"))

    assert service.step()["results"] >= 0
    assert service.iterations == 1


def test_parse_args_defaults():
    args = parse_args([])
    assert args.config.endswith("health-monitor.json")
    assert args.max_iterations is None
