"""System-spec detection + engine recommendation for the Models menu
(TSK-SPK-UX-MODELSELECT).

Deliberately modest: speakeasy ships exactly two dictation engines (see
engine/download.py) and both run comfortably via int8 CPU inference on any
Mac capable of running the app at all -- there is no real hardware-gated
speed/accuracy tradeoff here like SuperWhisper's tiny->large model lineup.
`recommend_engine` exists as ONE place for that logic to live if/when more
engine tiers are ever added, not to fabricate a spec-based recommendation
that doesn't actually exist yet.
"""
import subprocess
from dataclasses import dataclass


@dataclass
class SystemSpecs:
    chip: str
    ram_gb: float
    is_apple_silicon: bool


def detect_specs(run=subprocess.check_output) -> SystemSpecs:
    def _sysctl(key: str) -> str:
        try:
            return run(["sysctl", "-n", key]).decode().strip()
        except Exception:
            return ""

    chip = _sysctl("machdep.cpu.brand_string") or "Unknown"
    try:
        ram_gb = round(int(_sysctl("hw.memsize")) / (1024 ** 3), 1)
    except ValueError:
        ram_gb = 0.0
    return SystemSpecs(chip=chip, ram_gb=ram_gb, is_apple_silicon=chip.startswith("Apple"))


# Real engines only -- "whisper-translate" is an internal implementation
# detail of translate mode (spec item 1-4 in app.py), not a general-purpose
# dictation choice a user would pick here.
ENGINE_INFO = {
    "parakeet": {
        "label": "Parakeet-TDT 0.6B (int8)",
        "detail": "Fast local dictation — ~230ms, English-only, ~650MB",
    },
    "whisper": {
        "label": "Whisper small.en (int8)",
        "detail": "Alternate local engine — English-only, ~250MB",
    },
}


def recommend_engine(specs: SystemSpecs) -> str:
    return "parakeet"
