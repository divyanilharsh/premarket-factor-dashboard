#!/bin/bash
# Optional: start Pre-Market Pulse V2 automatically at login on macOS.
#
# This is the macOS equivalent of "Install V2 AutoStart.bat".  It installs a
# LaunchAgent that starts V2 only.  It does not touch the V1 dashboard, its
# port (8080), or its data.
#
#   ./v2/install_autostart_macos.sh
#   ./v2/install_autostart_macos.sh --port 8081
#
# Remove it again with:  ./v2/uninstall_autostart_macos.sh

set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PORT=8081
if [ "${1:-}" = "--port" ] && [ -n "${2:-}" ]; then
  PORT="$2"
fi

LABEL="com.premarketpulse.v2"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$ROOT/data/v2/logs"

find_python() {
  if [ -n "${PYTHON:-}" ] && command -v "$PYTHON" >/dev/null 2>&1; then
    command -v "$PYTHON"; return 0
  fi
  for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
                   /opt/miniconda3/bin/python3 /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}

PY="$(find_python)" || {
  echo "Python 3 was not found. Install it with: brew install python3"
  exit 1
}

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$ROOT/v2/market_pulse_v2.py</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$ROOT</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <false/>

  <key>ProcessType</key>
  <string>Background</string>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/v2.launchd.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/v2.launchd.log</string>
</dict>
</plist>
PLIST_EOF

# Replace any previous version of the agent.
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
  :
else
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  launchctl load -w "$PLIST"
fi

echo "Installed: $PLIST"
echo "V2 will start at every login on http://127.0.0.1:$PORT"
echo "Python:    $PY"
echo "Log:       $LOG_DIR/v2.launchd.log"
echo "V1 auto-start was not touched."
