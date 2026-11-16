#!/bin/bash
# Build speakeasy.app via py2app into a stable-bundle-ID standalone .app,
# so macOS TCC (Accessibility, Microphone) grants attach to the bundle
# instead of whatever terminal launches the dev process.
#
# Usage:
#   scripts/package.sh          # full py2app build (release-shaped bundle)
#   scripts/package.sh -A       # alias mode (dev build; symlinks back to
#                                # the source tree instead of copying it —
#                                # faster iteration, NOT for distribution)
#
# Uses a dedicated .venv-pkg/ (not the shared .venv/) so py2app's own
# dependency set doesn't leak into the dev/test environment. See
# docs/adr/0002-py2app-packaging.md for why py2app was chosen and what
# full vs alias mode means for the TCC grant.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE_FLAG=""
if [[ "${1:-}" == "-A" ]]; then
  MODE_FLAG="-A"
fi

VENV="$ROOT/.venv-pkg"

# py2app GUI apps require a *framework* Python; a conda/Homebrew python3 on
# PATH produces a bundle with broken dyld paths (canary caught exactly this:
# python3.12/_ctypes.so -> libffi.8.dylib not loadable). Pin the python.org
# framework build; override with PACKAGE_PYTHON if it ever moves.
PACKAGE_PYTHON="${PACKAGE_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13}"
if [[ ! -x "$PACKAGE_PYTHON" ]]; then
  echo "FAIL: framework python not found at $PACKAGE_PYTHON (set PACKAGE_PYTHON)" >&2
  exit 1
fi
if [[ -z "$("$PACKAGE_PYTHON" -c 'import sysconfig; print(sysconfig.get_config_var("PYTHONFRAMEWORK") or "")')" ]]; then
  echo "FAIL: $PACKAGE_PYTHON is not a framework build — py2app bundles from it will not launch" >&2
  exit 1
fi
# A venv bootstrapped from the wrong interpreter poisons every later build:
# detect and rebuild it rather than trusting whatever is there.
if [[ -x "$VENV/bin/python" ]]; then
  WANT="$("$PACKAGE_PYTHON" -c 'import sys; print(sys.version_info[:2])')"
  HAVE="$("$VENV/bin/python" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || echo none)"
  if [[ "$WANT" != "$HAVE" ]]; then
    echo "==> .venv-pkg is $HAVE but packaging python is $WANT — rebuilding venv"
    rm -rf "$VENV"
  fi
fi
BOOTSTRAPPED=0
if [[ ! -x "$VENV/bin/python" ]]; then
  BOOTSTRAPPED=1
  echo "==> bootstrapping $VENV from $PACKAGE_PYTHON"
  "$PACKAGE_PYTHON" -m venv "$VENV"
  # python.org builds sometimes ship without a CA bundle; point at macOS
  # system roots so pip's TLS handshake to PyPI doesn't fail.
  export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/cert.pem}"
  "$VENV/bin/pip" install --upgrade pip -q
fi
# Always re-sync deps (idempotent, seconds when satisfied): a reused venv
# must pick up new pyproject dependencies, or py2app fails on modules the
# code imports but the venv never received (canary caught exactly this
# with pyobjc-framework-ApplicationServices).
export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/cert.pem}"
"$VENV/bin/pip" install -e . py2app -q

echo "==> rm -rf build/ dist/"
rm -rf build dist

echo "==> $VENV/bin/python setup.py py2app ${MODE_FLAG}"
"$VENV/bin/python" setup.py py2app ${MODE_FLAG}

APP="dist/speakeasy.app"
BIN="$APP/Contents/MacOS/speakeasy"

echo "==> smoke: verify bundle binary exists"
if [[ ! -x "$BIN" ]]; then
  echo "FAIL: $BIN not found or not executable" >&2
  exit 1
fi
echo "OK: $BIN"

echo "==> smoke: codesign (stable identity preferred — keeps TCC grants across rebuilds)"
# Optional stable code-signing identity. Per-deployment values come from the environment (set them
# from your deployment profile, docs/PROFILE.md); nothing here is secret and no password is stored:
#   SPEAKEASY_SIGN_IDENTITY            codesign identity name (default: speakeasy-signing)
#   SPEAKEASY_SIGN_KEYCHAIN            keychain file holding that identity (unset = ad-hoc signing)
#   SPEAKEASY_SIGN_KEYCHAIN_PW_ENV     NAME of the env var holding that keychain's password
# Signing with the SAME certificate every build keeps macOS TCC grants (Accessibility/Microphone)
# valid across rebuilds; ad-hoc signing invalidates them every time (per-build cdhash DR).
SIGN_IDENTITY="${SPEAKEASY_SIGN_IDENTITY:-speakeasy-signing}"
SIGN_KEYCHAIN="${SPEAKEASY_SIGN_KEYCHAIN:-}"
PW_ENV_NAME="${SPEAKEASY_SIGN_KEYCHAIN_PW_ENV:-SPK_SIGN_KEYCHAIN_PW}"
if [[ -n "$SIGN_KEYCHAIN" && -n "${!PW_ENV_NAME:-}" ]] \
   && security unlock-keychain -p "${!PW_ENV_NAME}" "$SIGN_KEYCHAIN" 2>/dev/null \
   && security find-certificate -c "$SIGN_IDENTITY" "$SIGN_KEYCHAIN" >/dev/null 2>&1; then
  codesign --force --deep --sign "$SIGN_IDENTITY" --keychain "$SIGN_KEYCHAIN" "$APP"
else
  echo "WARN: stable signing identity unavailable -- falling back to AD-HOC" >&2
  echo "      (TCC grants will NOT survive this rebuild)" >&2
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --verbose "$APP"
codesign -dvv "$APP" 2>&1 | grep -E "^Authority" | head -1

echo "==> done: $APP ($(du -sh "$APP" | cut -f1))"
echo "    Bundle ID: $(defaults read "$ROOT/$APP/Contents/Info" CFBundleIdentifier)"
if [[ -n "$MODE_FLAG" ]]; then
  echo "    Mode: ALIAS (-A) — dev build, symlinks to source; not for distribution."
else
  echo "    Mode: FULL — self-contained bundle."
fi
