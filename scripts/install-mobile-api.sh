#!/usr/bin/env bash
# Install/refresh the mobile API launchd job + tailnet-only HTTPS (never Funnel).
# Rollback: launchctl bootout gui/$(id -u)/org.speakeasy.mobile-api ; tailscale serve --https=8445 off
set -euo pipefail
REPO="${REPO:-$HOME/Projects/speakeasy}"
LABEL=org.speakeasy.mobile-api
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/.speakeasy/mobile" && chmod 700 "$HOME/.speakeasy/mobile"
sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO#g" \
  "$REPO/scripts/launchd/$LABEL.plist.template" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
tailscale serve --bg --https=8445 http://127.0.0.1:8812
# wrapper so `speakeasy pair --device <name>` works from any shell
mkdir -p "$HOME/bin"
cat > "$HOME/bin/speakeasy" <<W
#!/usr/bin/env bash
PYTHONPATH="$REPO" exec "$HOME/.speakeasy/venv-mobile/bin/python" -m speakeasy.mobile.cli "\$@"
W
chmod 755 "$HOME/bin/speakeasy"
