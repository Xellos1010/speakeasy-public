#!/usr/bin/env bash
# Install SpeakEasy on a phone over adb and prove the live path on-device.
# Usage: SPEAKEASY_SERVER_URL=https://<your-host>:8445 android/scripts/install-and-e2e.sh <adb-serial>
#   (ANDROID_SDK_ROOT / ANDROID_HOME locate adb; `speakeasy` must be on PATH)
# - installs app-debug + androidTest APKs (never uninstalls, never changes phone settings)
# - if the app is not yet paired, mints a one-time code (`speakeasy pair`) into a 0600 temp
#   file and passes it to the instrumented test WITHOUT printing it; the test pairs the app, so
#   the phone is ready to dictate afterwards
# - runs LiveServerE2ETest via `am instrument` (no UI input), prints E2E_RESULT + latency
# - launches SpeakEasy once at the end
set -euo pipefail
SERIAL="${1:?usage: install-and-e2e.sh <adb-serial>}"
SERVER_URL="${SPEAKEASY_SERVER_URL:?set SPEAKEASY_SERVER_URL to your server base URL}"
SDK="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
ADB="$SDK/platform-tools/adb"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APK="$ROOT/app/build/outputs/apk/debug/app-debug.apk"
TAPK="$ROOT/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk"
# rung ladder: cheapest first, stop at first failure
"$ADB" devices | grep -q "^$SERIAL[[:space:]]*device$" || { echo "FAIL rung1: $SERIAL not on adb"; exit 2; }
[ -f "$APK" ] && [ -f "$TAPK" ] || { echo "FAIL rung2: build first: ./gradlew assembleDebug assembleDebugAndroidTest"; exit 2; }
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$SERVER_URL/v1/health") || true
[ "$code" = "200" ] || { echo "FAIL rung3: API health=$code"; exit 2; }
"$ADB" -s "$SERIAL" shell pm list packages | grep -q com.tailscale.ipn || echo "WARN: Tailscale app not installed on phone"
"$ADB" -s "$SERIAL" install -r "$APK" | tail -1
"$ADB" -s "$SERIAL" install -r -t "$TAPK" | tail -1
ARGS=()
TMP="$(mktemp -d)"; chmod 700 "$TMP"; trap 'command -v rmq >/dev/null && rmq "$TMP" >/dev/null 2>&1 || true' EXIT
if [ "${PAIR:-1}" = "1" ]; then
  ( umask 077; speakeasy pair --device "$("$ADB" -s "$SERIAL" shell getprop ro.product.model | tr -d '\r')" --ttl 300 --out "$TMP/pair.md" >/dev/null )
  PC="$(grep -oE "': [0-9]{6}" "$TMP/pair.md" | grep -oE '[0-9]{6}')"
  ARGS=(-e pairCode "$PC")
fi
OUT="$("$ADB" -s "$SERIAL" shell am instrument -w -r "${ARGS[@]}" \
  -e class com.speakeasy.android.LiveServerE2ETest \
  com.speakeasy.android.test/androidx.test.runner.AndroidJUnitRunner 2>&1 | tr -d '\r')"
echo "$OUT" | grep -E "E2E_RESULT|INSTRUMENTATION_CODE|OK \(|FAILURES|Error|assum" | sed -E 's/pairCode[^ ]*//g' | head -12
"$ADB" -s "$SERIAL" shell am start -n com.speakeasy.android/.ui.MainActivity >/dev/null
echo "launched SpeakEasy on $SERIAL"
