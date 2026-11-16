from dataclasses import dataclass

# Maps the injection outcome (app.py's `status`, also what's written to
# history.db) to a short human label for the "done" scratchpad state
# (TSK-SPK-UX-SCRATCHPAD). Unrecognized/empty status falls back to the raw
# code itself in `label()` rather than silently showing nothing.
_STATUS_LABELS = {
    "injected": "✓ Inserted",
    "clipboard_only": "📋 Copied — paste manually (secure field)",
    "rewrite_failed": "⚠ Rewrite unavailable — raw text inserted",
    "engine_failed": "⚠ Transcription failed — audio saved (menu: Retry last failed)",
    "inject_failed": "⚠ Couldn't insert — text kept in History",
    "translated": "✓ Translated & inserted",
}


@dataclass
class OverlayModel:
    state: str = "hidden"       # hidden|recording|transcribing|rewriting|done|info
    partial: str = ""
    mode_name: str = "raw"
    level: float = 0.0
    message: str = ""           # info state: shown verbatim. done state: a
                                 # status code key into _STATUS_LABELS

    def label(self) -> str:
        if self.state == "info":
            return self.message
        if self.state == "recording":
            return f"● Recording — {self.mode_name}"
        if self.state == "transcribing":
            return "… Transcribing"
        if self.state == "rewriting":
            return f"✍ Rewriting — {self.mode_name}"
        if self.state == "polishing":
            return "Polishing…"
        if self.state == "done":
            return _STATUS_LABELS.get(self.message, self.message or "Done")
        return ""

# Per-bar weight profile giving the meter a waveform silhouette (louder in
# the middle) instead of one flat block. Purely a function of the real RMS
# `level` -- no synthetic jitter, so motion always maps to actual audio
# (glanceable-dashboard-design: meaningful motion only, never decorative).
_BAR_WEIGHTS = (0.5, 0.8, 1.0, 0.8, 0.5)


def level_to_bar_heights(level: float, min_frac: float = 0.15) -> list[float]:
    """Map a Recorder.level RMS reading (observed ~0.02-0.25 for speech) to
    normalized (0..1) bar heights. `min_frac` is a visibility floor so the
    meter never reads as fully dead air while recording is active."""
    clamped = max(0.0, min(1.0, level * 4.0))
    return [max(min_frac, clamped * w) for w in _BAR_WEIGHTS]


# States that expand the panel: "recording" for the live meter, "done" for
# the post-dictation scratchpad (TSK-SPK-UX-SCRATCHPAD -- the transcribed
# text needs room to read and a Copy button, since automatic ⌘V injection
# has no way to confirm a text field was actually focused, and the system
# clipboard gets restored to its prior contents 0.6s after every injection
# attempt either way). Everything else (transcribing/rewriting/polishing/
# info) stays a collapsed single-line pill -- progressive disclosure.
_EXPANDED_STATES = {"recording", "done"}

_meter_view_cls = None
_close_target_cls = None
_copy_target_cls = None


def _meter_view_class():
    """Lazy NSView subclass -- built on first real use so importing this
    module (e.g. under pytest, no GUI session) never touches AppKit."""
    global _meter_view_cls
    if _meter_view_cls is not None:
        return _meter_view_cls
    from AppKit import NSView, NSColor, NSBezierPath
    import Foundation
    import objc

    class _MeterView(NSView):
        def initWithFrame_(self, frame):
            # Plain `super()` misresolves for ObjC-inherited init methods in
            # PyObjC (ObjCSuperWarning) -- must go through objc.super
            # explicitly or `self` silently comes back un-initialized.
            self = objc.super(_MeterView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.heights = [0.15] * len(_BAR_WEIGHTS)
            return self

        def set_heights(self, heights):
            self.heights = heights
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            bounds = self.bounds()
            n = len(self.heights)
            gap = 6.0
            bar_w = (bounds.size.width - gap * (n - 1)) / n
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.30, 0.85, 0.60, 0.95).set()
            for i, frac in enumerate(self.heights):
                bh = max(4.0, bounds.size.height * frac)
                x = i * (bar_w + gap)
                y = (bounds.size.height - bh) / 2
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    Foundation.NSMakeRect(x, y, bar_w, bh),
                    bar_w / 2, bar_w / 2).fill()

    _meter_view_cls = _MeterView
    return _meter_view_cls


def _close_target_class():
    global _close_target_cls
    if _close_target_cls is not None:
        return _close_target_cls
    from Foundation import NSObject

    class _CloseTarget(NSObject):
        def closeTapped_(self, _sender):
            self.panel.orderOut_(None)

    _close_target_cls = _CloseTarget
    return _close_target_cls


def _copy_target_class():
    global _copy_target_cls
    if _copy_target_cls is not None:
        return _copy_target_cls
    from Foundation import NSObject

    class _CopyTarget(NSObject):
        def copyTapped_(self, _sender):
            from AppKit import NSPasteboard, NSPasteboardTypeString
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(self.text or "", NSPasteboardTypeString)

    _copy_target_cls = _CopyTarget
    return _copy_target_cls


class OverlayPanel:
    COLLAPSED_W, COLLAPSED_H = 220, 40
    EXPANDED_W, EXPANDED_H = 420, 88
    _MARGIN_TOP = 60

    def __init__(self):
        self._panel = None
        self._layout_mode = "collapsed"

    def _ensure_panel(self):
        from AppKit import (NSPanel, NSScreen, NSColor, NSTextField, NSFont,
                            NSButton, NSForegroundColorAttributeName,
                            NSWindowStyleMaskNonactivatingPanel,
                            NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
                            NSFloatingWindowLevel)
        import Foundation
        import objc
        if self._panel is not None:
            return
        screen = NSScreen.mainScreen().frame()
        w, h = self.COLLAPSED_W, self.COLLAPSED_H
        rect = Foundation.NSMakeRect(
            (screen.size.width - w) / 2,
            screen.size.height - h - self._MARGIN_TOP, w, h)
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        p.setLevel_(NSFloatingWindowLevel)
        p.setOpaque_(False)
        p.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.92))
        # Draggable (move it out of the way) and clickable (close button) --
        # a deliberate trade-off vs. the old click-through HUD: this panel
        # now intercepts clicks, but only within its own small bounds.
        p.setIgnoresMouseEvents_(False)
        p.setMovableByWindowBackground_(True)
        p.setCollectionBehavior_(1 << 0)   # canJoinAllSpaces

        label = NSTextField.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 0, 0))
        label.setEditable_(False); label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setTextColor_(NSColor.whiteColor())
        label.setFont_(NSFont.systemFontOfSize_(14))
        # "done" mode's transcribed text can run to several wrapped lines
        # (the recording/transcribing states' single status line fits
        # either way) -- explicit, not relying on NSTextField's default.
        label.setUsesSingleLineMode_(False)
        label.cell().setWraps_(True)
        p.contentView().addSubview_(label)

        meter = self._meter_view_class().alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, 0, 0))
        meter.setHidden_(True)
        p.contentView().addSubview_(meter)

        close_target = self._close_target_class().alloc().init()
        close_target.panel = p
        close = NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 16, 16))
        close.setBordered_(False)
        attrs = {NSForegroundColorAttributeName:
                 NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.55)}
        close.setAttributedTitle_(
            Foundation.NSAttributedString.alloc().initWithString_attributes_(
                "✕", attrs))
        close.setTarget_(close_target)
        close.setAction_(objc.selector(close_target.closeTapped_, signature=b"v@:@"))
        p.contentView().addSubview_(close)

        copy_target = self._copy_target_class().alloc().init()
        copy_target.text = ""
        copy = NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 46, 20))
        copy.setBordered_(False)
        copy_attrs = {NSForegroundColorAttributeName:
                      NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.85)}
        copy.setAttributedTitle_(
            Foundation.NSAttributedString.alloc().initWithString_attributes_(
                "Copy", copy_attrs))
        copy.setFont_(NSFont.systemFontOfSize_(12))
        copy.setTarget_(copy_target)
        copy.setAction_(objc.selector(copy_target.copyTapped_, signature=b"v@:@"))
        copy.setHidden_(True)
        p.contentView().addSubview_(copy)

        self._panel = p
        self._label, self._meter, self._close, self._copy = label, meter, close, copy
        # keep targets alive (AppKit holds only a weak/unretained ref to
        # setTarget_, so a bare local would be GC'd before ever firing)
        self._close_target, self._copy_target = close_target, copy_target
        self._layout("collapsed")

    # Split out purely so tests could stub AppKit class construction if ever
    # needed; production path always goes through the module-level builders.
    def _meter_view_class(self):
        return _meter_view_class()

    def _close_target_class(self):
        return _close_target_class()

    def _copy_target_class(self):
        return _copy_target_class()

    def _layout(self, mode: str):
        # mode: "collapsed" | "recording" (meter) | "done" (scratchpad + Copy)
        import Foundation
        expanded = mode in _EXPANDED_STATES
        w, h = ((self.EXPANDED_W, self.EXPANDED_H) if expanded
                else (self.COLLAPSED_W, self.COLLAPSED_H))
        old = self._panel.frame()
        # Keep the top edge fixed and stay horizontally centered on resize,
        # so expanding never drifts the panel toward a screen edge.
        new_origin_x = old.origin.x + (old.size.width - w) / 2
        new_origin_y = old.origin.y + old.size.height - h
        self._panel.setFrame_display_animate_(
            Foundation.NSMakeRect(new_origin_x, new_origin_y, w, h), True, True)
        label_right_margin = 34 if mode != "done" else 34 + 46
        if mode == "done":
            # "done" has no meter competing for space -- give the label
            # nearly the full panel height so the transcribed text (line 2
            # of its string, after the status label) actually has room to
            # wrap and render instead of being clipped to one line.
            label_frame = Foundation.NSMakeRect(12, 8, w - label_right_margin, h - 16)
        elif mode == "recording":
            # Same clipping bug as "done" had: the live partial-transcript
            # preview (line 2, refreshed every partial_interval) needs room
            # to wrap above the meter, not just the one-line status height.
            label_frame = Foundation.NSMakeRect(12, 44, w - label_right_margin, h - 52)
        else:
            label_frame = Foundation.NSMakeRect(12, h - 30, w - label_right_margin, 20)
        self._label.setFrame_(label_frame)
        self._close.setFrame_(Foundation.NSMakeRect(w - 24, h - 24, 16, 16))
        self._meter.setFrame_(Foundation.NSMakeRect(12, 10, w - 24, 34))
        self._meter.setHidden_(mode != "recording")
        self._copy.setFrame_(Foundation.NSMakeRect(w - 70, h - 24, 46, 20))
        self._copy.setHidden_(mode != "done")
        self._layout_mode = mode

    def _render_main(self, model: OverlayModel):
        self._ensure_panel()
        if model.state == "hidden":
            self._panel.orderOut_(None)
            return
        mode = model.state if model.state in _EXPANDED_STATES else "collapsed"
        if mode != self._layout_mode:
            self._layout(mode)
        partial = model.partial[-200:]
        self._label.setStringValue_(
            model.label() + ("\n" + partial if partial else ""))
        if mode == "recording":
            self._meter.set_heights(level_to_bar_heights(model.level))
        elif mode == "done":
            self._copy_target.text = model.partial
        self._panel.orderFrontRegardless()

    def render(self, model: OverlayModel) -> None:
        run_on_main_thread(lambda: self._render_main(model))


_main_thread_runner_cls = None


def _main_thread_runner_class():
    # Lazily built ONCE and cached: defining a new NSObject subclass named
    # `_R` on every render() call (the pre-existing pattern this replaces)
    # raises `objc.error: _R is overriding existing Objective-C class` on
    # the second call -- PyObjC classes are registered globally by name and
    # can't be silently redefined. Root cause of TSK-SPK-UX-METER's overlay
    # getting stuck visible forever: the very first _hide() render() call
    # after the initial info-flash render() hit this, the exception was
    # swallowed inside rumps' timer callback, and orderOut_ never ran.
    global _main_thread_runner_cls
    if _main_thread_runner_cls is not None:
        return _main_thread_runner_cls
    from Foundation import NSObject

    class _MainThreadRunner(NSObject):
        def run_(self, _):
            self.fn()

    _main_thread_runner_cls = _MainThreadRunner
    return _main_thread_runner_cls


def run_on_main_thread(fn) -> None:
    """Marshal a no-arg callable onto the AppKit main thread. Any UI
    mutation (NSPanel, NSStatusItem title, ...) must go through this --
    Controller state changes can originate from a background transcription
    thread (TSK-SPK-UX-MENUBARSTATE reuses this for the menu-bar glyph)."""
    import objc
    runner = _main_thread_runner_class().alloc().init()
    runner.fn = fn
    runner.performSelectorOnMainThread_withObject_waitUntilDone_(
        objc.selector(runner.run_, signature=b"v@:@"), None, False)
