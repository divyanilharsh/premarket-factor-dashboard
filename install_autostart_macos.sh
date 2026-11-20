#!/bin/bash
# Start Pre-Market Pulse automatically at login on macOS.
#
# Installs two separate LaunchAgents:
#
#   com.premarketpulse.v1  ->  market_mobile.py on port 8080
#   com.premarketpulse.v2  ->  v2/market_pulse_v2.py on port 8081
#
#   ./install_autostart_macos.sh              # both
#   ./install_autostart_macos.sh --only v1    # V1 only
#   ./install_autostart_macos.sh --only v2    # V2 only
#   ./install_autostart_macos.sh --port 8090  # base port for both
#
# Remove them again with:  ./uninstall_autostart_macos.sh

set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE"

ONLY=""
V1_PORT=8080
V2_PORT=8081

while [ $# -gt 0 ]; do
  case "$1" in
    --only)
      ONLY="${2:-}"; shift 2 ;;
    --only=*)
      ONLY="${1#*=}"; shift ;;
    --port)
      V1_PORT="${2:-8080}"; V2_PORT=$((V1_PORT + 1)); shift 2 ;;
    --v1-port)
      V1_PORT="${2:-8080}"; shift 2 ;;
    --v2-port)
      V2_PORT="${2:-8081}"; shift 2 ;;
    -h|--help)
      sed -n '2,17p' "$0"; exit 0 ;;
    *)
      echo "Unknown option: $1"; exit 2 ;;
  esac
done

case "$ONLY" in
  ""|v1|v2) ;;
  *) echo "--only must be v1 or v2"; exit 2 ;;
esac

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

if ! "$PY" -c "import requests" >/dev/null 2>&1; then
  echo "The 'requests' package is missing for $PY."
  echo "Install it with:  $PY -m pip install -r requirements.txt"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs" "$ROOT/data/v2/logs"

install_agent() {
  label="$1"; script="$2"; port="$3"; logfile="$4"; title="$5"

  if [ ! -f "$script" ]; then
    echo "ERROR: missing $script"
    return 1
  fi

  plist="$HOME/Library/LaunchAgents/$label.plist"

  cat > "$plist" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$script</string>
    <string>--port</string>
    <string>$port</string>
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
  <string>$logfile</string>

  <key>StandardErrorPath</key>
  <string>$logfile</string>
</dict>
</plist>
PLIST_EOF

  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
    :
  else
    launchctl unload "$plist" >/dev/null 2>&1 || true
    launchctl load -w "$plist"
  fi

  echo "Installed $title"
  echo "  label : $label"
  echo "  url   : http://127.0.0.1:$port"
  echo "  plist : $plist"
}

if [ "$ONLY" != "v2" ]; then
  install_agent "com.premarketpulse.v1" "$ROOT/market_mobile.py" \
    "$V1_PORT" "$ROOT/data/logs/v1.launchd.log" "V1 (port $V1_PORT)"
fi

if [ "$ONLY" != "v1" ]; then
  install_agent "com.premarketpulse.v2" "$ROOT/v2/market_pulse_v2.py" \
    "$V2_PORT" "$ROOT/data/v2/logs/v2.launchd.log" "V2 (port $V2_PORT)"
fi

echo ""
echo "Both start at every login. Stop them with:"
echo "  ./start_v1.command --stop"
echo "  ./v2/start_v2.command --stop"
