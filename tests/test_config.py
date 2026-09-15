"""Configuration loading and validation tests."""

from __future__ import annotations

import json

import pytest

from health_monitor.config import ConfigError, ServiceConfig, describe, from_mapping, load


def test_defaults_are_valid():
    config = from_mapping({}, environ={})
    assert config.service_name == "health-monitor"
    assert config.heartbeat.peer_timeout_s > config.heartbeat.emit_period_s


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown configuration key"):
        from_mapping({"heartbeat": {"peer_timeuot_s": 4.0}}, environ={})


def test_env_override_applies_to_a_section():
    config = from_mapping({}, environ={"HM_HEARTBEAT__PEER_TIMEOUT_S": "9.5"})
    assert config.heartbeat.peer_timeout_s == pytest.approx(9.5)


def test_env_override_parses_lists():
    config = from_mapping({}, environ={"HM_STORAGE__MOUNTS": "/a, /b"})
    assert config.storage.mounts == ["/a", "/b"]


def test_peer_timeout_must_exceed_emit_period():
    with pytest.raises(ConfigError, match="peer_timeout_s"):
        from_mapping({"heartbeat": {"emit_period_s": 5.0, "peer_timeout_s": 2.0}}, environ={})


def test_fault_floor_must_sit_below_warning_floor():
    with pytest.raises(ConfigError, match="free_fault_fraction"):
        from_mapping(
            {"storage": {"free_warn_fraction": 0.1, "free_fault_fraction": 0.3}},
            environ={},
        )


def test_stall_threshold_must_exceed_scan_period():
    with pytest.raises(ConfigError, match="stall_threshold_s"):
        from_mapping(
            {"storage": {"scan_period_s": 60.0, "stall_threshold_s": 30.0}}, environ={}
        )


def test_quorum_cannot_exceed_peer_count():
    with pytest.raises(ConfigError, match="quorum_min_live"):
        from_mapping({"heartbeat": {"peers": ["a", "b"], "quorum_min_live": 3}}, environ={})


def test_load_reads_a_file(tmp_path):
    path = tmp_path / "hm.json"
    path.write_text(json.dumps({"instance_id": "hm-7", "memory": {"scan_period_s": 2.0}}))
    config = load(path, environ={})
    assert config.instance_id == "hm-7"
    assert config.memory.scan_period_s == pytest.approx(2.0)


def test_load_reports_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "absent.json", environ={})


def test_describe_flattens_nested_sections():
    flattened = describe(ServiceConfig())
    assert "heartbeat.peer_timeout_s" in flattened
    assert "memory.scan_period_s" in flattened
