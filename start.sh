#!/bin/bash
# Startet das Storyboard-Dashboard und öffnet den Browser.
cd "$(dirname "$0")"
# 8010 statt 8000: Docker Desktop (com.docker.backend) lauscht auf *:8000 per IPv6,
# während dieser Server nur IPv4/127.0.0.1 bindet. Löst der Browser "localhost" zu ::1
# auf, landet die Anfrage bei Docker (nacktes 404) statt hier — sah aus wie ein Absturz.
PORT="${1:-8010}"

# evtl. alte Instanz auf dem Port beenden
lsof -ti tcp:"$PORT" | xargs kill -9 2>/dev/null

PYTHON_BIN="python3"
if [ -f "./.venv_whisper/bin/python" ]; then
  PYTHON_BIN="./.venv_whisper/bin/python"
elif [ -f "./.venv/bin/python" ]; then
  PYTHON_BIN="./.venv/bin/python"
fi

echo "Starte Dashboard auf http://localhost:$PORT mit $PYTHON_BIN …"
"$PYTHON_BIN" dashboard.py --port "$PORT" &
PID=$!

# warten bis der Server antwortet, dann Browser öffnen
for i in $(seq 1 20); do
  if curl -s "http://localhost:$PORT/" >/dev/null 2>&1; then break; fi
  sleep 0.3
done
open "http://localhost:$PORT"

echo "Dashboard läuft (PID $PID). Strg+C beendet es."
trap "kill $PID 2>/dev/null" INT TERM
wait $PID
