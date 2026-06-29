#!/bin/bash
# Startet das Storyboard-Dashboard und öffnet den Browser.
cd "$(dirname "$0")"
PORT="${1:-8000}"

# evtl. alte Instanz auf dem Port beenden
lsof -ti tcp:"$PORT" | xargs kill -9 2>/dev/null

echo "Starte Dashboard auf http://localhost:$PORT …"
python3 dashboard.py --port "$PORT" &
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
