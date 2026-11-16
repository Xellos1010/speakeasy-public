from dataclasses import dataclass, replace
from pathlib import Path
import yaml

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

@dataclass
class Mode:
    name: str
    system_prompt: str | None
    model: str = DEFAULT_MODEL

_BUILTINS: dict[str, str | None] = {
    "raw": None,
    "clean": ("You clean up dictated text. Fix punctuation, casing, and obvious "
              "speech artifacts (um, uh, repeated words). Preserve the speaker's words "
              "and meaning. Output only the cleaned text."),
    "email": ("Rewrite the dictated text as a clear, professional email body. "
              "Keep it concise and friendly. Output only the email text."),
    "prompt-polish": ("Rewrite the dictated text as a precise, well-structured prompt "
                      "for an AI coding assistant. Output only the prompt."),
    # translate: NOT a rewrite-path mode. system_prompt stays None so it never
    # enters the Controller's LLM-rewrite branch; localization correction is a
    # separate, confidence-gated background audit (see speakeasy/translate_audit.py)
    # that never blocks the immediate raw injection.
    "translate": None,
}

def load_modes(path: Path | None = None) -> dict[str, Mode]:
    modes = {n: Mode(n, sp) for n, sp in _BUILTINS.items()}
    if path is not None and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text()) or {}
        for name, cfg in data.items():
            if name == "raw" or not isinstance(cfg, dict):
                continue
            base = modes.get(name, Mode(name, None))
            modes[name] = replace(
                base,
                system_prompt=cfg.get("system_prompt", base.system_prompt),
                model=cfg.get("model", base.model))
    return modes
