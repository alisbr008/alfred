"""
Permission helper — Alfred's REAL-HANDS & VISION permissions.

There are FOUR separate macOS privacy grants, and they are easy to confuse:
  • Accessibility     → move mouse / click / type          (pyautogui)
  • Automation        → read on-screen buttons by name      (osascript → System Events)  ← the one everyone misses
  • Screen Recording  → screenshot / screen vision
  • Camera            → webcam vision ("see me")

`check_permissions()` actually TESTS each one from Alfred's own process, so he can
tell you precisely which is blocked instead of you guessing. `open_permission_panes`
opens exactly the panes that are missing.

All must be granted to whichever app launches Alfred — you launch via Terminal
(⌘⇧A → guardian in Terminal), so grant Terminal. Restart Alfred after granting.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

_PANE = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "automation":    "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
    "screen":        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "camera":        "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
}
_LABEL = {"accessibility": "clicking & typing", "automation": "reading on-screen buttons",
          "screen": "seeing your screen", "camera": "the webcam"}


def _open(url: str) -> None:
    try:
        subprocess.Popen(["open", url])
    except Exception:
        pass


def check_permissions() -> dict:
    """Live test of each permission FROM ALFRED'S PROCESS. True = granted."""
    res: dict = {}

    # Accessibility — the OS's own trust check
    try:
        import ctypes, ctypes.util
        a = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        a.AXIsProcessTrusted.restype = ctypes.c_bool
        res["accessibility"] = bool(a.AXIsProcessTrusted())
    except Exception:
        res["accessibility"] = None

    # Automation — can we drive System Events? (-1743 = not authorised)
    try:
        r = subprocess.run(["osascript", "-e",
                            'tell application "System Events" to return name of first process'],
                           capture_output=True, text=True, timeout=8)
        res["automation"] = (r.returncode == 0 and "-1743" not in (r.stderr or ""))
    except Exception:
        res["automation"] = None

    # Screen Recording — a real capture with real bytes
    try:
        p = tempfile.mktemp(suffix=".jpg")
        subprocess.run(["screencapture", "-x", "-t", "jpg", p], capture_output=True, timeout=8)
        res["screen"] = os.path.exists(p) and os.path.getsize(p) > 8000
        try: os.remove(p)
        except Exception: pass
    except Exception:
        res["screen"] = None

    # Camera — open device 0 and grab a real frame
    try:
        import cv2
        cap = cv2.VideoCapture(0)
        ok = cap.isOpened()
        if ok:
            for _ in range(4):
                cap.read()
            got, _frame = cap.read()
            ok = bool(got)
        cap.release()
        res["camera"] = ok
    except Exception:
        res["camera"] = None
    return res


def permissions_report(parameters=None, response=None, player=None,
                       session_memory=None, jarvis=None, speak=None) -> str:
    r = check_permissions()
    ok = [k for k, v in r.items() if v]
    missing = [k for k, v in r.items() if not v]
    if player:
        try:
            player.write_log("SYS: 🔑 Permissions — " +
                             " ".join(f"{k}:{'✓' if v else '✗'}" for k, v in r.items()))
        except Exception:
            pass
    if not missing:
        return "All permissions are granted, sir — I have full hands and eyes."
    for k in missing:
        _open(_PANE[k])
    names = ", ".join(_LABEL[k] for k in missing)
    return (f"I'm still missing permission for {names}, sir. I've opened the exact panes — "
            "enable Terminal in each (Automation is a separate list from Accessibility, that's "
            "the one usually missed), then relaunch me.")


def open_permission_panes(parameters=None, response=None, player=None,
                          session_memory=None, jarvis=None, speak=None) -> str:
    import time as _t
    for i, key in enumerate(("accessibility", "automation", "screen", "camera")):
        _open(_PANE[key])
        _t.sleep(0.9)
    if player:
        try:
            player.write_log("SYS: 🔑 Opened Accessibility · Automation · Screen Recording · Camera. "
                             "Enable Terminal in ALL FOUR, then relaunch me.")
        except Exception:
            pass
    return ("I've opened all four privacy panes, sir — Accessibility, Automation, Screen Recording "
            "and Camera. Switch Terminal on in each — Automation is the one most people miss — then "
            "relaunch me and I'll have full hands and eyes.")
