#!/bin/bash
# Remove the Pre-Market Pulse V2 login agent installed by
# install_autostart_macos.sh.  V1 is not touched.

set -eu

LABEL="com.premarketpulse.v2"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl unload "$PLIST" >/dev/null 2>&1 || true

if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  echo "Removed: $PLIST"
else
  echo "Nothing to remove at $PLIST"
fi
