"""
Contextual awareness — Alfred sees what you're doing (not spying, awareness):
frontmost app, open apps, the active document, and the next calendar event with
minutes-until. Powers proactive nudges like:

    "Ali, your Zoom starts in 8 minutes. The presentation is already open."

All via AppleScript / osascript — no extra deps.
"""
from __future__ import annotations

import subprocess


def _osa(script: str, timeout: int = 6) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def frontmost_app() -> str:
    return _osa('tell application "System Events" to get name of first '
                'application process whose frontmost is true')


def active_document() -> str:
    """Best-effort: the document/window title of the frontmost app."""
    return _osa(
        'tell application "System Events"\n'
        ' set p to first application process whose frontmost is true\n'
        ' try\n  return name of front window of p\n on error\n  return ""\n end try\n'
        'end tell')


def open_apps(limit: int = 12) -> list:
    out = _osa('tell application "System Events" to get name of every '
               'application process whose background only is false')
    apps = [a.strip() for a in out.split(",") if a.strip()]
    skip = {"Finder", "System Events"}
    return [a for a in apps if a not in skip][:limit]


def next_event() -> dict | None:
    """The next upcoming calendar event within 12h → {minutes, summary}."""
    script = (
        'set n to current date\n'
        'set d1 to n + (12 * hours)\n'
        'set bestT to d1\n'
        'set bestS to "NONE"\n'
        'tell application "Calendar"\n'
        ' repeat with c in calendars\n'
        '  repeat with e in (every event of c whose start date ≥ n and start date < d1)\n'
        '   if (start date of e) < bestT then\n'
        '    set bestT to start date of e\n'
        '    set bestS to summary of e\n'
        '   end if\n'
        '  end repeat\n'
        ' end repeat\n'
        'end tell\n'
        'if bestS is "NONE" then\n return "NONE"\n else\n'
        ' return ((round ((bestT - n) / 60)) as string) & "|" & bestS\n end if')
    out = _osa(script, timeout=10)
    if not out or out == "NONE" or "|" not in out:
        return None
    mins, _, summ = out.partition("|")
    try:
        return {"minutes": int(mins), "summary": summ.strip()}
    except Exception:
        return None


def snapshot() -> dict:
    return {
        "frontmost": frontmost_app(),
        "document": active_document(),
        "open_apps": open_apps(),
        "next_event": next_event(),
    }


def snapshot_spoken() -> str:
    s = snapshot()
    parts = []
    if s["frontmost"]:
        d = f" — {s['document']}" if s["document"] else ""
        parts.append(f"You're in {s['frontmost']}{d}.")
    if s["open_apps"]:
        parts.append("Open: " + ", ".join(s["open_apps"][:6]) + ".")
    ev = s["next_event"]
    if ev:
        parts.append(f"Next up: {ev['summary']} in {ev['minutes']} minutes.")
    return " ".join(parts) or "Nothing notable on screen, sir."


# ── proactive meeting nudge ──────────────────────────────────────────────────

_announced: set = set()


def due_meeting_alert(within_min: int = 10) -> str | None:
    """If a meeting starts within `within_min`, return a one-time spoken nudge.
    Mentions an already-open relevant doc if it can spot one."""
    ev = next_event()
    if not ev or ev["minutes"] < 0 or ev["minutes"] > within_min:
        return None
    key = f"{ev['summary']}|{ev['minutes']//1}"
    # de-dupe per (event, ~minute bucket) so it nudges roughly once
    bucket = f"{ev['summary']}"
    if bucket in _announced:
        return None
    _announced.add(bucket)
    msg = f"Sir, {ev['summary']} starts in {ev['minutes']} minutes."
    # if a relevant doc/app is open, say so
    try:
        apps = open_apps()
        if any(a in ("zoom.us", "Zoom", "Google Meet", "Microsoft Teams") for a in apps):
            msg += " Your meeting app is already open."
    except Exception:
        pass
    return msg


def reset_announced() -> None:
    _announced.clear()
