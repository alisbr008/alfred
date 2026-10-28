#!/bin/bash
# Installs the tripwire daemon under launchd: fires every 10 min, Alfred running or not.
PLIST=~/Library/LaunchAgents/com.velthar.alfred.tripwires.plist
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.velthar.alfred.tripwires</string>
  <key>ProgramArguments</key><array>
    <string>$HOME/AlfredV2/.venv/bin/python</string>
    <string>$HOME/AlfredV2/tripwire_daemon.py</string>
  </array>
  <key>StartInterval</key><integer>600</integer>
  <key>StandardOutPath</key><string>/tmp/alfred_tripwires.log</string>
  <key>StandardErrorPath</key><string>/tmp/alfred_tripwires.log</string>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null; launchctl load "$PLIST"
echo "✅ Tripwire daemon installed — checks every 10 min, even with Alfred closed."
echo "   Log: /tmp/alfred_tripwires.log   ·   Uninstall: launchctl unload $PLIST"
read -n 1 -p "Press any key to close…"
