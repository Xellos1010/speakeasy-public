"""Native host window for the SpeakEasy voice-training WIZARD: a second,
independent WKWebView displaying speakeasy/gui/assets/training_wizard.html,
wired to real backend data/actions via the SAME JS<->Python bridge dispatch
registry as the Home window (speakeasy/gui/bridge.py) -- but through its
OWN WKUserContentController script message handler ("wizardBridge",
distinct from the Home window's "bridge" handler name in
speakeasy/gui/window.py) so the two windows never share native message-
handler state.

This mirrors speakeasy/gui/window.py::GuiWindow's pattern exactly: lazy
AppKit/WebKit/Foundation imports INSIDE methods only (never at module
level -- this is why bare `pytest tests/` collects cleanly on Linux, same
as window.py), a module-level singleton via get_or_create(ctx), and
push()/_call_js() for real-time events (wizard_prompt, wizard_qc_failure,
wizard_review, wizard_summary, wizard_level -- see
speakeasy/gui/handlers/training_wizard.py and speakeasy/__main__.py's
_begin_training_wizard for what fires each one).

Protocol (JS side): identical callBridge/__bridgeResponse/__bridgePush
helper as app.html's foundation script (see window.py's module docstring),
copied verbatim into training_wizard.html so the wizard page can be opened
standalone (e.g. for design review, where it fails fast with
"no native bridge present" instead of hanging).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from speakeasy.gui import bridge
from speakeasy.gui.window import GuiContext
# Import handlers so their @register(...) decorators populate
# bridge.ACTIONS -- shared registry with the Home window; handlers/__init__
# already imports handlers/training_wizard.py alongside the others.
from speakeasy.gui import handlers  # noqa: F401

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class WizardWindow:
    def __init__(self, ctx: GuiContext):
        self.ctx = ctx
        self._window = None
        self._webview = None
        self._handler = None

    def _ensure_built(self):
        if self._window is not None:
            return
        import AppKit
        import WebKit
        import Foundation

        ctx = self.ctx
        window_self = self

        class _WizardBridgeHandler(AppKit.NSObject):
            def userContentController_didReceiveScriptMessage_(self, _controller, message):
                try:
                    body = message.body()
                    # PyObjC hands back an NSDictionary; convert to plain
                    # dict/JSON the same way window.py's handler does.
                    req = json.loads(Foundation.NSJSONSerialization.dataWithJSONObject_options_error_(
                        body, 0, None)[0].bytes().tobytes().decode("utf-8"))
                except Exception:
                    return
                req_id = req.get("id")
                action = req.get("action")
                payload = req.get("payload") or {}
                response = bridge.dispatch(action, payload, ctx)
                window_self._call_js("__bridgeResponse", req_id, response)

        self._handler = _WizardBridgeHandler.alloc().init()

        config = WebKit.WKWebViewConfiguration.alloc().init()
        ucc = config.userContentController()
        ucc.addScriptMessageHandler_name_(self._handler, "wizardBridge")

        # ~600 x 720 pt per the approved Gate -1 spec (surface: floating
        # panel, resizable, non-fullscreen).
        rect = Foundation.NSMakeRect(160, 120, 600, 720)
        webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(rect, config)

        html_path = _ASSETS_DIR / "training_wizard.html"
        file_url = Foundation.NSURL.fileURLWithPath_(str(html_path))
        webview.loadFileURL_allowingReadAccessToURL_(file_url, Foundation.NSURL.fileURLWithPath_(str(_ASSETS_DIR)))

        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable |
                 AppKit.NSWindowStyleMaskResizable | AppKit.NSWindowStyleMaskMiniaturizable)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False)
        window.setContentView_(webview)
        window.setTitle_("Train your voice")
        window.setMinSize_(Foundation.NSMakeSize(420, 520))
        window.center()
        # Same rationale as GuiWindow (speakeasy/gui/window.py): a titled/
        # closable NSWindow defaults to releasedWhenClosed=YES, which would
        # deallocate this singleton the moment the user clicks the red
        # close button -- the next show() would then call
        # makeKeyAndOrderFront_ on a freed object. False makes "close"
        # hide it instead, matching the one-singleton-reused lifecycle
        # below (get_or_create).
        window.setReleasedWhenClosed_(False)

        self._window = window
        self._webview = webview

    def show(self):
        """Build (if needed) and raise the wizard window. Deliberately
        does NOT catch exceptions here -- callers that need a graceful
        fallback (speakeasy/gui/wizard_start.py::open_wizard_or_fallback,
        used by every real entry point that opens this window) wrap this
        call themselves and fall back to the existing overlay-based
        training flow on failure. Swallowing errors in show() itself would
        hide a real failure (no WebKit available, a missing/corrupt asset,
        ...) from that fallback path, defeating the whole point of it."""
        import AppKit
        self._ensure_built()
        self._window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def _call_js(self, fn_name: str, *args) -> None:
        if self._webview is None:
            return
        # Must run on the main thread -- see window.py's identical comment;
        # push() here is called from the training flow's own callbacks,
        # which may fire from a background thread (e.g. the level-push
        # timer in handlers/training_wizard.py).
        from speakeasy.overlay import run_on_main_thread
        webview = self._webview
        args_json = ", ".join(json.dumps(a) for a in args)
        js = f"if (window.{fn_name}) {{ window.{fn_name}({args_json}); }}"
        run_on_main_thread(lambda: webview.evaluateJavaScript_completionHandler_(js, None))

    def push(self, event: str, payload: dict) -> None:
        """Push a real-time event to the wizard page's JS if the window is
        currently open. No-op otherwise -- never builds/shows the window
        just to push to it (mirrors GuiWindow.push exactly)."""
        if self._window is None:
            return
        self._call_js("__bridgePush", event, payload)


_instance: Optional[WizardWindow] = None


def get_or_create(ctx: GuiContext) -> WizardWindow:
    global _instance
    if _instance is None:
        _instance = WizardWindow(ctx)
    return _instance
