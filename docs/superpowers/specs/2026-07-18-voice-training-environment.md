# speakeasy — voice-training environment (design)

Date: 2026-07-18 · Status: proposed · Initiative: INI-SPEAKEASY · Work order: TSK-SPK-TRAIN-SPEC
Depends on: `docs/superpowers/specs/2026-07-18-speakeasy-design.md`

## Operator intent (verbatim)

> "it should be able to train on my voice and needs to have a training environment which is
> reliable to build my profile and ensure accuracy and speed — speed and accuracy are highly
> important."

Two hard constraints follow directly: (1) **reliability** of the training environment itself
(resumable, QC'd, deterministic) and (2) a **profile must never be allowed to make things worse** —
it activates only if it provably improves accuracy without meaningfully hurting speed.

---

## 1. Research findings

### 1.1 sherpa-onnx hotwords / contextual biasing for Parakeet-TDT — VERDICT: supported, but conditionally

**Supported.** sherpa-onnx implements hotwords (contextual biasing) via an Aho-Corasick automaton
over token sequences ([Hotwords docs](https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html)).
Historically this was transducer-only and NeMo/Parakeet TDT models were explicitly excluded
([issue #2541](https://github.com/k2-fsa/sherpa-onnx/issues/2541), [issue #2753](https://github.com/k2-fsa/sherpa-onnx/issues/2753)).
That gap was closed by [PR #3077](https://github.com/k2-fsa/sherpa-onnx/pull/3077) ("Add modified
beam search and hotwords support for NeMo transducer models"), merged into master 2026-02-05 and
shipped starting **sherpa-onnx v1.12.24** (current release as of this research: v1.13.4,
2026-07-07). It adds `OfflineTransducerModifiedBeamSearchNeMoDecoder`, wired to the existing
`ContextGraph` infrastructure, with TDT frame-offset/duration handling.

**Exact API, `OfflineRecognizer.from_transducer(...)`:**

| Param | Value | Notes |
|---|---|---|
| `decoding_method` | `"modified_beam_search"` | **Required.** Default `"greedy_search"` does not support hotwords at all. |
| `hotwords_file` | path | One hotword phrase per line; optional per-line score override via colon suffix, e.g. `SPEECH RECOGNITION :2.5` |
| `hotwords_score` | float | Global default boost per matched token; PR default `1.5` |
| `modeling_unit` | `"bpe"` | Parakeet-TDT uses a SentencePiece BPE vocabulary — English-only deployments use plain `bpe` (not `cjkchar`/`cjkchar+bpe`) |
| `bpe_vocab` | path | Required alongside `modeling_unit=bpe`; extracted from the model's tokenizer via the utility script added in PR #3077 |

**Action required:** `pyproject.toml` currently pins `sherpa-onnx>=1.10` (`speakeasy/pyproject.toml`),
which predates the Feb 2026 merge. Must bump to `>=1.12.24` (pin `>=1.13.4` to track the version
actually tested) before any hotwords code lands.

**Critical caveat — do not default to it.** [Issue #3267](https://github.com/k2-fsa/sherpa-onnx/issues/3267)
(open, unresolved as of this research) reports `modified_beam_search` with NeMo TDT/Parakeet
hallucinating (emitting stray "Yeah.") or returning empty text on ~20% of utterances, while
`greedy_search` (today's `speakeasy/engine/parakeet.py` behavior) is reliable. The reporter's own
mitigation — "default to greedy_search, only enable modified_beam_search when the user explicitly
opts in to hotword boosting" — is exactly what this spec adopts (§3, §5). Given the operator's
explicit speed/accuracy/reliability priorities, hotwords are treated as an **experimental, gated,
opt-in decoding path that must clear the same benchmark-harness regression gate as any other
profile change** (§5) — never a default-on feature and never a code path that touches the
raw/no-profile fallback.

### 1.2 Personalization levers, ranked by (accuracy gain × feasibility on Intel i9 CPU-only)

1. **Per-user text post-processing (learned corrections, casing, vocabulary substitutions).**
   Highest feasibility (pure string/regex ops on the existing transcript pipeline, zero inference
   cost, sub-millisecond), and directly closes the gap for the operator's own recurring
   misrecognitions (proper nouns, jargon) captured from `~/.speakeasy/history.db` corrections.
   Chosen as the **primary** lever (§ profile artifact).
2. **Engine/decoding parameter selection via per-voice benchmark.** Near-zero marginal cost (the
   benchmark harness in §6 already exists for gating) — running it against both `parakeet` and
   `whisper` fallback, and against greedy vs. modified_beam_search once hotwords are evaluated,
   turns "which config wins for this voice" into a measured decision instead of a guess. Chosen as
   the **second** lever.
3. **Contextual biasing / hotwords (§1.1).** Real accuracy upside for names/jargon the base model
   has never seen, but gated by the v1.12.24+ dependency bump, the BPE-vocab extraction step, and
   the open reliability bug — feasibility is *conditional*, not free. Chosen as the **third** lever,
   shipped behind the regression gate, never replacing greedy_search as the default.
4. VAD/endpointing tuning — speakeasy currently has no VAD at all (`speakeasy/audio.py` records
   continuously between hotkey toggles; `speakeasy/app.py` gates only on `min_samples`). Worth
   revisiting for a future streaming mode, but it is a latency/UX lever, not an accuracy-of-*this
   voice* lever, so it is **out of scope** for the training environment itself (noted as a
   non-goal, §2).
5. **Whisper LoRA fine-tuning — documented and deferred.** Published LoRA fine-tunes of Whisper
   report large relative gains (e.g., CER 49.5%→11.1% on a 12-hour corpus) but training is reported
   on GPUs (Whisper-large-v2, 3 epochs, 12h Common Voice subset ≈ 6–8h wall-clock on an 8 GB GPU).
   No CPU-only training benchmarks were found; CPU training would be materially slower still, and
   speakeasy's primary engine (Parakeet-TDT via sherpa-onnx int8 ONNX graph) has no supported
   fine-tuning path in sherpa-onnx at all — only whisper-onnx (the fallback engine) could even
   accept a fine-tuned checkpoint, and only after re-exporting to ONNX. Given the CPU-only,
   Parakeet-primary constraint and the 10–20+ minutes of the operator's own recording time the
   corpus in §3 already asks for, LoRA fine-tuning is **out of scope for this design** — revisit
   only if levers 1–3 plateau below the WER target.

### 1.3 WER/CER tooling

**[`jiwer`](https://pypi.org/project/jiwer/)** (jitsi/jiwer, PyPI): `pip install jiwer`, Python
≥3.8, depends on `click` + `rapidfuzz`. Computes WER, MER, WIL, WIP, and CER via edit distance;
`from jiwer import wer, cer`. Chosen as the benchmark harness's scoring dependency (§6) — no
in-house edit-distance code needed.

### 1.4 Prompt corpus source

**Harvard sentences** (IEEE 297-1969, "IEEE Recommended Practice for Speech Quality Measurements",
DOI 10.1109/IEEESTD.1969.7405210): 72 lists of 10 phonetically balanced sentences, public domain,
originally developed at Harvard's Psycho-Acoustic Laboratory. A plaintext reproduction is available
at the [Open Speech Repository](https://www.cs.columbia.edu/~hgs/audio/harvard.html) and as a
[raw gist](https://gist.github.com/jonhassall/e9254e50a8a9acb7bbfe916dae59a4cf). Used as prompt
set (a) in §3 — vendor once into `speakeasy/training/corpora/harvard_sentences.txt`, no network
dependency at record time.

---

## 2. Goals / non-goals

**Goals**
- Build a per-operator voice profile entirely from local data — no audio or transcript ever
  leaves the Mac; no network calls in the training or benchmark path.
- English only (matches the existing Parakeet/Whisper English-only pipeline).
- Never regress the existing baseline: p50 transcription latency (~230 ms today, per current
  Parakeet-TDT int8 CPU numbers) must not regress more than 10% with a profile active.
- A profile activates **only if measured to help** (WER improves, latency within budget) — see
  §6 regression gate. Reliability of the *training environment* (resumable sessions, audio QC,
  deterministic manifests) is as important as the accuracy gain itself, per operator intent.

**Non-goals**
- No cloud/remote inference, no telemetry, no data sharing across machines or accounts.
- No non-English support.
- No VAD/endpointing redesign (tracked separately — current continuous-recording model is
  unaffected by this work).
- No Whisper LoRA fine-tuning (§1.2 item 5) — deferred, not attempted in TSK-SPK-TRAIN-IMPL.
- No change to the existing rewrite-mode (LLM) pipeline — training only affects the STT engine
  stage and its immediate post-processing.

---

## 3. Training environment UX

Menu-bar item **"Train my voice…"** (added to `speakeasy/app.py`'s `rumps.App` menu, alongside the
existing `Mode` submenu) opens a guided recording flow, driven from the terminal or a minimal
overlay window (reuses `speakeasy/overlay.py`'s `OverlayPanel` machinery) — implementation detail
deferred to TSK-SPK-TRAIN-IMPL, but the flow contract is fixed here:

1. **Session start.** User picks a target session length (quick: 20 prompts / full: 80 prompts) or
   resumes an incomplete session. A session manifest is created immediately (§4) so a crash or quit
   mid-session loses at most the current prompt, not the whole session.
2. **Prompt sets**, drawn in a fixed, seeded-random interleave so short quick-sessions still sample
   all three sets:
   - **(a) Phonetically balanced** — Harvard sentences (§1.4), vendored locally, no PII.
   - **(b) Personal/domain vocabulary** — mined from `~/.speakeasy/history.db.raw` transcripts
     (proper nouns, capitalized multi-word terms, and terms appearing in `rewritten` but not `raw`
     as a proxy for "the model got this wrong before") plus a user-supplied plain-text term list
     (`~/.speakeasy/training/vocab.txt`). Terms are wrapped into short carrier sentences
     ("The word is ___.", "Please schedule a call with ___.") so they're spoken in natural
     phonetic context rather than read as bare words.
   - **(c) Correction pairs** — rows from `history.db` where `rewritten != raw` under `clean` mode
     are the closest available signal for "the operator corrected/rewrote this"; the *raw* text is
     replayed as the prompt (user re-reads what they originally said) so the new recording can be
     compared against both the old `raw` transcript-that-was-wrong and the desired final form.
3. **Per-prompt recording.** Prompt text shown, user hits the hotkey to start/stop (reuses
   `speakeasy/audio.py::Recorder`), waveform level shown live (`Recorder.level`).
4. **Per-clip QC gate**, run immediately after each recording, before advancing:
   - **Clipping**: reject if `max(abs(samples)) >= 0.99` (int16 full-scale float32 proxy).
   - **Silence**: reject if voiced duration (simple energy-threshold VAD, no model needed) is
     `< 40%` of clip duration.
   - **Min RMS / SNR proxy**: reject if RMS `< -45 dBFS`, or if a leading/trailing 200 ms noise-floor
     estimate implies SNR `< 15 dB`.
   - On failure: prompt is re-shown immediately, up to 3 attempts, then flagged `low_quality` and
     skipped (recorded in the manifest so the benchmark step can exclude it, not silently drop it).
5. **Held-out split, fixed at record time.** Each accepted clip is assigned to `train` or `eval`
   at the moment it's recorded (deterministic hash of prompt id + session id, ~80/20), not after
   the fact — guarantees the eval split can never leak into whatever the profile builder uses,
   even if the builder changes later.
6. **Resumability.** The session manifest (§4) is the single source of truth for "what's done";
   re-invoking "Train my voice…" with an incomplete manifest resumes at the first un-recorded or
   `low_quality`-flagged prompt. No in-memory-only state.
7. **Session end.** Manifest marked `complete`; user is shown a summary (# clips, # rejected,
   est. minutes) and prompted to run the benchmark (§6) — or it runs automatically as the last
   step of the session.

---

## 4. Corpus layout

```
~/.speakeasy/training/
  corpora/
    harvard_sentences.txt          # vendored, public domain, 720 lines
  vocab.txt                        # user-supplied terms, one per line (optional)
  sessions/
    <session_id>/                  # session_id = ISO8601 timestamp, e.g. 2026-07-18T14-02-11
      manifest.json                # see schema below
      clips/
        <prompt_id>.wav             # 16 kHz mono int16 PCM, matches tests/assets/sample.wav convention
```

`manifest.json` schema (deterministic, append-only during a session):

```json
{
  "session_id": "2026-07-18T14-02-11",
  "created_ts": 1799000000.0,
  "status": "in_progress | complete",
  "speakeasy_version": "<git sha or package version>",
  "prompts": [
    {
      "prompt_id": "harvard-0007",
      "source": "harvard | vocab | correction",
      "text": "The birch canoe slid on the smooth planks.",
      "split": "train | eval",
      "clip_path": "clips/harvard-0007.wav",
      "status": "accepted | low_quality | pending",
      "qc": {"clipping": false, "silence_ratio": 0.81, "rms_dbfs": -22.4, "snr_db": 27.1},
      "attempts": 1,
      "recorded_ts": 1799000042.1
    }
  ]
}
```

---

## 5. Profile artifact

Versioned, append-only: `~/.speakeasy/profile/profile-v<N>.json`. A pointer file
`~/.speakeasy/profile/active.json` (`{"active_version": N}`) is the only thing the runtime engine
reads — the profile store itself is otherwise immutable once written, so a bad build never
corrupts a good one and rollback is "flip the pointer back."

```json
{
  "version": 3,
  "built_ts": 1799000500.0,
  "built_from_session": "2026-07-18T14-02-11",
  "provenance": {
    "n_train_clips": 64, "n_eval_clips": 16,
    "sherpa_onnx_version": "1.13.4",
    "engine": "parakeet"
  },
  "hotwords": [
    {"phrase": "Karina Legal Workspace", "score": 2.5},
    {"phrase": "sherpa onnx", "score": 2.0}
  ],
  "corrections": [
    {"pattern": "\\bpair a keet\\b", "replacement": "Parakeet", "kind": "regex", "case_sensitive": false},
    {"pattern": "gonna", "replacement": "going to", "kind": "literal"}
  ],
  "decoding": {
    "decoding_method": "greedy_search",
    "hotwords_enabled": false
  },
  "metrics": {
    "baseline_wer": 0.041, "profile_wer": 0.028,
    "baseline_p50_ms": 231, "profile_p50_ms": 238,
    "baseline_p95_ms": 310, "profile_p95_ms": 322
  }
}
```

Notes:
- `decoding.hotwords_enabled` defaults `false` even when a `hotwords` list exists — activation is
  a separate, explicit gate result (§6), because `modified_beam_search` is the reliability-risky
  path (§1.1).
- `corrections` are applied as a post-processing pass on the engine's raw output, ordered as
  listed, before the existing mode/rewrite pipeline (`speakeasy/app.py::Controller._process`) sees
  the text — this is the "primary lever" from §1.2 and requires no sherpa-onnx version bump.

---

## 6. Benchmark harness

New module `speakeasy/training/benchmark.py` (name indicative, finalized in implementation):

- **Inputs**: a profile candidate (or "baseline" = no profile) + the `eval` split of one or more
  session manifests.
- **Metrics**: `jiwer.wer` and `jiwer.cer` (§1.3) over the eval split, comparing engine output
  (with corrections/hotwords applied, if candidate) against the recorded prompt `text`; p50/p95
  wall-clock transcription latency over the same clips, measured the same way
  `tests/test_engine_integration.py` already measures correctness (load wav → `engine.transcribe`).
- **Report**: written to `evidence/training-report-<date>.md` (matches the existing evidence
  conventions in `evidence/`), containing baseline vs. candidate WER/CER, p50/p95 latency,
  the regression-gate verdict, and the profile version under test.
- **Regression gate** (auto-activation rule — this is the mechanism that makes "reliable" concrete):
  a candidate profile is written to `active.json` **only if all of**:
  1. `profile_wer < baseline_wer` (strict improvement, not tie)
  2. `profile_p50_ms <= baseline_p50_ms * 1.10` (the operator's explicit ≤10% budget)
  3. `profile_p95_ms <= baseline_p95_ms * 1.15` (looser tail budget — avoids one slow outlier
     blocking an otherwise-good profile, while still catching real regressions)
  4. If `decoding.hotwords_enabled=true` in the candidate: additionally require zero empty-string
     or single-word-hallucination outputs on the eval split (direct guard against issue #3267,
     §1.1) — any occurrence forces `hotwords_enabled=false` in the written profile even if WER
     otherwise improved.
  - Gate failure ⇒ profile is still written to `profile-v<N>.json` (for inspection/debugging) but
    `active.json` is left untouched; report states which condition(s) failed.

---

## 7. Continuous learning loop

- Every dictation already lands in `history.db` (`speakeasy/history.py`) with `raw` and, for
  rewrite modes, `rewritten`. No schema change required for read access; this design treats
  `rewritten != raw` rows as an implicit correction signal (§3, prompt set (c)).
- A lightweight, explicit "that was wrong, it should have been ___" capture is a natural follow-on
  (e.g., a menu-bar "Fix last dictation" action that writes directly into
  `~/.speakeasy/training/vocab.txt` or a `corrections_pending.jsonl` file) but is **not required**
  for TSK-SPK-TRAIN-IMPL v1 — the existing `history.db` mining in §3(b)/(c) is sufficient to bootstrap
  profile v1 and v2; explicit correction capture is a fast-follow noted in Open Questions (§10).
- Recommended cadence: user re-runs "Train my voice…" (short top-up session) whenever they've
  noticed repeat misrecognitions; each run produces a new profile version and re-runs the full
  gate — profiles are never blindly incremented.

---

## 8. Acceptance criteria (measurable)

1. A training session can be started, interrupted (process killed) after any prompt, and resumed
   from `manifest.json` with zero re-recording of already-`accepted` clips.
2. Every accepted clip in every manifest has `qc.clipping=false`, `qc.silence_ratio>=0.4`, and
   `qc.rms_dbfs>=-45` — i.e., the QC gate in §3.4 is enforced, not advisory.
3. `eval`/`train` split assignment is reproducible: re-running the split function on the same
   manifest (same prompt ids, same session id) yields an identical assignment (deterministic hash,
   §3.5) — verified by a unit test.
4. `benchmark.py` run on a fixture eval set with a synthetic "no-op" profile (identity corrections,
   no hotwords) reports `profile_wer == baseline_wer` and the gate refuses activation (guards
   against a gate that always says yes).
5. `benchmark.py` run on a fixture eval set with a profile containing a correction rule that fixes
   a known-wrong word reports `profile_wer < baseline_wer` and, given latency within budget, the
   gate activates it (guards against a gate that always says no).
6. A profile with `hotwords_enabled=true` that produces even one empty-string output on its eval
   split is force-downgraded to `hotwords_enabled=false` before being written as active — verified
   by a unit test with a stubbed engine.
7. `active.json`'s pointed-to profile is the only profile the runtime engine consults; rolling
   `active_version` back to `N-1` fully reverts behavior (no cached state elsewhere).
8. End-to-end: baseline p50 latency on the existing `tests/assets/sample.wav`-style fixture,
   measured before and after a profile is active, differs by `<=10%`.

---

## 9. Task decomposition — TSK-SPK-TRAIN-IMPL

Bounded, file-scoped tasks; ≥2 are independent and should be parallelized per the parallel-first
default (noted where a real data dependency forces sequencing).

| # | Task | File scope | Depends on |
|---|---|---|---|
| T1 | Vendor Harvard sentences corpus + vocab-mining utility (proper nouns / capitalized terms / `raw`-vs-`rewritten` diff from `history.db`) | `speakeasy/training/corpora/harvard_sentences.txt`, `speakeasy/training/vocab_mining.py`, `tests/training/test_vocab_mining.py` | none |
| T2 | Audio QC gate (clipping/silence/RMS/SNR proxy) as pure functions over `np.ndarray` | `speakeasy/training/qc.py`, `tests/training/test_qc.py` | none |
| T3 | Session manifest model + resumable session controller (deterministic train/eval split, prompt sequencing, QC integration) | `speakeasy/training/session.py`, `tests/training/test_session.py` | T1, T2 |
| T4 | Menu-bar "Train my voice…" flow wiring into `speakeasy/app.py`/`speakeasy/__main__.py` (reuses `Recorder`, `OverlayPanel`) | `speakeasy/__main__.py`, `speakeasy/app.py` (additive menu item + session-driving controller), `tests/test_app.py` (additive cases) | T3 |
| T5 | Profile artifact schema + builder (corrections from correction-pair prompts, hotwords list from vocab mining, versioned write, `active.json` pointer) | `speakeasy/training/profile.py`, `tests/training/test_profile.py` | T1, T3 |
| T6 | sherpa-onnx dependency bump + hotwords-capable engine variant (`decoding_method`, `hotwords_file`/`hotwords_score`, `bpe_vocab` extraction), gated behind `decoding.hotwords_enabled` | `pyproject.toml` (bump `sherpa-onnx>=1.13.4`), `speakeasy/engine/parakeet.py` (additive param path, default unchanged), `tests/test_engine_integration.py` (additive hotwords case, `@pytest.mark.integration`) | none (parallel with T1–T5) |
| T7 | Benchmark harness: jiwer-based WER/CER + p50/p95 latency, regression-gate logic, evidence report writer | `speakeasy/training/benchmark.py`, `pyproject.toml` (add `jiwer` dependency), `tests/training/test_benchmark.py` | T3, T5, T6 |
| T8 | Correction-rule post-processing pass wired into the transcription path ahead of mode/rewrite | `speakeasy/engine/postprocess.py`, `speakeasy/app.py` (`Controller._process` applies active profile's corrections before mode dispatch), `tests/test_app.py` (additive), `tests/training/test_postprocess.py` | T5 |

Sequencing: T1/T2/T6 fully independent, fan out first. T3 depends on T1+T2. T4 depends on T3.
T5 depends on T1+T3. T7 (the gate) depends on T3+T5+T6 — it is the integration point and should
be the last task closed, since it's what proves T5/T6's outputs are actually safe to activate.
T8 depends only on T5 and can run in parallel with T4/T6/T7.

---

## 10. Open questions

1. **BPE-vocab extraction for the bundled Parakeet-TDT int8 model** — PR #3077 added a utility
   script for this, but it needs to be run against `sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`
   specifically (the exact model in `speakeasy/engine/download.py`) to confirm it's compatible
   with that export; not verified in this research pass.
2. **Correction-pair signal quality** — `rewritten != raw` under `clean` mode is a proxy for "this
   was wrong," but `clean` mode also fixes casing/punctuation/filler words unrelated to STT
   accuracy; T5's builder needs a heuristic (or manual review step) to avoid mining false corrections.
3. **Explicit "fix last dictation" capture** (§7) — deferred out of v1; worth a follow-up work
   order once profile v1/v2 are in use and the operator has a feel for what's still wrong.
4. Whether the modified_beam_search hallucination bug (#3267) gets fixed upstream before
   TSK-SPK-TRAIN-IMPL starts — if fixed, T6's gating logic in T7 condition 4 (§6) should be
   revisited, not removed (defense in depth is still warranted for a reliability-critical path).
