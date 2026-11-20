#!/bin/bash
# Pre-Market Pulse V1 - macOS launcher.
#
# Double-click this file in Finder, or run it from Terminal:
#
#   ./start_v1.command              # start in background, open browser
#   ./start_v1.command --no-open    # start in background, no browser
#   ./start_v1.command --foreground # keep the server in this terminal
#   ./start_v1.command --port 8082  # different port
#   ./start_v1.command --stop       # stop the background server
#
# This is the macOS equivalent of "Start Market Pulse.bat".

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE"
cd "$ROOT" || exit 1

PORT=8080
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
      PORT="${2:-8080}"; PORT_SET=1
      ARGS+=("--port" "$PORT"); shift 2 ;;
    --port=*)
      PORT="${1#*=}"; PORT_SET=1
      ARGS+=("--port" "$PORT"); shift ;;
    *)
      ARGS+=("$1"); shift ;;
  esac
done

LOG_DIR="$ROOT/data/logs"
LOG_FILE="$LOG_DIR/v1.log"
PID_FILE="$ROOT/data/v1.pid"
URL="http://127.0.0.1:$PORT"

health() {
  curl -fsS --max-time 2 "$URL/api/health" >/dev/null 2>&1
}

if [ "$STOP" -eq 1 ]; then
  PID=""
  if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  fi
  if [ -z "${PID:-}" ]; then
    PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  fi
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    if ps -p "$PID" -o command= 2>/dev/null | grep -q "market_mobile.py"; then
      kill "$PID" && echo "Stopped V1 (pid $PID)."
    else
      echo "pid $PID is not V1 (stale pid file), not killing it."
    fi
  else
    echo "V1 is not running on port $PORT."
  fi
  rm -f "$PID_FILE"
  exit 0
fi

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

if health; then
  echo "V1 is already running at $URL"
  [ "$OPEN" -eq 1 ] && open "$URL"
  exit 0
fi

[ "$PORT_SET" -eq 1 ] || ARGS+=("--port" "$PORT")

if [ "$FOREGROUND" -eq 1 ]; then
  exec "$PY" "$ROOT/market_mobile.py" ${ARGS[@]+"${ARGS[@]}"}
fi

mkdir -p "$LOG_DIR"
echo "" >> "$LOG_FILE"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') starting V1 on port $PORT ===" >> "$LOG_FILE"

nohup "$PY" "$ROOT/market_mobile.py" ${ARGS[@]+"${ARGS[@]}"} >> "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

for _ in $(seq 1 30); do
  sleep 0.5
  health && break
  kill -0 "$PID" 2>/dev/null || break
done

if health; then
  echo "V1 started (pid $PID) at $URL"
  echo "Log: $LOG_FILE"
  echo "Stop with: ./start_v1.command --stop"
  [ "$OPEN" -eq 1 ] && open "$URL"
else
  echo "V1 did not start. Last log lines:"
  tail -n 20 "$LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
