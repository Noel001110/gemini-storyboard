#!/bin/bash
# Generiert alle Frames aus scenes.tsv. Überspringt bereits vorhandene PNGs (Resume-fähig).
cd "$(dirname "$0")"
LOG="batch.log"
LIMIT="${LIMIT:-0}"   # 0 = alle; sonst nur die ersten N Zeilen
SRC=scenes.tsv
if [ "$LIMIT" -gt 0 ]; then SRC=$(mktemp); head -n "$LIMIT" scenes.tsv > "$SRC"; fi
total=$(wc -l < "$SRC" | tr -d ' ')
i=0; done=0; skip=0; fail=0
echo "=== Batch-Start $(date '+%H:%M:%S') — $total Frames ===" | tee -a "$LOG"
while IFS=$'\t' read -r ts prompt; do
  i=$((i+1))
  out="${ts}.png"
  if [ -s "$out" ]; then
    skip=$((skip+1)); echo "[$i/$total] SKIP $out (existiert)" | tee -a "$LOG"; continue
  fi
  echo "[$i/$total] GEN $out ..." | tee -a "$LOG"
  if python3 gen.py "$out" "$prompt" >> "$LOG" 2>&1; then
    done=$((done+1)); echo "[$i/$total] OK $out" | tee -a "$LOG"
  else
    rc=$?; fail=$((fail+1))
    echo "[$i/$total] FAIL $out (exit $rc)" | tee -a "$LOG"
    if [ "$rc" = "3" ]; then
      echo "!!! Rate-Limit erreicht — Batch gestoppt bei $out. Später erneut starten zum Fortsetzen." | tee -a "$LOG"
      break
    fi
  fi
  sleep 3
done < "$SRC"
echo "=== Fertig $(date '+%H:%M:%S') — neu:$done übersprungen:$skip fehlgeschlagen:$fail ===" | tee -a "$LOG"
