#!/bin/bash
# Compatibility wrapper: remove only the V2 login agent.
#
#   ./v2/uninstall_autostart_macos.sh
#
# The shared uninstaller lives at the project root:
#   ./uninstall_autostart_macos.sh [--only v1|v2]

set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

exec "$ROOT/uninstall_autostart_macos.sh" --only v2 "$@"
