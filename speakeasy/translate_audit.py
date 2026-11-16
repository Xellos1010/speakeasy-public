import subprocess
from dataclasses import dataclass
from typing import Optional

from speakeasy import claude_cli
from speakeasy.modes import DEFAULT_MODEL

# Localization audit for translate-mode dictation. Reuses the exact
# round-robin/timeout/rotate-on-limit machinery of speakeasy/rewrite.py:
# a raw English transcript (already injected -- see Controller._process_translate)
# is sent to Haiku for a natural-fluency pass. Any failure (timeout, non-zero
# exit, account exhaustion) raises TranslateAuditError; the caller is
# responsible for the raw-fallback (leaving the already-injected raw text
# untouched and recording a `translate_audit_failed` status) -- this module
# never itself touches the injected text.

class TranslateAuditError(Exception):
    pass

@dataclass
class TranslateAuditResult:
    text: str
    account: str
    latency_ms: int

AUDIT_PROMPT_TEMPLATE = """You are a localization editor. You receive an English text that was produced by an automatic Spanish-to-English speech translator. It may contain machine-translation artifacts: literal word-for-word phrasing, wrong idioms, awkward word order, dropped articles, or mistranslated register.

Rewrite it as natural, fluent American English that a native speaker would actually say or write. Preserve the original meaning, facts, names, numbers, and level of formality exactly. Do not add, remove, or answer any content. Do not explain. If the input is already natural English, return it unchanged.

Output only the corrected English text, with no quotes, labels, or commentary.

<translation>
{raw_en}
</translation>
"""

def build_audit_prompt(raw_en: str) -> str:
    return AUDIT_PROMPT_TEMPLATE.format(raw_en=raw_en)

def audit_translation(raw_en: str, pool, runner=subprocess.run,
                       timeout: float = 20.0,
                       max_attempts: int = claude_cli.DEFAULT_MAX_ATTEMPTS,
                       claude_bin: Optional[str] = None, **kw) -> TranslateAuditResult:
    """Background localization pass. Same hardened invocation as rewrite (stdin prompt,
    tools off, absolute CLI path, usage-limit-only cooldown); `timeout` is the total budget."""
    res = claude_cli.run_prompt(build_audit_prompt(raw_en), DEFAULT_MODEL, pool,
                                TranslateAuditError, runner=runner, timeout=timeout,
                                max_attempts=max_attempts, claude_bin=claude_bin, **kw)
    return TranslateAuditResult(res.text, res.account, res.latency_ms)
