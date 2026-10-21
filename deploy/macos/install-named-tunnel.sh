#!/bin/sh
# Sets up a permanent Cloudflare tunnel (fixed hostname on your own Cloudflare domain) to the
# local app, runs it under launchd, and points download redirects at it. Replaces the quick
# tunnel from install-download-tunnel.sh. The hostname also serves the whole app directly.
# Usage (from the app checkout): deploy/macos/install-named-tunnel.sh media.example.com [app-port]
set -eu
HOST=${1:?usage: $0 <hostname-on-your-cloudflare-domain> [app-port]}
APP_PORT=${2:-8766}
APP_DIR=$(cd "$(dirname "$0")/../.." && pwd)
NAME=${TUNNEL_NAME:-mediaflow}
CF_DIR="$HOME/.cloudflared"
LABEL=com.universal-media-processor.cloudflared
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/universal-media-processor"
CLOUDFLARED=$(command -v cloudflared || true)
[ -n "$CLOUDFLARED" ] || { echo "Install cloudflared first: brew install cloudflared" >&2; exit 1; }
PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python3

[ -f "$CF_DIR/cert.pem" ] || "$CLOUDFLARED" tunnel login
tunnel_id() {
  "$CLOUDFLARED" tunnel list --name "$NAME" --output json 2>/dev/null |
    "$PY" -c 'import json,sys; t=json.load(sys.stdin) or []; print(t[0]["id"] if t else "")'
}
ID=$(tunnel_id)
if [ -z "$ID" ]; then
  "$CLOUDFLARED" tunnel create "$NAME"
  ID=$(tunnel_id)
fi
[ -f "$CF_DIR/$ID.json" ] || {
  echo "Missing credentials $CF_DIR/$ID.json for tunnel $NAME; delete it (cloudflared tunnel delete $NAME) and re-run." >&2
  exit 1
}
"$CLOUDFLARED" tunnel route dns --overwrite-dns "$NAME" "$HOST"

CONFIG="$CF_DIR/$NAME.yml"
cat > "$CONFIG" <<YML
tunnel: $ID
credentials-file: $CF_DIR/$ID.json
ingress:
  - hostname: $HOST
    service: http://127.0.0.1:$APP_PORT
  - service: http_status:404
YML

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$CLOUDFLARED</string><string>tunnel</string><string>--no-autoupdate</string>
    <string>--config</string><string>$CONFIG</string>
    <string>--metrics</string><string>127.0.0.1:20241</string>
    <string>run</string><string>$NAME</string>
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
if grep -q '^DOWNLOAD_TUNNEL_HOSTNAME=' "$ENV_FILE"; then
  sed -i '' "s|^DOWNLOAD_TUNNEL_HOSTNAME=.*|DOWNLOAD_TUNNEL_HOSTNAME=$HOST|" "$ENV_FILE"
else
  printf '\nDOWNLOAD_TUNNEL_HOSTNAME=%s\n' "$HOST" >> "$ENV_FILE"
fi
echo "Tunnel $NAME ($ID) serves https://$HOST; log: $LOG_DIR/cloudflared.log"
echo "Restart the app to pick up DOWNLOAD_TUNNEL_HOSTNAME from $ENV_FILE."
