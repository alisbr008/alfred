"""Apple Calendar for Alfred via AppleScript — read today's schedule + add events."""
import subprocess


def _osa(script: str, timeout: int = 30) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "AppleScript error").strip())
    return r.stdout.strip()


_TODAY = '''
set out to ""
set d0 to current date
set hours of d0 to 0
set minutes of d0 to 0
set seconds of d0 to 0
set d1 to d0 + (1 * days)
tell application "Calendar"
  repeat with c in calendars
    repeat with e in (every event of c whose start date ≥ d0 and start date < d1)
      set t to (start date of e) as string
      set out to out & "• " & (summary of e) & " — " & t & linefeed
    end repeat
  end repeat
end tell
return out
'''


def calendar_tool(parameters: dict, player=None) -> str:
    action = (parameters.get("action") or "today").strip().lower()
    try:
        if action == "add":
            title = (parameters.get("title") or "Event").replace('"', "'")
            when  = (parameters.get("date") or "").strip()      # e.g. "tomorrow 3pm" handled by caller upstream
            # Expect an ISO-ish or natural date already resolved to a start; default +1h block.
            day   = (parameters.get("start") or when or "").replace('"', "'")
            script = f'''
            tell application "Calendar"
              tell calendar 1
                set s to (current date)
                make new event with properties {{summary:"{title}", start date:s, end date:s + (60 * minutes)}}
              end tell
            end tell
            return "added"
            '''
            _osa(script)
            return f"Added '{title}' to your calendar, sir."

        out = _osa(_TODAY, timeout=40)
        if not out.strip():
            return "Your calendar is clear today, sir."
        return "Today's schedule, sir:\n" + out.strip()
    except subprocess.TimeoutExpired:
        return "Sir, the calendar took too long to respond."
    except Exception as e:
        return f"Sir, I couldn't read your calendar: {e}"
