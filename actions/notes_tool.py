"""Apple Notes for Alfred via AppleScript — create and read notes."""
import subprocess


def _osa(script: str, timeout: int = 25) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "AppleScript error").strip())
    return r.stdout.strip()


def notes_tool(parameters: dict, player=None) -> str:
    action = (parameters.get("action") or "create").strip().lower()
    title  = (parameters.get("title") or "Alfred Note").replace('"', "'")
    body   = (parameters.get("content") or parameters.get("body") or "").replace('"', "'")

    try:
        if action in ("read", "get", "open"):
            script = f'''
            tell application "Notes"
              set b to body of (first note whose name is "{title}")
            end tell
            return b
            '''
            out = _osa(script)
            import re
            text = re.sub(r"<[^>]+>", " ", out)
            return f"'{title}', sir:\n{text.strip()[:600]}" if text.strip() else f"'{title}' is empty, sir."

        if action in ("list",):
            script = '''
            set out to ""
            tell application "Notes"
              set k to 0
              repeat with n in notes
                set out to out & "• " & (name of n) & linefeed
                set k to k + 1
                if k ≥ 12 then exit repeat
              end repeat
            end tell
            return out
            '''
            out = _osa(script)
            return "Your notes, sir:\n" + (out.strip() or "none")

        # create (default)
        html = body.replace("\n", "<br>")
        script = f'''
        tell application "Notes"
          make new note with properties {{name:"{title}", body:"<div><b>{title}</b></div><div>{html}</div>"}}
        end tell
        return "created"
        '''
        _osa(script)
        return f"Created the note '{title}', sir."
    except subprocess.TimeoutExpired:
        return "Sir, Notes took too long to respond."
    except Exception as e:
        return f"Sir, I couldn't access Notes: {e}"
