#!/bin/sh
# Installs a launchd job that runs the FastVideoSave probe hourly for a macOS deployment.
# The probe re-learns FastVideoSave's API host into DATA_DIR, which the running app
# re-reads per request, so no restart is needed. Usage (from the app checkout):
#   deploy/macos/install-fastvideosave-probe.sh [interval-seconds]
set -eu
APP_DIR=$(cd "$(dirname "$0")/../.." && pwd)
INTERVAL=${1:-3600}
LABEL=com.universal-media-processor.fastvideosave-probe
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/universal-media-processor"
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>ProgramArguments</key><array>
    <string>$APP_DIR/.venv/bin/python</string><string>-m</string><string>app.fastvideosave_probe</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/fastvideosave-probe.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/fastvideosave-probe.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL (every ${INTERVAL}s); log: $LOG_DIR/fastvideosave-probe.log"
