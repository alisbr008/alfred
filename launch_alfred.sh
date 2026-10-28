#!/bin/bash
# Summon Alfred (⌘⇧A). Launches him INSIDE Terminal running the guardian, so:
#   • macOS attributes Accessibility / Screen Recording to Terminal (stable, reliable
#     — a detached `nohup python &` can't hold those permissions)
#   • the guardian watchdog restarts him if he crashes
#   • you get a live log window for demos (minimise it if you like)
# If he's already running, just focus him instead of starting a second copy.

if pgrep -f "AlfredV2/.venv/bin/python main.py" >/dev/null 2>&1; then
  osascript -e 'tell application "Terminal" to activate' >/dev/null 2>&1
else
  osascript \
    -e 'tell application "Terminal" to do script "cd ~/AlfredV2 && ./guardian.sh"' \
    -e 'tell application "Terminal" to activate' >/dev/null 2>&1
fi
