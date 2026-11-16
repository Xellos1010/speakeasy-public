import ctypes, ctypes.util, time
from typing import Callable

def secure_input_active() -> bool:
    carbon = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/Carbon.framework/Carbon")
    return bool(carbon.IsSecureEventInputEnabled())

def frontmost_app_name() -> str | None:
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(app.localizedName()) if app else None

class _SystemPasteboard:
    def get(self):
        from AppKit import NSPasteboard, NSPasteboardTypeString
        return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
    def set(self, s):
        from AppKit import NSPasteboard, NSPasteboardTypeString
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(s, NSPasteboardTypeString)

def _post_cmd_v():
    import Quartz
    KEY_V = 9
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, KEY_V, True)
    up = Quartz.CGEventCreateKeyboardEvent(src, KEY_V, False)
    Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

def inject(text: str, pasteboard=None, post_paste=None, secure_check=None,
           restore_delay: float = 0.6, sleep=time.sleep) -> str:
    pb = pasteboard or _SystemPasteboard()
    secure = secure_check or secure_input_active
    paste = post_paste or _post_cmd_v
    if secure():
        pb.set(text)
        return "clipboard_only"
    old = pb.get()
    pb.set(text)
    try:
        paste()
        sleep(restore_delay)
    finally:
        pb.set(old if old is not None else "")
    return "injected"

def _post_backspace():
    import Quartz
    KEY_DELETE = 51  # keycode-51: backspace/delete
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, KEY_DELETE, True)
    up = Quartz.CGEventCreateKeyboardEvent(src, KEY_DELETE, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

def replace_last(old_text: str, new_text: str, expected_app: str | None,
                  frontmost: Callable[[], str | None] = frontmost_app_name,
                  pasteboard=None, post_paste=None, post_backspace=None,
                  secure_check=None, restore_delay: float = 0.6,
                  sleep=time.sleep) -> str:
    """In-place correction for a translate-mode injection that already
    happened: backspaces the previously-injected `old_text` (keycode-51 x
    its length) then pastes `new_text`.

    Focus guard (never correct into the wrong app): the caller passes
    `expected_app`, the frontmost app name captured at the time of the
    ORIGINAL injection. If the frontmost app has changed by the time this
    runs, this is a no-op -- returns "skipped" so the caller can record
    `translate_correction_skipped` instead of risking a backspace/paste into
    whatever the user has since switched to.
    """
    old_len = len(old_text)
    if frontmost() != expected_app:
        return "skipped"
    secure = secure_check or secure_input_active
    if secure():
        return "skipped"
    backspace = post_backspace or _post_backspace
    paste = post_paste or _post_cmd_v
    pb = pasteboard or _SystemPasteboard()
    for _ in range(old_len):
        backspace()
    old = pb.get()
    pb.set(new_text)
    try:
        paste()
        sleep(restore_delay)
    finally:
        pb.set(old if old is not None else "")
    return "replaced"
