"""JS<->Python bridge dispatch for the GUI window (speakeasy/gui/window.py).

Action-registry pattern: each handler module in speakeasy/gui/handlers/
registers its own action names via @register("action_name"). This keeps
each screen's real-data wiring in its own file with zero shared mutable
state between handler modules, so parallel work on different screens never
collides on the same file.
"""
from typing import Callable

ACTIONS: dict[str, Callable] = {}


def register(name: str):
    def deco(fn):
        if name in ACTIONS:
            raise ValueError(f"duplicate bridge action registered: {name!r}")
        ACTIONS[name] = fn
        return fn
    return deco


def dispatch(name: str, payload: dict, ctx) -> dict:
    """ctx is a GuiContext (speakeasy/gui/window.py) carrying real backend
    refs (Controller, History, ProfileStore, etc.) a handler needs. Never
    raises -- an unknown action or a handler exception both come back as a
    normal {"ok": False, "error": ...} response the JS side can render,
    never a crash of the bridge itself."""
    fn = ACTIONS.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown action {name!r}"}
    try:
        return {"ok": True, "result": fn(payload, ctx)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
