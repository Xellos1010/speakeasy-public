#!/bin/bash
# Production-readiness canary for the packaged speakeasy.app
# (TSK-SPK-HOTFIX-LAUNCH). Two probes:
#
#   1. SELFTEST — runs the frozen binary with --canary: real settings,
#      real model files, real Parakeet transcription of the bundled
#      sample, history SQLite round-trip, profile store, permission state.
#   2. LAUNCH — opens the .app through LaunchServices (the same code path
#      as a Dock click), then requires a FRESH run-loop heartbeat
#      (~/.speakeasy/run/heartbeat.json) within $LAUNCH_TIMEOUT seconds.
#      This is the probe that would have caught the original
#      dies-before-UI-without-Accessibility bug.
#
# Usage: scripts/canary.sh [/path/to/speakeasy.app]   (default /Applications)
set -uo pipefail

APP="${1:-/Applications/speakeasy.app}"
BIN="$APP/Contents/MacOS/speakeasy"
HEARTBEAT="$HOME/.speakeasy/run/heartbeat.json"
LAUNCH_TIMEOUT="${LAUNCH_TIMEOUT:-30}"
FAIL=0

say() { printf '%s\n' "$*"; }

[[ -x "$BIN" ]] || { say "FAIL: $BIN not found/executable"; exit 1; }

say "=== Canary 1/2: frozen-bundle selftest (--canary) ==="
SELFTEST_OUT="$("$BIN" --canary 2>&1)"
SELFTEST_RC=$?
say "$SELFTEST_OUT"
if (( SELFTEST_RC == 0 )); then
  say "PASS: selftest"
else
  say "FAIL: selftest (exit $SELFTEST_RC)"
  FAIL=1
fi

say ""
say "=== Canary 2/2: LaunchServices (Dock-path) launch probe ==="
# Only kill instances of THIS bundle, never the dev checkout.
pkill -f "$APP/Contents/MacOS/speakeasy" 2>/dev/null && sleep 1
BEFORE_TS=0
[[ -f "$HEARTBEAT" ]] && BEFORE_TS=$(stat -f '%m' "$HEARTBEAT")

open -g "$APP" || { say "FAIL: open(1) refused to launch"; exit 1; }

DEADLINE=$((SECONDS + LAUNCH_TIMEOUT))
LAUNCH_OK=0
while (( SECONDS < DEADLINE )); do
  if [[ -f "$HEARTBEAT" ]] && (( $(stat -f '%m' "$HEARTBEAT") > BEFORE_TS )); then
    LAUNCH_OK=1
    break
  fi
  sleep 1
done

if (( LAUNCH_OK )); then
  say "PASS: fresh heartbeat within ${LAUNCH_TIMEOUT}s:"
  cat "$HEARTBEAT"; echo
  if pgrep -f "$APP/Contents/MacOS/speakeasy" >/dev/null; then
    say "PASS: process alive"
  else
    say "FAIL: heartbeat written but process died"
    FAIL=1
  fi
else
  say "FAIL: no fresh heartbeat within ${LAUNCH_TIMEOUT}s (app died or hung pre-run-loop)"
  FAIL=1
fi

# Clean shutdown of the canary instance.
pkill -f "$APP/Contents/MacOS/speakeasy" 2>/dev/null

say ""
if (( FAIL == 0 )); then
  say "CANARY VERDICT: PASS — bundle is production-ready"
else
  say "CANARY VERDICT: FAIL"
fi
exit $FAIL
