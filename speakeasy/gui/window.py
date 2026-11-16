"""Native host window for the speakeasy GUI: a WKWebView displaying
speakeasy/gui/assets/app.html (the approved design mockup), wired to real
backend data/actions via a JS<->Python bridge (speakeasy/gui/bridge.py).

Protocol (JS side, shared helper defined in app.html's foundation script):
  callBridge(action, payload) -> Promise<result>
    posts {id, action, payload} to window.webkit.messageHandlers.bridge,
    Python responds by calling window.__bridgeResponse(id, response) where
    response is {ok, result} or {ok: false, error}.

Push channel (Python -> JS, no request): GuiWindow.push(event, payload)
calls window.__bridgePush(event, payload) if the window is open -- used for
real-time state (recording level, dictation status) so the Home screen
reflects the SAME live state as the menu-bar icon/overlay, not a stale copy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from speakeasy.gui import bridge
# Import handlers so their @register(...) decorators populate bridge.ACTIONS.
from speakeasy.gui import handlers  # noqa: F401


@dataclass
class GuiContext:
    """Real backend refs a bridge handler may need. Constructed once in
    speakeasy/__main__.py::run() from the objects already live there --
    never duplicated/reconstructed, so the GUI window always reflects the
    same state as the menu-bar app."""
    controller: object
    settings: object
    history: object
    profile_store: object
    base: Path
    switch_engine: Callable[[str], dict]
    detect_specs: Callable[[], object]
    start_training: Callable[[str], dict]
    resume_training: Callable[[], dict]
    training_resumable: Callable[[], bool]
    # Wizard-specific entry points (speakeasy/gui/wizard_window.py,
    # speakeasy/gui/handlers/training_wizard.py): distinct from
    # start_training/resume_training above because the wizard's own
    # welcome screen offers Quick/Full/Resume choices from INSIDE the
    # already-open wizard window, while start_training/resume_training
    # back the OLDER Training & Profile dashboard's Start/Resume buttons
    # (which, per the wizard work order's step 5, now ALSO open the
    # wizard, via the same open-wizard-or-fall-back-to-overlay path --
    # see speakeasy/__main__.py's _start_training_flow/
    # _resume_training_flow).
    start_training_wizard: Callable[[str], dict]
    resume_training_wizard: Callable[[], dict]
    # Mic-selection (TSK-SPK-MICSELECT): validates + persists
    # settings.input_device, mirroring switch_engine's shape (see
    # speakeasy/__main__.py::_set_input_device). Used by the wizard's
    # mic-check screen's device dropdown
    # (speakeasy/gui/handlers/training_wizard.py::training_wizard_mic_check_set_device).
    set_input_device: Callable[[Optional[int]], dict]


_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class GuiWindow:
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

        class _BridgeHandler(AppKit.NSObject):
            def userContentController_didReceiveScriptMessage_(self, _controller, message):
                try:
                    body = message.body()
                    # PyObjC hands back an NSDictionary; convert to plain dict/JSON.
                    req = json.loads(Foundation.NSJSONSerialization.dataWithJSONObject_options_error_(
                        body, 0, None)[0].bytes().tobytes().decode("utf-8"))
                except Exception:
                    return
                req_id = req.get("id")
                action = req.get("action")
                payload = req.get("payload") or {}
                response = bridge.dispatch(action, payload, ctx)
                window_self._call_js("__bridgeResponse", req_id, response)

        self._handler = _BridgeHandler.alloc().init()

        config = WebKit.WKWebViewConfiguration.alloc().init()
        ucc = config.userContentController()
        ucc.addScriptMessageHandler_name_(self._handler, "bridge")

        rect = Foundation.NSMakeRect(120, 120, 1200, 800)
        webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(rect, config)

        html_path = _ASSETS_DIR / "app.html"
        file_url = Foundation.NSURL.fileURLWithPath_(str(html_path))
        webview.loadFileURL_allowingReadAccessToURL_(file_url, Foundation.NSURL.fileURLWithPath_(str(_ASSETS_DIR)))

        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable |
                 AppKit.NSWindowStyleMaskResizable | AppKit.NSWindowStyleMaskMiniaturizable)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False)
        window.setContentView_(webview)
        window.setTitle_("speakeasy")
        window.setMinSize_(Foundation.NSMakeSize(880, 600))
        window.center()
        # AppKit's default for a titled/closable window is
        # releasedWhenClosed=YES: closing the window (the user clicking the
        # red button) would deallocate it, leaving self._window/_webview as
        # dangling references -- the next show() (via _ensure_built's
        # early-return-if-not-None) would call makeKeyAndOrderFront_ on a
        # freed object, and any in-flight push() would evaluate JS against
        # it. False makes "close" hide it instead, matching this class's
        # actual lifecycle (one singleton, reused across show/hide, per
        # get_or_create below).
        window.setReleasedWhenClosed_(False)

        self._window = window
        self._webview = webview

    def show(self):
        import AppKit
        self._ensure_built()
        self._window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def _call_js(self, fn_name: str, *args) -> None:
        if self._webview is None:
            return
        # WKWebView.evaluateJavaScript_completionHandler_ is an AppKit/WebKit
        # call and must run on the main thread. push() is called from
        # background threads (e.g. __main__.py's engine-swap thread and the
        # Controller's transcription thread) -- calling straight through
        # here would be a real thread-safety bug (same class of bug
        # run_on_main_thread already exists in speakeasy/overlay.py to
        # prevent for the overlay panel and menu-bar title).
        from speakeasy.overlay import run_on_main_thread
        webview = self._webview
        args_json = ", ".join(json.dumps(a) for a in args)
        js = f"if (window.{fn_name}) {{ window.{fn_name}({args_json}); }}"
        run_on_main_thread(lambda: webview.evaluateJavaScript_completionHandler_(js, None))

    def push(self, event: str, payload: dict) -> None:
        """Push a real-time event to the JS side (e.g. dictation state
        changes) if the window is currently open. No-op otherwise -- never
        builds/shows the window just to push to it."""
        if self._window is None:
            return
        self._call_js("__bridgePush", event, payload)


_instance: Optional[GuiWindow] = None


def get_or_create(ctx: GuiContext) -> GuiWindow:
    global _instance
    if _instance is None:
        _instance = GuiWindow(ctx)
    return _instance
