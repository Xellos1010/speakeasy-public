"""Correction-rule post-processing pass wired into the transcription path
ahead of the mode/rewrite pipeline (TSK-SPK-TRAIN-T8).

Governing spec: docs/superpowers/specs/2026-07-18-voice-training-environment.md
- §5 note: "`corrections` are applied as a post-processing pass on the
  engine's raw output, ordered as listed, before the existing mode/rewrite
  pipeline (`speakeasy/app.py::Controller._process`) sees the text."
- §8 AC7: "`active.json`'s pointed-to profile is the only profile the
  runtime engine consults; rolling `active_version` back to `N-1` fully
  reverts behavior (no cached state elsewhere)."

This module deliberately does not reimplement profile loading or correction
application -- both are reused from `speakeasy.training.profile`
(`ProfileStore.load_active`, `apply_corrections`), per T5.

Also applies the active profile's hotwords via `apply_hotword_fuzzy_correction`
(TSK-SPK-TRANSFER): native decode-time hotwords/contextual-biasing is a
sherpa-onnx transducer-only mechanism (Whisper can't use it at all) and, even
on Parakeet, the bundled model export lacks the bpe_vocab it needs -- see
`speakeasy.engine.parakeet.HotwordsUnavailable`'s docstring -- so this
post-hoc fuzzy pass is, in practice, the only way a profile's hotword list
provides any real value today, on either engine. Applied unconditionally
(not gated on which engine is active): when native hotwords genuinely are
boosting a term correctly, the fuzzy pass finds nothing to fix and is a
harmless no-op; when they aren't (the common case), it's the fallback.
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path
from typing import Optional, Tuple, Union

from speakeasy.training.profile import (
    Profile,
    ProfileStore,
    apply_corrections,
    apply_hotword_fuzzy_correction,
)

PathLike = Union[str, Path]

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_DIR = Path("~/.speakeasy/profile").expanduser()

# Cache key: (active_version, active.json mtime_ns) once a pointer has been
# parsed successfully; `None` means "no active profile" and `"corrupt"`
# means "active.json exists but isn't valid" (§ corrupt-passthrough case).
_PointerKey = Union[None, str, Tuple[int, int]]


class Postprocessor:
    """Applies the active voice profile's correction rules to raw engine
    output.

    Caching (spec §8 AC7): the loaded `Profile` is cached in memory, but on
    every `process()` call `active.json` (a few bytes) is re-parsed and its
    mtime re-stat'd to detect a pointer flip or rollback -- this is the
    "cheap stat(), no re-read when unchanged" the spec calls for: what's
    actually expensive and skipped when the pointer hasn't moved is
    re-reading the (potentially much larger) `profile-v<N>.json` file, not
    the tiny pointer file itself. A flip or rollback is therefore picked up
    on the very next dictation with no process restart, and no state other
    than `active.json` + the versioned profile files is ever consulted
    (AC7's "no cached state elsewhere").

    Never crashes a dictation: no active profile, a corrupt/missing
    `active.json`, or a corrupt/missing `profile-v<N>.json` all fall back
    to identity passthrough (optionally logging a warning for the latter
    two, since those indicate real corruption rather than "no profile yet").
    """

    def __init__(self, profile_dir: PathLike = _DEFAULT_PROFILE_DIR):
        self.profile_dir = Path(profile_dir).expanduser()
        self._store = ProfileStore(self.profile_dir)
        self._cached_key: _PointerKey = None
        self._cached_profile: Optional[Profile] = None

    # -- pointer inspection ---------------------------------------------------

    def _read_pointer(self) -> _PointerKey:
        """Returns the current `active.json` cache key, or `None` if there is
        no active profile, or `"corrupt"` if `active.json` exists but can't
        be parsed into a version."""
        active_path = self.profile_dir / "active.json"
        try:
            stat = active_path.stat()
        except OSError:
            return None
        try:
            with open(active_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            version = data.get("active_version")
        except (OSError, ValueError) as exc:
            msg = f"speakeasy: corrupt active profile pointer at {active_path}: {exc}"
            logger.warning(msg)
            warnings.warn(msg, RuntimeWarning)
            return "corrupt"
        if version is None:
            return None
        return (version, stat.st_mtime_ns)

    def _load(self) -> Optional[Profile]:
        pointer = self._read_pointer()

        if pointer is None or pointer == "corrupt":
            self._cached_key = None
            self._cached_profile = None
            return None

        if pointer == self._cached_key and self._cached_profile is not None:
            return self._cached_profile

        version, _mtime_ns = pointer
        try:
            profile = self._store.read(version)
        except (OSError, ValueError, KeyError) as exc:
            msg = (
                f"speakeasy: failed to load voice profile version {version} from "
                f"{self.profile_dir}: {exc}"
            )
            logger.warning(msg)
            warnings.warn(msg, RuntimeWarning)
            self._cached_key = None
            self._cached_profile = None
            return None

        self._cached_key = pointer
        self._cached_profile = profile
        return profile

    # -- public API ------------------------------------------------------------

    def process(self, raw_text: str) -> str:
        """Apply the active profile's hotwords (fuzzy, engine-agnostic
        fallback -- see module docstring) then its correction rules to
        `raw_text`, in that order: hotwords approximate a recognition-level
        fix, corrections are the broader text-level pass and should see
        already hotword-corrected text. Identity passthrough if there is no
        active profile, or if anything about loading it is broken (never
        raises)."""
        profile = self._load()
        if profile is None:
            return raw_text
        text = apply_hotword_fuzzy_correction(raw_text, profile.hotwords)
        return apply_corrections(text, profile.corrections)
