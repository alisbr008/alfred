#!/usr/bin/env python3
"""Tripwire daemon — run by launchd every 10 minutes, EVEN WHEN ALFRED IS CLOSED.
Runs the checks once and exits (launchd schedules it — no resident process, no heat)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from actions.tripwires import check_all
fired = check_all(source="daemon")
print(f"[tripwire-daemon] checked — {len(fired)} fired")
