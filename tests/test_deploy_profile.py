"""Deployment profile (docs/PROFILE.md): resolution, validation, safe defaults, wiring."""
import json
from pathlib import Path

import pytest

from speakeasy import deploy_profile as dp

VALID = {
    "schema_version": 1,
    "profile_name": "t",
    "server": {"public_url": "https://host.example.com:8445"},
    "hosts": {"inference_ranking": ["other"], "host_addresses": {"other": "192.0.2.7:7420"}},
    "accounts": {"accounts_file": "~/acc.json"},
    "operator_overlay": {"id": "me", "dir": "~/ov"},
    "android": {"store_password_env": "MY_KS_PW"},
    "secrets": {"ks": {"source": "env", "name": "MY_KS_PW"}},
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No profile anywhere: empty XDG dir, SPEAKEASY_PROFILE unset."""
    monkeypatch.delenv(dp.ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SPEAKEASY_MOBILE_BASE_URL", raising=False)
    monkeypatch.delenv("OPERATOR_PROFILE", raising=False)
    monkeypatch.delenv("OPERATOR_PROFILES_DIR", raising=False)
    return tmp_path


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_no_profile_gives_defaults(isolated):
    assert dp.profile_path() is None
    prof = dp.load()
    assert prof.data == {} and prof.source is None
    assert prof.get("server.public_url") is None
    assert dp.accounts_path() == Path.home() / ".claude-accounts.json"


def test_env_var_dir_and_file_forms(isolated, monkeypatch):
    f = _write(isolated / "p" / "profile.json", VALID)
    monkeypatch.setenv(dp.ENV_VAR, str(isolated / "p"))
    assert dp.profile_path() == f
    monkeypatch.setenv(dp.ENV_VAR, str(f))
    assert dp.profile_path() == f
    assert dp.load(strict=True).get("server.public_url") == "https://host.example.com:8445"


def test_xdg_default_location(isolated):
    f = _write(isolated / "xdg" / "speakeasy" / "profile" / "profile.json", VALID)
    assert dp.profile_path() == f


def test_env_var_pointing_nowhere_means_defaults(isolated, monkeypatch):
    monkeypatch.setenv(dp.ENV_VAR, str(isolated / "missing"))
    assert dp.load().data == {}


@pytest.mark.parametrize("bad", [
    {"schema_version": 2},
    {"schema_version": 1, "unknown": 1},
    {"schema_version": 1, "server": {"public_url": "http://insecure.example.com"}},
    {"schema_version": 1, "android": {"store_password_env": "not-an-env-name"}},
    {"schema_version": 1, "hosts": {"host_addresses": {"x": "no-port"}}},
    {"schema_version": 1, "secrets": {"k": {"source": "plaintext", "name": "x"}}},
    {"schema_version": 1, "secrets": {"k": {"source": "env", "name": "x", "value": "hunter2"}}},
    {},
])
def test_schema_rejects(bad):
    assert dp.validate(bad), bad


def test_valid_example_has_no_errors():
    assert dp.validate(VALID) == []


def test_invalid_profile_never_breaks_startup_but_strict_raises(isolated, monkeypatch):
    f = _write(isolated / "bad.json", {"schema_version": 1, "oops": True})
    monkeypatch.setenv(dp.ENV_VAR, str(f))
    assert dp.load().data == {}
    with pytest.raises(dp.ProfileError):
        dp.load(strict=True)


def test_secret_from_env_returns_value_without_storing_it(isolated, monkeypatch):
    f = _write(isolated / "p.json", VALID)
    monkeypatch.setenv(dp.ENV_VAR, str(f))
    monkeypatch.setenv("MY_KS_PW", "s3cret-test-value")
    assert dp.load().secret_from_env("android.store_password_env") == "s3cret-test-value"
    assert "s3cret" not in json.dumps(dp.load().data)


def test_public_url_precedence_env_then_profile(isolated, monkeypatch):
    from speakeasy.mobile import cli
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert cli.public_base_url() == cli.DEFAULT_PUBLIC_URL
    f = _write(isolated / "p.json", VALID)
    monkeypatch.setenv(dp.ENV_VAR, str(f))
    assert cli.public_base_url() == "https://host.example.com:8445"
    monkeypatch.setenv("SPEAKEASY_MOBILE_BASE_URL", "https://env.example.com/")
    assert cli.public_base_url() == "https://env.example.com"


def test_settings_defaults_come_from_profile_only_for_default_dir(isolated, monkeypatch):
    from speakeasy import config
    f = _write(isolated / "p.json", VALID)
    monkeypatch.setenv(dp.ENV_VAR, str(f))
    monkeypatch.setattr(config, "speakeasy_dir", lambda: isolated / "home")
    (isolated / "home").mkdir()
    s = config.load_settings()
    assert s.inference_ranking == ["other"] and s.host_addresses == {"other": "192.0.2.7:7420"}
    # explicit dir (tests, tools) is untouched by the profile
    assert config.load_settings(isolated / "home").inference_ranking == []
    # settings.json wins over the profile
    (isolated / "home" / "settings.json").write_text(json.dumps({"inference_ranking": ["mine"]}))
    assert config.load_settings().inference_ranking == ["mine"]


def test_operator_overlay_defaults_are_generic(isolated):
    from speakeasy.mobile import operator_overlay as ov
    assert ov.profile_id() == "default"
    assert ov.profiles_dir() == ov.DEFAULT_PROFILES_DIR
    assert "Projects" not in str(ov.DEFAULT_PROFILES_DIR)
