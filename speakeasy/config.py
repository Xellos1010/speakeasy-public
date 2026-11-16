import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

@dataclass
class Settings:
    hotkey_keycode: int = 96          # F5 (legacy; kept for backward compat)
    hotkey: str | int = "right_command"  # "right_command" or a legacy int keycode
    # Linux only (speakeasy.injector_linux): "paste" (clipboard save/restore,
    # matches macOS semantics) or "type" (xdotool types the text directly,
    # bypassing the clipboard entirely). No effect on macOS, which is
    # paste-only.
    injection_mode: str = "paste"
    engine: str = "parakeet"
    input_device: int | None = None
    active_mode: str = "raw"
    account_allowlist: list[str] = field(default_factory=list)
    translate_source_lang: str = "es"
    translate_audit_logprob_threshold: float = -0.55
    translate_audit_enabled: bool = True
    # Multi-host network STT (docs/superpowers/specs/2026-08-18-network-service.md).
    # inference_ranking omits "self" by default -- speakeasy.ranking.load_ranking()
    # always appends it, so an empty/default list here means single-host, unchanged
    # behavior (R2.1/R2.4). host_addresses maps each non-self ranking entry to
    # "ip:port" for the host it actually reaches.
    inference_ranking: list[str] = field(default_factory=list)
    host_addresses: dict[str, str] = field(default_factory=dict)
    # Rewrite modes (audit 2026-11-09 P0-1/P1-4): absolute path to the claude CLI
    # ("" = auto-discover: PATH, ~/.local/bin, /usr/local/bin, /opt/homebrew/bin), and
    # the TOTAL wall-clock budget before the raw transcript is inserted instead.
    claude_bin: str = ""
    rewrite_timeout_s: float = 8.0

_PRIVATE_FILES = ("history.db", "history.db-wal", "history.db-shm", "history.db-journal",
                  "settings.json", "state.json", "host_settings.json")
_PRIVATE_DIRS = ("profile", "training", "run", "audio")


def secure_tree(d: Path) -> None:
    """P2-3: private data is owner-only. Dirs 0700, transcript/settings/state files
    0600, everything under training/ and profile/ (voice clips, profiles) 0600/0700.
    Idempotent; migrates an existing ~/.speakeasy on every start."""
    def _chmod(p: Path, mode: int) -> None:
        try:
            if not p.is_symlink() and (p.stat().st_mode & 0o777) != mode:
                os.chmod(p, mode)
        except OSError:
            pass
    _chmod(d, 0o700)
    for name in _PRIVATE_FILES:
        p = d / name
        if p.exists():
            _chmod(p, 0o600)
    for name in _PRIVATE_DIRS:
        root = d / name
        if not root.is_dir():
            continue
        _chmod(root, 0o700)
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames:
                _chmod(Path(dirpath) / n, 0o700)
            for n in filenames:
                _chmod(Path(dirpath) / n, 0o600)


def write_private(path: Path, text: str) -> None:
    """Write a file created/kept at 0600 (atomic replace)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def speakeasy_dir() -> Path:
    d = Path.home() / ".speakeasy"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if (d.stat().st_mode & 0o777) != 0o700:
            os.chmod(d, 0o700)
    except OSError:
        pass
    return d

def _settings_path(dir: Path | None) -> Path:
    return (dir or speakeasy_dir()) / "settings.json"

def _profile_host_defaults() -> dict:
    """Host-network defaults from the deployment profile (docs/PROFILE.md). Used only for the real
    per-user settings location, and only for keys settings.json does not set itself."""
    from speakeasy import deploy_profile
    prof = deploy_profile.load()
    out: dict = {}
    ranking = prof.get("hosts.inference_ranking")
    if ranking:
        out["inference_ranking"] = list(ranking)
    addrs = prof.get("hosts.host_addresses")
    if addrs:
        out["host_addresses"] = dict(addrs)
    return out


def load_settings(dir: Path | None = None) -> Settings:
    p = _settings_path(dir)
    profile_defaults = _profile_host_defaults() if dir is None else {}
    if not p.exists():
        return Settings(**profile_defaults)
    data = json.loads(p.read_text())
    data = {**profile_defaults, **data}
    known = {f for f in Settings.__dataclass_fields__}
    filtered = {k: v for k, v in data.items() if k in known}
    # Backward compat: old configs only had `hotkey_keycode`. If a file
    # predates the `hotkey` setting, honor the legacy keycode they had
    # instead of silently switching them to the new right_command default.
    if "hotkey_keycode" in data and "hotkey" not in data:
        filtered["hotkey"] = data["hotkey_keycode"]
    return Settings(**filtered)

def save_settings(s: Settings, dir: Path | None = None) -> None:
    write_private(_settings_path(dir), json.dumps(asdict(s), indent=2))

def load_host_settings_record(dir: Path | None = None) -> dict:
    """Full per-host record (docs/superpowers/specs/2026-08-18-linux-client-port.md §4 shape:
    {"host_id", "tailscale_ip", "updated", "engine": {"measured_p95_ms": {...}, ...}}),
    written by the benchmark. Returns {} if this host hasn't been benchmarked yet."""
    p = (dir or speakeasy_dir()) / "host_settings.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())

def load_host_engine_settings(dir: Path | None = None) -> dict:
    """Just the "engine" sub-object of load_host_settings_record() -- what
    RemoteFallbackEngine's deadline computation needs. {} if unbenchmarked."""
    return load_host_settings_record(dir).get("engine", {})
