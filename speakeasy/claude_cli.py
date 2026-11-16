"""Headless `claude -p` invocation shared by rewrite modes and the translate audit.

Audit 2026-11-09 fixes:
- P0-1: a Dock/LaunchServices-launched app has no ``~/.local/bin`` on PATH, so the bare
  name ``claude`` raised FileNotFoundError and lost the dictation. The binary is now
  resolved to an absolute path (settings override, env override, PATH, then the known
  install locations), and every OS-level failure becomes the caller's error class.
- P1-4: one wall-clock budget for ALL attempts (default 8 s, configurable), at most two
  attempts, and an account is cooled down only when its output carries a measured
  usage-limit signature -- never for timeouts, auth or generic failures.
- P1-7: the dictation goes over stdin, never argv (``ps`` would show it); tools are
  disabled (``--tools ""``), customizations/hooks/MCP are skipped (``--safe-mode``,
  ``--strict-mcp-config``), nothing is persisted (``--no-session-persistence``), and the
  process runs in an empty temporary cwd.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_TIMEOUT_S = 8.0
DEFAULT_MAX_ATTEMPTS = 2
# Below this much remaining budget a new attempt cannot realistically finish.
MIN_ATTEMPT_S = 1.0
USAGE_LIMIT_COOLDOWN_S = 3600.0
# An account whose login is expired/revoked stays broken until someone logs it in
# again; skip it for longer so dictations stop paying for it (FIX-LIST P1-4 allows
# cooling on usage-limit OR auth signatures, nothing else).
AUTH_FAILURE_COOLDOWN_S = 6 * 3600.0
ENV_OVERRIDE = "SPEAKEASY_CLAUDE_BIN"


def candidate_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
        home / ".claude" / "local" / "claude",
        home / ".npm-global" / "bin" / "claude",
    ]


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def resolve_claude(override: Optional[str] = None) -> Optional[str]:
    """Absolute path of the claude CLI, or None. Order: explicit override (settings
    ``claude_bin``), ``$SPEAKEASY_CLAUDE_BIN``, ``PATH``, then known install dirs."""
    for explicit in (override, os.environ.get(ENV_OVERRIDE)):
        if explicit:
            p = Path(explicit).expanduser()
            if _executable(p):
                return str(p)
    found = shutil.which("claude")
    if found:
        return found
    for p in candidate_paths():
        if _executable(p):
            return str(p)
    return None


_USAGE_LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|limit reached|limit will reset|resets? at|"
    r"out of (?:extra )?usage|quota|credit balance is too low|\b429\b",
    re.IGNORECASE)


def is_usage_limit(output: str) -> bool:
    return bool(output) and bool(_USAGE_LIMIT_RE.search(output))


_AUTH_FAILURE_RE = re.compile(
    r"failed to authenticate|oauth (?:session|token) (?:has )?expired|invalid api key|"
    r"please run /login|not logged in|authentication_error|\b401\b|token (?:has been )?revoked",
    re.IGNORECASE)


def is_auth_failure(output: str) -> bool:
    return bool(output) and bool(_AUTH_FAILURE_RE.search(output))


def build_cmd(exe: str, model: str) -> list[str]:
    """argv for one call. Must never contain the dictation (P1-7): the prompt is stdin."""
    return [exe, "-p", "--model", model, "--output-format", "text",
            "--tools", "", "--safe-mode", "--strict-mcp-config",
            "--no-session-persistence"]


@dataclass
class ClaudeResult:
    text: str
    account: str
    latency_ms: int


def _tail(s: str, n: int = 200) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[-n:]


def run_prompt(prompt: str, model: str, pool, error_cls: type[Exception],
               runner: Callable = subprocess.run,
               timeout: float = DEFAULT_TIMEOUT_S,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               claude_bin: Optional[str] = None,
               clock: Callable[[], float] = time.monotonic,
               resolver: Optional[Callable[[Optional[str]], Optional[str]]] = None,
               ) -> ClaudeResult:
    """Round-robin `claude -p` over the account pool within ONE wall-clock budget.
    Raises ``error_cls`` with a reason on any failure; never lets OSError escape."""
    exe = (resolver or resolve_claude)(claude_bin)
    if exe is None:
        raise error_cls("claude CLI not found (searched PATH, ~/.local/bin, /usr/local/bin, "
                        "/opt/homebrew/bin; set claude_bin in ~/.speakeasy/settings.json)")
    deadline = clock() + timeout
    errors: list[str] = []
    for _ in range(max_attempts):
        remaining = deadline - clock()
        if remaining < MIN_ATTEMPT_S:
            errors.append(f"time budget {timeout:.0f}s exhausted")
            break
        acc = pool.next_account()
        if acc is None:
            errors.append("no eligible account")
            break
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(acc.dir)}
        t0 = clock()
        try:
            with tempfile.TemporaryDirectory(prefix="speakeasy-claude-") as cwd:
                # encoding pinned: a Dock/launchd-launched app has no LANG, so
                # text=True alone decodes as ASCII and any non-ASCII byte in
                # claude's output raised UnicodeDecodeError (measured 2026-11-09).
                proc = runner(build_cmd(exe, model), input=prompt, env=env, cwd=cwd,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=remaining)
        except subprocess.TimeoutExpired:
            errors.append(f"{acc.name}: timeout after {remaining:.1f}s")
            continue
        except OSError as exc:  # exec failure: same for every account
            errors.append(f"claude CLI could not run: {type(exc).__name__}: {exc}")
            break
        out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if proc.returncode != 0:
            if is_usage_limit(out):
                pool.mark_cooling(acc.name, USAGE_LIMIT_COOLDOWN_S)
            elif is_auth_failure(out):
                pool.mark_cooling(acc.name, AUTH_FAILURE_COOLDOWN_S)
            errors.append(f"{acc.name}: exit {proc.returncode}: {_tail(out)}")
            continue
        text = (proc.stdout or "").strip()
        if not text:
            errors.append(f"{acc.name}: empty output")
            continue
        if len(text) < 300 and is_usage_limit(text) and re.search(
                r"usage limit|limit reached|out of (?:extra )?usage", text, re.IGNORECASE):
            # A limit notice printed on stdout with exit 0 must never be injected.
            pool.mark_cooling(acc.name, USAGE_LIMIT_COOLDOWN_S)
            errors.append(f"{acc.name}: usage limit: {_tail(text)}")
            continue
        return ClaudeResult(text, acc.name, int((clock() - t0) * 1000))
    raise error_cls(" | ".join(errors) or "no eligible account")
