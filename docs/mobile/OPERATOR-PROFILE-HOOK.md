# Operator-profile hook (SpeakEasy -> R-30 operator profile)

Source of truth for the operator: `~/.speakeasy/operator-profiles/<id>.json`
(`id` from `OPERATOR_PROFILE`, default `default`; override the directory with `OPERATOR_PROFILES_DIR`;
both can also be set in the deployment profile, docs/PROFILE.md).
SpeakEasy NEVER edits that file.

`speakeasy profile operator-overlay [--beside]` (and the server, after every profile change)
writes `~/.speakeasy/profile/operator-overlay/<id>.speakeasy-overlay.json` (+ a `.v<N>.json`
snapshot from the CLI). With `--beside` it also writes `<id>.speakeasy-overlay.json` next to the
operator profile. The overlay holds only keys absent from the operator file; operator-authored
keys it would have set are listed in `skipped_operator_authored_keys`. Consumers combine them with
`speakeasy.mobile.operator_overlay.merged_view(base, overlay)` (operator values always win).

| Overlay key | Derived from (portable profile) |
|---|---|
| `input.voice_first` = true | SpeakEasy surfaces are voice-first. Does not replace `input.primary` (mouse) for other surfaces. |
| `input.voice.preferred_dictation_mode` | most-used mode across dictation + `mode_choice` events |
| `input.voice.language` | most-used `lang` |
| `input.voice.devices` | paired devices / devices seen in events |
| `vocabulary.spoken_terms` | vocab learned from corrections + `vocab_add` events (top 100) |
| `vocabulary.speech_corrections` | active correction rules `{heard, meant}` (top 50) |
| `pace.speech_words_per_minute` | words / speech seconds across dictations |
| `pace.dictations_total` | dictation count |
| `ui_preferences` | `preference` events, last writer wins |

Schema: `operator-profile-overlay/1` with `id`, `overlay_version` (= portable profile version),
`speakeasy_profile_digest`, `base_path`, `base_sha256`, `overlay`, `skipped_operator_authored_keys`.
