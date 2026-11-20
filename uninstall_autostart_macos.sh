#!/bin/bash
# Remove the Pre-Market Pulse login agents installed by
# install_autostart_macos.sh (V1 and V2).
#
#   ./uninstall_autostart_macos.sh          # both
#   ./uninstall_autostart_macos.sh --only v1
#   ./uninstall_autostart_macos.sh --only v2

set -eu

ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only)
      ONLY="${2:-}"; shift 2 ;;
    --only=*)
      ONLY="${1#*=}"; shift ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done

case "$ONLY" in
  ""|v1|v2) ;;
  *) echo "--only must be v1 or v2"; exit 2 ;;
esac

remove_agent() {
  label="$1"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
  launchctl unload "$plist" >/dev/null 2>&1 || true
  if [ -f "$plist" ]; then
    rm -f "$plist"
    echo "Removed $label"
  else
    echo "Nothing to remove for $label"
  fi
}

if [ "$ONLY" != "v2" ]; then
  remove_agent "com.premarketpulse.v1"
fi
if [ "$ONLY" != "v1" ]; then
  remove_agent "com.premarketpulse.v2"
fi

echo "The running dashboards, if any, were left alone."
echo "Stop them with: ./start_v1.command --stop  and  ./v2/start_v2.command --stop"
