import json
from pathlib import Path
from speakeasy.config import (
    Settings,
    load_host_engine_settings,
    load_host_settings_record,
    load_settings,
    save_settings,
)

def test_defaults(tmp_path: Path):
    s = load_settings(tmp_path)          # no file yet -> defaults
    assert s.hotkey_keycode == 96        # F5
    assert s.engine == "parakeet"
    assert s.active_mode == "raw"
    assert s.account_allowlist == []
    assert s.translate_source_lang == "es"
    assert s.translate_audit_logprob_threshold == -0.55
    assert s.translate_audit_enabled is True
    assert s.inference_ranking == []     # single-host, unchanged behavior by default
    assert s.host_addresses == {}

def test_roundtrip(tmp_path: Path):
    s = load_settings(tmp_path)
    s.active_mode = "clean"
    save_settings(s, tmp_path)
    assert load_settings(tmp_path).active_mode == "clean"

def test_input_device_defaults_to_none_system_default(tmp_path: Path):
    assert load_settings(tmp_path).input_device is None

def test_input_device_roundtrip(tmp_path: Path):
    s = load_settings(tmp_path)
    s.input_device = 2
    save_settings(s, tmp_path)
    assert load_settings(tmp_path).input_device == 2

def test_input_device_explicit_none_roundtrips_as_system_default(tmp_path: Path):
    # TSK-SPK-MICSELECT's stale-device fallback (speakeasy/gui/input_device.py)
    # persists None to mean "use the system default" -- confirm that survives
    # a round trip distinctly from "never set" (both read back None, but via
    # different code paths: defaults vs an explicit save).
    s = load_settings(tmp_path)
    s.input_device = 3
    save_settings(s, tmp_path)
    s2 = load_settings(tmp_path)
    s2.input_device = None
    save_settings(s2, tmp_path)
    assert load_settings(tmp_path).input_device is None

def test_ranking_and_host_addresses_roundtrip(tmp_path: Path):
    s = load_settings(tmp_path)
    s.inference_ranking = ["linux", "self"]
    s.host_addresses = {"linux": "192.0.2.3:7420"}
    save_settings(s, tmp_path)
    loaded = load_settings(tmp_path)
    assert loaded.inference_ranking == ["linux", "self"]
    assert loaded.host_addresses == {"linux": "192.0.2.3:7420"}

def test_load_host_settings_record_missing_file_returns_empty(tmp_path: Path):
    assert load_host_settings_record(tmp_path) == {}
    assert load_host_engine_settings(tmp_path) == {}

def test_load_host_settings_record_and_engine_subset(tmp_path: Path):
    record = {"host_id": "linux-host", "updated": "2026-08-17",
              "engine": {"provider": "cpu", "measured_p95_ms": {"3s_clip": 216.1}}}
    (tmp_path / "host_settings.json").write_text(json.dumps(record))
    assert load_host_settings_record(tmp_path) == record
    assert load_host_engine_settings(tmp_path) == record["engine"]
