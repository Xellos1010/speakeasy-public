"""Bridge handlers for the Home screen -- real data, no sample/mock values.

Actions:
  home_get_state -> {today_count, today_words, engine_label, engine_detail,
                      profile_label, profile_detail, profile_active,
                      recent: [{text, relative_time, words}]}
  start_dictation -> triggers the same toggle the hotkey uses
"""
import time

from speakeasy.gui.bridge import register
from speakeasy.sysinfo import ENGINE_INFO

_APP_VERSION = "0.1.0"  # setup.py CFBundleShortVersionString -- single source, kept in sync manually


def _relative_time(ts: float, now: float) -> str:
    delta = max(0, now - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hr ago"
    return f"{int(delta // 86400)} d ago"


def _word_count(text: str) -> int:
    return len((text or "").split())


@register("home_get_state")
def home_get_state(_payload, ctx):
    now = time.time()
    today_start = now - (now % 86400)  # local-agnostic UTC-day bucket is fine for a glance stat

    rows = ctx.history.recent(200)  # enough to cover "today" without scanning the whole table
    today_rows = [r for r in rows if r["ts"] >= today_start and r["status"] != "discarded"]
    today_words = sum(_word_count(r["rewritten"] or r["raw"]) for r in today_rows)

    engine_name = ctx.settings.engine
    engine_info = ENGINE_INFO.get(engine_name, {"label": engine_name, "detail": ""})

    profile = ctx.profile_store.load_active()
    if profile is not None:
        days = max(0, int((now - profile.built_ts) // 86400))
        age = "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")
        metrics = profile.metrics or {}
        wer_note = ""
        if metrics.get("baseline_wer") is not None and metrics.get("profile_wer") is not None:
            wer_note = f"{metrics['baseline_wer']*100:.1f}% -> {metrics['profile_wer']*100:.1f}% WER"
        profile_state = {
            "active": True,
            "label": f"v{profile.version} - Trained",
            "detail": f"Last refined {age}" + (f" - {wer_note}" if wer_note else ""),
        }
    else:
        profile_state = {"active": False, "label": "Not trained yet",
                          "detail": "Train my voice... to build a profile"}

    recent = [
        {"text": (r["rewritten"] or r["raw"])[:140],
         "relative_time": _relative_time(r["ts"], now),
         "words": _word_count(r["rewritten"] or r["raw"])}
        for r in rows[:3] if r["status"] != "discarded"
    ]

    return {
        "today_count": len(today_rows),
        "today_words": today_words,
        "engine_label": engine_info["label"],
        "engine_detail": f"Running on-device - v{_APP_VERSION}",
        "profile": profile_state,
        "recent": recent,
    }


@register("start_dictation")
def start_dictation(_payload, ctx):
    ctx.controller.on_tap_toggle()
    return {"state": ctx.controller.state}
