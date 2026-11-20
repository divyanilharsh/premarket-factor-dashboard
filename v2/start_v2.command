#!/bin/bash
# Pre-Market Pulse V2 - macOS launcher.
#
# Double-click this file in Finder, or run it from Terminal:
#
#   ./v2/start_v2.command              # start in background, open browser
#   ./v2/start_v2.command --no-open    # start in background, no browser
#   ./v2/start_v2.command --foreground # keep the server in this terminal
#   ./v2/start_v2.command --port 8082  # different port
#   ./v2/start_v2.command --stop       # stop the background server
#
# This is the macOS equivalent of "Start Market Pulse V2.bat".  It does not
# touch the V1 dashboard, its port (8080), or its data.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || exit 1

PORT=8081
PORT_SET=0
OPEN=1
FOREGROUND=0
STOP=0
ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --no-open) OPEN=0; shift ;;
    --open) OPEN=1; shift ;;
    --foreground|-f) FOREGROUND=1; shift ;;
    --stop) STOP=1; shift ;;
    --port)
      PORT="${2:-8081}"; PORT_SET=1
      ARGS+=("--port" "$PORT"); shift 2 ;;
    --port=*)
      PORT="${1#*=}"; PORT_SET=1
      ARGS+=("--port" "$PORT"); shift ;;
    *)
      ARGS+=("$1"); shift ;;
  esac
done

LOG_DIR="$ROOT/data/v2/logs"
LOG_FILE="$LOG_DIR/v2.log"
PID_FILE="$ROOT/data/v2/v2.pid"
URL="http://127.0.0.1:$PORT"

health() {
  curl -fsS --max-time 2 "$URL/api/health" >/dev/null 2>&1
}

# ---- stop ----------------------------------------------------------------
if [ "$STOP" -eq 1 ]; then
  if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID" && echo "Stopped V2 (pid $PID)."
    else
      echo "No running V2 process for pid ${PID:-?}."
    fi
    rm -f "$PID_FILE"
  else
    echo "No pid file at $PID_FILE."
  fi
  exit 0
fi

# ---- locate Python -------------------------------------------------------
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
  echo "Python 3 was not found."
  echo "Install it with:  brew install python3"
  echo "or set PYTHON=/path/to/python3 and run again."
  exit 1
}

if ! "$PY" -c "import requests" >/dev/null 2>&1; then
  echo "The 'requests' package is missing for $PY."
  echo "Install it with:  $PY -m pip install -r requirements.txt"
  exit 1
fi

# ---- already running? just open the dashboard ----------------------------
if health; then
  echo "V2 is already running at $URL"
  [ "$OPEN" -eq 1 ] && open "$URL"
  exit 0
fi

[ "$PORT_SET" -eq 1 ] || ARGS+=("--port" "$PORT")

# ---- foreground: run in this terminal ------------------------------------
if [ "$FOREGROUND" -eq 1 ]; then
  exec "$PY" "$ROOT/v2/market_pulse_v2.py" ${ARGS[@]+"${ARGS[@]}"}
fi

# ---- background: log to data/v2/logs/v2.log ------------------------------
mkdir -p "$LOG_DIR"
echo "" >> "$LOG_FILE"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') starting V2 on port $PORT ===" >> "$LOG_FILE"

nohup "$PY" "$ROOT/v2/market_pulse_v2.py" ${ARGS[@]+"${ARGS[@]}"} >> "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

for _ in $(seq 1 30); do
  sleep 0.5
  health && break
  kill -0 "$PID" 2>/dev/null || break
done

if health; then
  echo "V2 started (pid $PID) at $URL"
  echo "Log: $LOG_FILE"
  echo "Stop with: ./v2/start_v2.command --stop"
  [ "$OPEN" -eq 1 ] && open "$URL"
else
  echo "V2 did not start. Last log lines:"
  tail -n 20 "$LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
