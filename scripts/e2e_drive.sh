#!/bin/bash
# Autonomous E2E proof: dictate via speakers->mic, verify injection into TextEdit.
# Usage: e2e_drive.sh "<sentence to speak>" <evidence-tag>
set -euo pipefail
SENTENCE="${1:?sentence}"
TAG="${2:?tag}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
EV="$ROOT/evidence"
mkdir -p "$EV"

press_f5() {
  "$PY" - <<'PYEOF'
import Quartz, time
src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
down = Quartz.CGEventCreateKeyboardEvent(src, 96, True)
up = Quartz.CGEventCreateKeyboardEvent(src, 96, False)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
time.sleep(0.05)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
PYEOF
}

osascript -e 'set volume output volume 65'
osascript -e 'tell application "TextEdit"
  activate
  make new document
end tell'
sleep 2

press_f5                      # start recording
sleep 1.2
say "$SENTENCE"               # speakers -> mic
sleep 0.6
press_f5                      # stop -> transcribe -> (rewrite) -> inject
sleep 10

screencapture -x "$EV/e2e-$TAG.png"
osascript -e 'tell application "TextEdit" to get text of document 1' > "$EV/e2e-$TAG-textedit.txt"
echo "--- TextEdit content ---"
cat "$EV/e2e-$TAG-textedit.txt"
echo "--- history ---"
sqlite3 ~/.speakeasy/history.db "SELECT id,mode,status,account,latency_ms,substr(raw,1,80),substr(COALESCE(rewritten,''),1,80) FROM dictations ORDER BY id DESC LIMIT 2;" | tee "$EV/e2e-$TAG-history.txt"
