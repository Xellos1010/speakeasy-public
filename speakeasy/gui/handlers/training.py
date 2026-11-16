"""Bridge handlers for the Training & Profile screen -- real data, no
sample/mock values (speakeasy/training/profile.py::ProfileStore is the real
backend).

Actions:
  training_get_state -> {has_active, active, versions, resumable}
  training_start      -> payload {size: "quick"|"full"}; starts the real
                          live training session (recording continues in the
                          overlay HUD, not this window).
  training_resume     -> resumes an in-progress live training session.
  training_restore_version -> payload {version: int}; flips the active
                          pointer (ProfileStore.set_active). Raises
                          FileNotFoundError for a nonexistent version --
                          bridge.dispatch already turns that into
                          {"ok": false, "error": ...} for the JS side.
"""
import time

from speakeasy.gui.bridge import register


def _days_ago(built_ts: float, now: float = None) -> int:
    """Pure, injectable-`now` helper so this is unit testable without
    mocking time.time(). Never negative (a profile built "in the future"
    relative to a slightly skewed clock still reads as 0 days ago)."""
    if now is None:
        now = time.time()
    return max(0, int((now - built_ts) // 86400))


def _profile_summary(profile, now: float) -> dict:
    """Shared shape for both the active profile and each version-history
    row -- {version, built_ts, days_ago, n_train_clips, n_eval_clips,
    engine, hotwords, corrections, metrics}."""
    return {
        "version": profile.version,
        "built_ts": profile.built_ts,
        "days_ago": _days_ago(profile.built_ts, now),
        "n_train_clips": profile.provenance.n_train_clips,
        "n_eval_clips": profile.provenance.n_eval_clips,
        "engine": profile.provenance.engine,
        "hotwords": [h.to_dict() for h in profile.hotwords],
        "corrections": [c.to_dict() for c in profile.corrections],
        "metrics": profile.metrics,
    }


@register("training_get_state")
def training_get_state(_payload, ctx):
    now = time.time()
    store = ctx.profile_store

    active_profile = store.load_active()
    active = _profile_summary(active_profile, now) if active_profile is not None else None

    active_version = store.active_version()
    versions = []
    for version in reversed(store.list_versions()):  # newest first for display
        profile = store.read(version)
        summary = _profile_summary(profile, now)
        summary["is_active"] = version == active_version
        versions.append(summary)

    return {
        "has_active": active is not None,
        "active": active,
        "versions": versions,
        "resumable": bool(ctx.training_resumable()),
    }


@register("training_start")
def training_start(payload, ctx):
    size = payload.get("size", "quick")
    return ctx.start_training(size)


@register("training_resume")
def training_resume(_payload, ctx):
    return ctx.resume_training()


@register("training_restore_version")
def training_restore_version(payload, ctx):
    version = payload["version"]
    ctx.profile_store.set_active(version)
    return {"active_version": version}
