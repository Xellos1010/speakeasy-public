from dataclasses import dataclass

from speakeasy.config import Settings
from speakeasy.gui.handlers import models
from speakeasy.sysinfo import SystemSpecs


@dataclass
class _FakeCtx:
    settings: Settings
    _specs: SystemSpecs
    _switch_result: dict

    def detect_specs(self):
        return self._specs

    def switch_engine(self, name):
        self.last_switch_arg = name
        return self._switch_result


def _make_ctx(active_engine="parakeet", specs=None, switch_result=None):
    return _FakeCtx(
        settings=Settings(engine=active_engine),
        _specs=specs or SystemSpecs("Apple M2 Pro", 16.0, True),
        _switch_result=switch_result or {"started": True},
    )


def test_get_state_reflects_currently_active_engine():
    ctx = _make_ctx(active_engine="whisper")
    result = models.models_get_state({}, ctx)

    assert result["active_engine"] == "whisper"
    engines_by_key = {e["key"]: e for e in result["engines"]}
    assert engines_by_key["whisper"]["is_active"] is True
    assert engines_by_key["parakeet"]["is_active"] is False


def test_get_state_includes_real_system_specs():
    ctx = _make_ctx(specs=SystemSpecs("Apple M3 Max", 64.0, True))
    result = models.models_get_state({}, ctx)

    assert result["chip"] == "Apple M3 Max"
    assert result["ram_gb"] == 64.0


def test_get_state_lists_exactly_the_two_real_engines():
    ctx = _make_ctx()
    result = models.models_get_state({}, ctx)

    keys = {e["key"] for e in result["engines"]}
    assert keys == {"parakeet", "whisper"}
    for engine in result["engines"]:
        assert engine["label"]
        assert engine["detail"]


def test_recommended_flag_only_on_parakeet():
    ctx = _make_ctx(active_engine="whisper")
    result = models.models_get_state({}, ctx)

    assert result["recommended_engine"] == "parakeet"
    engines_by_key = {e["key"]: e for e in result["engines"]}
    assert engines_by_key["parakeet"]["is_recommended"] is True
    assert engines_by_key["whisper"]["is_recommended"] is False


def test_switch_engine_passes_through_started_result():
    ctx = _make_ctx(active_engine="parakeet", switch_result={"started": True})
    result = models.models_switch_engine({"engine": "whisper"}, ctx)

    assert result == {"started": True}
    assert ctx.last_switch_arg == "whisper"


def test_switch_engine_passes_through_already_active_result():
    ctx = _make_ctx(active_engine="parakeet",
                     switch_result={"already_active": True})
    result = models.models_switch_engine({"engine": "parakeet"}, ctx)

    assert result == {"already_active": True}
    assert ctx.last_switch_arg == "parakeet"


def test_switch_engine_passes_through_failure_result():
    ctx = _make_ctx(switch_result={"ok": False, "engine": "whisper",
                                    "error": "download failed"})
    result = models.models_switch_engine({"engine": "whisper"}, ctx)

    assert result["ok"] is False
    assert result["error"] == "download failed"
