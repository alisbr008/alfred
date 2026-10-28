"""
Instagram — Alfred posts photos to your Instagram.

Uses instagrapi (unofficial private API — Instagram has no official personal-account
posting API). Session is cached to memory/insta_session.json so we log in ONCE and
reuse it; repeated fresh logins are what trigger Instagram's suspicion.

Config (config/api_keys.json):
  instagram_username / instagram_password

Honest note: unofficial automation carries a small account-flag risk. Posting a few
times a day with a cached session is the pattern thousands use safely; hammering it
is what gets flagged. Alfred posts only when YOU tell him to, behind the confirm
guardrail, so volume stays human.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_SESSION = _base() / "memory" / "insta_session.json"
_client = None          # reused across calls in-process


def _cfg() -> dict:
    try:
        return json.loads((_base() / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _login():
    """Login once, cache the session, reuse forever."""
    global _client
    if _client is not None:
        return _client
    cfg = _cfg()
    user = (cfg.get("instagram_username") or "").strip()
    pw   = (cfg.get("instagram_password") or "").strip()
    if not user or not pw:
        return None
    from instagrapi import Client
    cl = Client()
    cl.delay_range = [1, 3]                      # human-ish pacing
    try:
        if _SESSION.exists():
            cl.load_settings(str(_SESSION))
            cl.login(user, pw)                   # validates the cached session
        else:
            cl.login(user, pw)
        _SESSION.parent.mkdir(parents=True, exist_ok=True)
        cl.dump_settings(str(_SESSION))
        _client = cl
        return cl
    except Exception as e:
        print(f"[Insta] login failed: {e}")
        return None


def _find_image(hint: str | None) -> Path | None:
    """Resolve the image to post: explicit path → staged attachment → newest
    generated/screenshot image on Desktop or in web_ui/generated."""
    if hint:
        p = Path(hint).expanduser()
        if p.exists():
            return p
    cands: list[Path] = []
    for d in (_base() / "web_ui" / "generated", Path.home() / "Desktop"):
        if d.exists():
            cands += [f for f in d.iterdir()
                      if f.suffix.lower() in (".jpg", ".jpeg", ".png") and f.stat().st_size > 20_000]
    if not cands:
        return None
    return max(cands, key=lambda f: f.stat().st_mtime)


def instagram_tool(parameters: dict, response=None, player=None,
                   session_memory=None, jarvis=None, speak=None) -> str:
    """Post a photo to Instagram. parameters: caption, image (optional path)."""
    params  = parameters or {}
    caption = (params.get("caption") or params.get("text") or params.get("content") or "").strip()
    image   = params.get("image") or params.get("path")

    cfg = _cfg()
    if not (cfg.get("instagram_username") and cfg.get("instagram_password")):
        return ("Instagram isn't connected yet, sir — add instagram_username and "
                "instagram_password to the config and I'll handle the rest.")

    # staged 📎 attachment wins if present
    try:
        ui = (getattr(jarvis, "ui", None) if jarvis else None) or player
        att = ui.take_attachment() if (ui and hasattr(ui, "take_attachment")) else None
        if att and str(att.get("path", "")).lower().endswith((".jpg", ".jpeg", ".png")):
            image = att["path"]
    except Exception:
        pass

    img = _find_image(image)
    if img is None:
        return ("I need a photo to post, sir — attach one with the paperclip, or tell me "
                "the file, or have me generate one first.")

    if player:
        try: player.write_log(f"[Insta] Posting {img.name}: {caption[:50]}…")
        except Exception: pass

    cl = _login()
    if cl is None:
        return ("Instagram sign-in failed, sir — check the username and password, and if "
                "Instagram sent you a security prompt, approve it and we'll try again.")

    try:
        # instagrapi wants JPEG; convert PNG transparently
        upload = img
        if img.suffix.lower() == ".png":
            from PIL import Image
            jpg = img.with_suffix(".jpg")
            Image.open(img).convert("RGB").save(jpg, quality=92)
            upload = jpg
        media = cl.photo_upload(str(upload), caption or "")
        code = getattr(media, "code", "")
        link = f" instagram dot com slash p slash {code}" if code else ""
        return f"Posted to Instagram, sir.{(' Link: ' + link) if link else ''}"
    except Exception as e:
        err = str(e)[:160]
        if "challenge" in err.lower():
            return ("Instagram raised a security check, sir — open the app, approve the "
                    "'was this you?' prompt, and I'll post on the next try.")
        return f"Instagram refused the post, sir: {err}"
