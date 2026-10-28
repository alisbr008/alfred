"""Music control for Alfred — Apple Music (via AppleScript) + YouTube Music for searches."""
import subprocess
import webbrowser
from urllib.parse import quote_plus


def _osa(script: str):
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=8)


def play_music(parameters: dict, player=None) -> str:
    action = (parameters.get("action") or "play").strip().lower()
    query  = (parameters.get("query") or "").strip()

    def _log(msg: str):
        if player:
            try:
                player.write_log(f"SYS: ♪ {msg}")
            except Exception:
                pass

    try:
        if action in ("pause", "stop"):
            _osa('tell application "Music" to pause')
            _log("paused"); return "Paused, sir."
        if action in ("resume",):
            _osa('tell application "Music" to play')
            _log("resumed"); return "Resuming, sir."
        if action in ("next", "skip"):
            _osa('tell application "Music" to next track')
            _log("next track"); return "Next track, sir."
        if action in ("previous", "prev", "back"):
            _osa('tell application "Music" to previous track')
            _log("previous track"); return "Going back, sir."

        # play — actually PLAY via YouTube (scrape top result → open watch page, autoplays)
        q = query or "lofi hip hop radio beats to relax study to"
        try:
            from actions.youtube_video import _scrape_first_video_url, _open_url
            url = _scrape_first_video_url(q)
            if url:
                if "watch?v=" in url and "autoplay" not in url:
                    url += "&autoplay=1"
                _open_url(url)
                _log(f"playing {q}")
                return f"Putting on {query or 'some music'}, sir."
        except Exception:
            pass
        # fallback: resume Apple Music
        _osa('tell application "Music" to play')
        _log("playing"); return "Playing your music, sir."
    except Exception as e:
        msg = f"Sir, I couldn't control the music: {e}"
        _log("error"); return msg
