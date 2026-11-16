"""Bridge handlers for the History screen -- real data, no sample/mock values.

Actions:
  history_get_entries -> payload {search: str, mode: str|"all", status: str|"all",
                                   limit: int}
                         returns {entries: [{id, text, relative_time, target_app,
                                              mode, status, has_diff, raw, rewritten}, ...],
                                  total_matching: int}
  history_copy_text -> payload {text: str} -- copies text to the system clipboard.

History (speakeasy/history.py) exposes only recent(limit) -- there is no
search/filter query on the table today. We fetch a generous batch (500 rows)
and do search/mode/status filtering here in Python rather than in JS: the
bridge response is already a small, screen-shaped JSON payload (same pattern
as home.py's home_get_state), so filtering server-side keeps the JS side a
pure renderer with no business logic duplicated between Python and
JavaScript, and keeps the door open for History growing a real query layer
later without touching the JS at all.
"""
import time

from speakeasy.gui.bridge import register
from speakeasy.injector import _SystemPasteboard


def _relative_time(ts: float, now: float) -> str:
    delta = max(0, now - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hr ago"
    return f"{int(delta // 86400)} d ago"


@register("history_get_entries")
def history_get_entries(payload, ctx):
    search = (payload.get("search") or "").strip().lower()
    mode = payload.get("mode") or "all"
    status = payload.get("status") or "all"
    limit = payload.get("limit") or 50

    now = time.time()
    rows = ctx.history.recent(500)  # generous batch -- see module docstring

    matching = []
    for r in rows:
        if mode != "all" and r["mode"] != mode:
            continue
        if status != "all" and r["status"] != status:
            continue
        if search:
            raw_lower = (r["raw"] or "").lower()
            rewritten_lower = (r["rewritten"] or "").lower()
            if search not in raw_lower and search not in rewritten_lower:
                continue
        matching.append(r)

    total_matching = len(matching)

    entries = []
    for r in matching[:limit]:
        raw = r["raw"] or ""
        rewritten = r["rewritten"]
        final_text = rewritten if rewritten else raw
        has_diff = bool(rewritten) and rewritten != raw
        entries.append({
            "id": r["id"],
            "text": final_text,
            "relative_time": _relative_time(r["ts"], now),
            "target_app": r["target_app"],
            "mode": r["mode"],
            "status": r["status"],
            "has_diff": has_diff,
            "raw": raw,
            "rewritten": rewritten,
        })

    return {"entries": entries, "total_matching": total_matching}


@register("history_copy_text")
def history_copy_text(payload, _ctx):
    text = payload.get("text") or ""
    _SystemPasteboard().set(text)
    return {"copied": True}
