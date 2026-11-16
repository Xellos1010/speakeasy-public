"""User-visible launch/activation feedback (TSK-SPK-UX-DOCKFEEDBACK).

An LSUIElement app gives macOS-native zero feedback on Dock clicks: no
bounce, no running dot, no app menu. Users read that as "nothing happened."
This module flashes the overlay (no notification permissions needed) on
launch and whenever the Dock icon is clicked while the app is running.
"""
import logging
from typing import Callable, Optional

from speakeasy.overlay import OverlayModel

log = logging.getLogger(__name__)

RUNNING_MESSAGE = "🎙 speakeasy is running — icon in the menu bar · Right ⌘: tap to dictate, hold to talk"


def flash(render: Callable[[OverlayModel], None], message: str,
          duration: float = 2.5, timer_factory=None) -> None:
    """Show `message` in the overlay, auto-hide after `duration` seconds.

    `timer_factory(callback, interval)` must return an object with
    .start()/.stop() (rumps.Timer by default; injectable for tests).
    """
    if timer_factory is None:
        import rumps
        timer_factory = rumps.Timer

    render(OverlayModel(state="info", message=message))

    def _hide(timer):
        try:
            render(OverlayModel(state="hidden"))
        finally:
            try:
                timer.stop()
            except Exception:
                pass

    timer_factory(_hide, duration).start()


def install_reopen_handler(on_reopen: Callable[[], None]) -> bool:
    """Fire `on_reopen` when the user clicks the Dock icon while the app is
    already running (NSApplicationDelegate applicationShouldHandleReopen).
    rumps installs its own delegate class; add the selector to it. Returns
    False (logged) rather than raising if the rumps internals ever move.
    """
    try:
        import objc
        from rumps.rumps import NSApp as _RumpsDelegate

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):
            try:
                on_reopen()
            except Exception:
                log.warning("reopen feedback failed", exc_info=True)
            return False

        objc.classAddMethods(_RumpsDelegate, [
            objc.selector(applicationShouldHandleReopen_hasVisibleWindows_,
                          selector=b"applicationShouldHandleReopen:hasVisibleWindows:",
                          signature=b"B@:@B")])
        return True
    except Exception:
        log.warning("could not install Dock-reopen handler", exc_info=True)
        return False
