#!/bin/bash
# Remove ONLY the V3 login agent (com.premarketpulse.v3).

set -eu

LABEL="com.premarketpulse.v3"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl unload "$PLIST" >/dev/null 2>&1 || true

if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  echo "Removed $LABEL"
else
  echo "Nothing to remove at $PLIST"
fi
