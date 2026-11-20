#!/bin/bash
# Compatibility wrapper: install only the V2 login agent.
#
#   ./v2/install_autostart_macos.sh
#   ./v2/uninstall_autostart_macos.sh
#
# The shared installer lives at the project root and handles V1 and V2:
#   ./install_autostart_macos.sh --only v1|v2
#   ./install_autostart_macos.sh              # both

set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

exec "$ROOT/install_autostart_macos.sh" --only v2 "$@"
