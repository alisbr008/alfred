"""
Presence — Alfred notices when you leave and when you come back.

Uses macOS's own HID idle counter (ioreg), sampled every 30 seconds on a sleeping
thread — one instant subprocess call per sample, no busy-poll, no camera, ~0% CPU.
(The Air must never heat up for ambient awareness.)

Behaviour:
  • idle ≥ 15 min  → AWAY: state chip flips, world refresh keeps running silently
  • activity again → "Welcome back, sir." (skipped during NIGHT WATCH — wake-word
    only at night — and never interrupts a turn in progress)
"""
from __future__ import annotations

import subprocess
import threading
import time

AWAY_AFTER  = 15 * 60     # seconds of idle before he considers you gone
_CHECK_EVERY = 30


def idle_seconds() -> float:
    """System-wide seconds since last keyboard/mouse/trackpad input."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                return int(line.split("=")[-1].strip()) / 1_000_000_000
    except Exception:
        pass
    return 0.0


def start_presence_loop(jarvis) -> None:
    def _loop():
        away = False
        away_since = 0.0
        while True:
            try:
                idle = idle_seconds()
                if not away and idle >= AWAY_AFTER:
                    away, away_since = True, time.time() - idle
                    try:
                        jarvis.ui.write_log("SYS: 👁 Presence — he's stepped away.")
                        if not jarvis._responding:
                            jarvis.ui.set_state("AWAY")
                    except Exception:
                        pass
                elif away and idle < 10:
                    away = False
                    gone_min = int((time.time() - away_since) / 60)
                    try:
                        jarvis.ui.write_log(f"SYS: 👁 Presence — he's back (away ~{gone_min} min).")
                        if not jarvis._responding:
                            jarvis.ui.set_state("LISTENING")
                    except Exception:
                        pass
                    # Greet — but never during night watch (wake-word only) and
                    # never over an in-progress turn.
                    if (not getattr(jarvis, "_night_mode", False)
                            and not jarvis._responding and gone_min >= 20):
                        jarvis._responding = True
                        jarvis.speak("Welcome back, sir.")
                        jarvis._finish_turn()
            except Exception as e:
                print(f"[Presence] {e}")
            time.sleep(_CHECK_EVERY)

    threading.Thread(target=_loop, daemon=True).start()
