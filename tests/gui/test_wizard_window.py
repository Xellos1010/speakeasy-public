"""speakeasy/gui/wizard_window.py -- only the pure-Python surface (no
AppKit/WebKit) is exercised here. This matches speakeasy/gui/window.py
(the analogous Home-window class): it has no dedicated test file either,
because _ensure_built()/show()'s AppKit-dependent half can't run outside a
real macOS GUI session under pytest. See the report's launch instructions
for how that half is exercised manually.
"""
from dataclasses import dataclass

from speakeasy.gui import wizard_window


@dataclass
class _FakeCtx:
    controller: object = None


def test_module_has_no_top_level_darwin_imports():
    """ADJ-SPK-024-style cross-platform-collection guard (mirrors
    speakeasy/gui/window.py's own lazy-import convention): AppKit/WebKit/
    Foundation must only ever be imported inside a method body, never at
    module scope, or `pytest tests/` would fail to collect on Linux."""
    import ast
    from pathlib import Path

    src = Path(wizard_window.__file__).read_text()
    tree = ast.parse(src)
    forbidden = {"AppKit", "WebKit", "Foundation"}
    for node in tree.body:  # module-level statements only, not nested in defs
        if isinstance(node, ast.Import):
            names = {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {node.module} if node.module else set()
        else:
            continue
        assert not (names & forbidden), f"module-level import of {names & forbidden}"


def test_get_or_create_returns_the_same_singleton():
    wizard_window._instance = None
    try:
        ctx = _FakeCtx()
        a = wizard_window.get_or_create(ctx)
        b = wizard_window.get_or_create(ctx)
        assert a is b
        assert isinstance(a, wizard_window.WizardWindow)
    finally:
        wizard_window._instance = None


def test_push_is_a_noop_before_the_window_is_built():
    ctx = _FakeCtx()
    window = wizard_window.WizardWindow(ctx)
    # Must not raise and must not attempt any AppKit call -- _window is
    # still None because _ensure_built() (AppKit-only) was never invoked.
    window.push("wizard_prompt", {"text": "hello"})
    assert window._window is None
