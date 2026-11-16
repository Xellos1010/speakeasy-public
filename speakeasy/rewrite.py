import subprocess
from dataclasses import dataclass
from typing import Optional

from speakeasy import claude_cli
from speakeasy.modes import Mode


class RewriteError(Exception):
    pass


@dataclass
class RewriteResult:
    text: str
    account: str
    latency_ms: int


def build_prompt(transcript: str, mode: Mode) -> str:
    return f"{mode.system_prompt}\n\n<dictation>\n{transcript}\n</dictation>"


def rewrite(transcript: str, mode: Mode, pool, runner=subprocess.run,
            timeout: float = claude_cli.DEFAULT_TIMEOUT_S,
            max_attempts: int = claude_cli.DEFAULT_MAX_ATTEMPTS,
            claude_bin: Optional[str] = None, **kw) -> RewriteResult:
    """LLM rewrite of a dictation. `timeout` is the TOTAL wall-clock budget across all
    attempts (P1-4). Every failure -- missing CLI, timeout, non-zero exit, exhaustion --
    raises RewriteError; the caller injects the raw text instead (P0-1)."""
    if mode.system_prompt is None:
        return RewriteResult(transcript, "none", 0)
    res = claude_cli.run_prompt(build_prompt(transcript, mode), mode.model, pool,
                                RewriteError, runner=runner, timeout=timeout,
                                max_attempts=max_attempts, claude_bin=claude_bin, **kw)
    return RewriteResult(res.text, res.account, res.latency_ms)
