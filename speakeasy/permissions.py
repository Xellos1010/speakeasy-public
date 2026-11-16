"""macOS permission helpers + degraded-startup policy.

Root cause this module exists for (TSK-SPK-HOTFIX-LAUNCH): CGEventTapCreate
returns None when the bundle lacks the Accessibility grant, HotkeyListener
.start() raises PermissionError, and — pre-fix — that fired *before*
rumps' run loop started, so the packaged app died into py2app's error
dialog on first Dock launch. The app must always reach the menu bar; a
missing grant is a degraded mode, not a fatal error.
"""
import logging
from typing import Callable

log = logging.getLogger(__name__)


def accessibility_trusted(prompt: bool = False) -> bool:
    """True when this process holds the Accessibility grant.

    With prompt=True, macOS both shows the "grant access" dialog and adds
    the app to the System Settings > Privacy & Security > Accessibility
    list (untoggled), which is the only supported way to get it listed —
    CGEventTapCreate alone never registers the app there.
    """
    from ApplicationServices import (  # local import: needs a GUI session
        AXIsProcessTrustedWithOptions,
        kAXTrustedCheckOptionPrompt,
    )

    return bool(AXIsProcessTrustedWithOptions(
        {kAXTrustedCheckOptionPrompt: True} if prompt else None))


def start_hotkey_with_degradation(listener, on_degraded: Callable[[str], None]) -> bool:
    """Start the hotkey listener; on a *permission* failure, report and
    continue instead of raising (the caller keeps the app alive and
    retries once the grant appears). Any other exception is a real bug
    and still propagates.
    """
    try:
        listener.start()
        return True
    except PermissionError as exc:
        log.warning("hotkey disabled — %s", exc)
        on_degraded(str(exc))
        return False
