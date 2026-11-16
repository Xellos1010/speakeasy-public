"""py2app packaging target for speakeasy.

Builds a standalone menu-bar `.app` bundle so macOS privacy grants
(Accessibility, Microphone) attach to a stable bundle identifier
(`org.speakeasy.dictation` by default; override with SPEAKEASY_BUNDLE_ID or the
deployment profile's packaging.bundle_id) instead of whatever terminal happens to launch the
process. See docs/adr/0002-py2app-packaging.md for the decision record and
scripts/package.sh for the reproducible build entry point.

Models are NOT bundled: speakeasy downloads/reads them from
`~/.speakeasy/models/` at runtime (see speakeasy/engine/download.py and
speakeasy/config.py:speakeasy_dir) regardless of where the app binary lives,
so the app bundle stays small and model updates don't require a rebuild.
"""

from setuptools import setup

# py2app is only present in the dedicated packaging venv (.venv-pkg), never
# in the shared dev .venv or in pip's PEP 517 build-isolation env used for
# plain `pip install -e .` (that env only gets pyproject.toml's declared
# build requirement, setuptools). Import it lazily so metadata-only builds
# (editable installs, sdists) don't fail just because py2app isn't
# installed; the custom command class below is only wired up when it is.
try:
    from py2app.build_app import py2app as _py2app_command
except ImportError:
    _py2app_command = None

APP = ["speakeasy/__main__.py"]

CMDCLASS = {}
if _py2app_command is not None:

    class NoDepsPy2App(_py2app_command):
        """py2app hard-refuses to run when `Distribution.install_requires`
        is set (see py2app/build_app.py: "install_requires is no longer
        supported"). Modern setuptools synthesizes that attribute from
        this project's pyproject.toml `[project.dependencies]` table
        (needed for the normal `pip install -e .` dev workflow) even
        though setup.py itself never sets it — py2app can't tell the two
        apart and blocks unconditionally. It's a false positive here:
        py2app bundles the *already-installed* site-packages via the
        `packages=` option below, so pip-style requirement strings never
        come into play for the frozen build. Clear the attribute before
        py2app's own finalize_options() checks it.
        """

        def finalize_options(self):
            self.distribution.install_requires = None
            super().finalize_options()

    CMDCLASS["py2app"] = NoDepsPy2App

DATA_FILES = []

def _bundle_id() -> str:
    import os
    env = os.environ.get("SPEAKEASY_BUNDLE_ID")
    if env:
        return env
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from speakeasy.deploy_profile import load
        return load().get("packaging.bundle_id") or "org.speakeasy.dictation"
    except Exception:
        return "org.speakeasy.dictation"


BUNDLE_ID = _bundle_id()

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/speakeasy.icns",
    # LSUIElement: menu-bar-only app, no Dock icon, no app menu bar.
    "plist": {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "speakeasy",
        "CFBundleDisplayName": "speakeasy",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSUIElement": True,
        "LSMinimumSystemVersion": "11.0",
        # TCC (privacy) usage-description strings. macOS refuses to even
        # show the permission prompt without these; Accessibility does not
        # use an Info.plist description (it is granted purely via the
        # System Settings > Privacy & Security > Accessibility list keyed
        # off CFBundleIdentifier), but Microphone requires this key.
        "NSMicrophoneUsageDescription": (
            "speakeasy transcribes your speech locally (on-device) to "
            "insert dictated text at your cursor. Audio never leaves "
            "this Mac."
        ),
        # Some macOS versions also probe Apple Events / input-monitoring
        # style descriptions for hotkey-capture apps; harmless to declare
        # even if not strictly required for the CGEventTap approach used
        # by speakeasy.hotkey.
        "NSAppleEventsUsageDescription": (
            "speakeasy uses Apple Events to paste dictated text into the "
            "focused application."
        ),
    },
    # Keep the sherpa-onnx / sounddevice / pyobjc native extensions and
    # their bundled dylibs (libsherpa-onnx-c-api.dylib,
    # libonnxruntime*.dylib, _sounddevice_data/portaudio-binaries/
    # libportaudio.dylib, etc.) by packaging the whole package tree rather
    # than trying to enumerate individual native libs. py2app's dependency
    # walker does not reliably discover ctypes-loaded / dlopen'd dylibs
    # inside package data directories, so they are listed explicitly here
    # as full packages to force-include.
    "packages": [
        "speakeasy",
        "sherpa_onnx",
        "sounddevice",
        "_sounddevice_data",
        "rumps",
        # Accessibility prompt/self-heal (speakeasy.permissions) — imported
        # lazily, so modulegraph misses it without an explicit entry.
        "ApplicationServices",
        "HIServices",
    ],
    # rumps ships an .icns app icon resource; sherpa_onnx and
    # _sounddevice_data ship compiled/native binaries. Both need to be
    # copied verbatim rather than analyzed as pure-python source.
    "resources": [],
    "includes": [
        "yaml",
    ],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    # py2app itself is installed directly into .venv-pkg by
    # scripts/package.sh (`pip install -e . py2app`), not via the legacy
    # `setup_requires` egg-fetch mechanism, which is deprecated upstream.
    cmdclass=CMDCLASS,
)
