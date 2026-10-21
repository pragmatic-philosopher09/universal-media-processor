#!/bin/sh
# Installs a launchd job running a cloudflared quick tunnel to the local app and enables
# download redirects in .env. File downloads requested through the app's main public hostname
# (e.g. a Tailscale Funnel, which is throughput-limited) are then redirected to the tunnel.
# The tunnel hostname changes on restart; the app re-reads it from cloudflared's metrics server.
# Usage (from the app checkout): deploy/macos/install-download-tunnel.sh [app-port] [metrics-port]
set -eu
APP_DIR=$(cd "$(dirname "$0")/../.." && pwd)
APP_PORT=${1:-8766}
METRICS=127.0.0.1:${2:-20241}
LABEL=com.universal-media-processor.cloudflared
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/universal-media-processor"
CLOUDFLARED=$(command -v cloudflared || true)
[ -n "$CLOUDFLARED" ] || { echo "Install cloudflared first: brew install cloudflared" >&2; exit 1; }
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$CLOUDFLARED</string><string>tunnel</string><string>--no-autoupdate</string>
    <string>--metrics</string><string>$METRICS</string>
    <string>--url</string><string>http://127.0.0.1:$APP_PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/cloudflared.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/cloudflared.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
ENV_FILE="$APP_DIR/.env"
touch "$ENV_FILE"
if grep -q '^DOWNLOAD_TUNNEL_METRICS=' "$ENV_FILE"; then
  sed -i '' "s|^DOWNLOAD_TUNNEL_METRICS=.*|DOWNLOAD_TUNNEL_METRICS=$METRICS|" "$ENV_FILE"
else
  printf '\nDOWNLOAD_TUNNEL_METRICS=%s\n' "$METRICS" >> "$ENV_FILE"
fi
echo "Installed $LABEL (metrics $METRICS); log: $LOG_DIR/cloudflared.log"
echo "Restart the app to pick up DOWNLOAD_TUNNEL_METRICS from $ENV_FILE."
