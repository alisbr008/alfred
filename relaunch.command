#!/bin/bash
# Full relaunch — kills any running Alfred process, then starts a fresh one.
cd "$HOME/AlfredV2" || exit 1
echo "Stopping any running Alfred…"
pkill -f "AlfredV2/.venv/bin/python" 2>/dev/null
pkill -f "AlfredV2.*main.py"          2>/dev/null
sleep 1
echo "Launching Alfred…"
exec .venv/bin/python main.py
