#!/bin/bash
# ALFRED GUARDIAN — zero-poll crash supervisor.
#
# Why this can't fry a CPU: while Alfred runs, this script is BLOCKED inside
# `wait` on the child process — the kernel wakes it only when Alfred exits.
# 0.0% CPU, no polling loop, no timers, ~2 MB RAM. (V1's overheating watchdog
# was a busy-poll; this is a supervisor.)
#
# Behaviour:
#   • Alfred quits cleanly (exit 0)      → guardian stays down (you meant it)
#   • Alfred crashes                     → restart after 5 s
#   • 3 crashes inside 5 minutes        → stand down + Telegram alert (crash-loop)
cd "$(dirname "$0")"

BACKOFF=5
WINDOW_START=$(date +%s)
CRASHES=0

echo "[Guardian] 🛡  watching over Alfred (zero-poll)"
while true; do
  .venv/bin/python main.py
  CODE=$?
  if [ $CODE -eq 0 ]; then
    echo "[Guardian] Alfred stood down cleanly — good night."
    exit 0
  fi
  NOW=$(date +%s)
  if [ $((NOW - WINDOW_START)) -gt 300 ]; then
    WINDOW_START=$NOW; CRASHES=0
  fi
  CRASHES=$((CRASHES + 1))
  echo "[Guardian] ⚠️  Alfred exited (code $CODE) — restart #$CRASHES in ${BACKOFF}s"
  if [ $CRASHES -ge 3 ]; then
    echo "[Guardian] ⛔ crash-loop detected — standing down."
    TOKEN=$(python3 -c "import json;print(json.load(open('config/api_keys.json')).get('telegram_bot_token',''))" 2>/dev/null)
    CHAT=$(python3 -c "import json;print(json.load(open('config/api_keys.json')).get('telegram_chat_id',''))" 2>/dev/null)
    if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
      curl -s "https://api.telegram.org/bot$TOKEN/sendMessage" \
           -d chat_id="$CHAT" \
           -d text="⚠️ ALFRED IS DOWN — crash-looped 3x in 5 min. The guardian stood down; check the Mac." >/dev/null
    fi
    exit 1
  fi
  sleep $BACKOFF
done
