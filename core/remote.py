"""
Remote control — operate Alfred from your phone when you're away from the Mac.

Uses a Telegram bot (free, no port-forwarding/tunnel needed): you text the bot,
the message is relayed to Alfred running on your Mac, he executes it and texts the
reply back. The destructive-action guardrail still applies — he'll ask for a "yes"
in the chat before anything irreversible.

Config (config/api_keys.json):
  telegram_bot_token  — from @BotFather (free)
  telegram_chat_id    — your chat id (locks the bot to you). If blank, the bot
                        auto-locks to the FIRST person who messages it.
"""
from __future__ import annotations

import time

try:
    import requests
    _OK = True
except ImportError:
    _OK = False


def _persist_chat_id(chat_id) -> None:
    """Pin the authorised chat to config so the lock survives relaunches."""
    try:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        c = json.loads(p.read_text(encoding="utf-8"))
        if not str(c.get("telegram_chat_id", "")).strip():
            c["telegram_chat_id"] = str(chat_id)
            p.write_text(json.dumps(c, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Remote] could not persist chat lock: {e}")


def _send(base: str, chat_id, text: str) -> None:
    try:
        requests.post(f"{base}/sendMessage",
                      json={"chat_id": chat_id, "text": text[:4000]}, timeout=15)
    except Exception:
        pass


def _download_voice(base: str, file_id: str) -> str | None:
    """Download a Telegram voice/audio file → local .oga path (or None)."""
    try:
        import tempfile
        fp = requests.get(f"{base}/getFile", params={"file_id": file_id}, timeout=15).json()
        path = fp.get("result", {}).get("file_path")
        if not path:
            return None
        tok = base.rsplit("/bot", 1)[-1]
        data = requests.get(f"https://api.telegram.org/file/bot{tok}/{path}", timeout=30).content
        f = tempfile.NamedTemporaryFile(suffix=".oga", delete=False)
        f.write(data); f.close()
        return f.name
    except Exception:
        return None


def _send_voice(base: str, chat_id, mp3_bytes: bytes) -> bool:
    """Send a proper Telegram voice note. Converts MP3 → OGG/Opus via ffmpeg."""
    try:
        import subprocess, tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as m:
            m.write(mp3_bytes); mp3 = m.name
        ogg = mp3[:-4] + ".ogg"
        subprocess.run(["ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "48k", ogg],
                       capture_output=True, timeout=30)
        ok = False
        if os.path.exists(ogg) and os.path.getsize(ogg) > 0:
            with open(ogg, "rb") as vf:
                r = requests.post(f"{base}/sendVoice", data={"chat_id": chat_id},
                                  files={"voice": ("alfred.ogg", vf, "audio/ogg")}, timeout=30)
            ok = r.status_code == 200
        for p in (mp3, ogg):
            try: os.remove(p)
            except Exception: pass
        return ok
    except Exception:
        return False


def run_bridge(token: str, chat_id: str, handler, log=None,
               transcribe=None, synth=None) -> None:
    """Long-poll Telegram. For each authorised message, call handler(text) and
    send its reply back. VOICE BOTH WAYS: if the user sends a voice note it's
    transcribed (via `transcribe(path)->text`), and every reply is also sent as a
    spoken voice note (via `synth(text)->mp3 bytes`). Blocks — run in a thread."""
    if not (_OK and token):
        return
    base = f"https://api.telegram.org/bot{token}"
    authorised = str(chat_id).strip()
    offset = 0
    try:
        # clear backlog so old messages don't replay
        r = requests.get(f"{base}/getUpdates", params={"timeout": 0}, timeout=20)
        ups = r.json().get("result", [])
        if ups:
            offset = ups[-1]["update_id"] + 1
    except Exception:
        pass
    if authorised:
        _send(base, authorised, "🦇 Alfred online — send a command, sir.")
    if log:
        log("SYS: 📱 Remote control active (Telegram).")

    while True:
        try:
            r = requests.get(f"{base}/getUpdates",
                             params={"offset": offset, "timeout": 50}, timeout=60)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                frm = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                # Voice note in → transcribe it into the command text.
                voice = msg.get("voice") or msg.get("audio")
                if not text and voice and transcribe:
                    path = _download_voice(base, voice.get("file_id", ""))
                    if path:
                        try:
                            text = (transcribe(path) or "").strip()
                        except Exception:
                            text = ""
                        if text and str(frm) == str(authorised or frm):
                            _send(base, frm, f"🎙 heard: “{text}”")
                if not text:
                    continue
                # auto-lock to the first sender if no chat_id was configured, and
                # PERSIST it to config so the lock survives every relaunch.
                if not authorised:
                    authorised = frm
                    _persist_chat_id(frm)
                    _send(base, frm, "🔒 Locked to this chat permanently. You're my "
                                     "commander now, sir — no one else can reach me.")
                if frm != authorised:
                    _send(base, frm, "⛔ Not authorised. This bot is locked to its owner.")
                    if log:
                        log(f"SYS: ⛔ Blocked remote command from unauthorised chat {frm}")
                    continue
                try:
                    reply = handler(text) or "Done, sir."
                except Exception as e:
                    reply = f"That failed, sir: {e}"
                _send(base, frm, reply)
                # Voice reply — speak it back as a voice note (best effort).
                if synth and reply:
                    try:
                        audio = synth(reply[:600])
                        if audio:
                            _send_voice(base, frm, audio)
                    except Exception:
                        pass
        except Exception:
            time.sleep(3)
