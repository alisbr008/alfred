"""
Replicate — Alfred GENERATES media (images now, video too) via one API. He
engineers the prompt himself, runs the model, saves the result, shows it on the
dashboard, and drops a copy on your Desktop.

Config: replicate_api_token  (from replicate.com/account/api-tokens)
"""
from __future__ import annotations

import json
import sys
import time
import threading
from pathlib import Path

try:
    import requests
    _REQ = True
except ImportError:
    _REQ = False


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    try:
        return json.loads((_base() / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:40] or "art"


_MODELS = {                       # type → (owner/name, build-input fn, ext, poll?)
    "image": ("black-forest-labs/flux-schnell",
              lambda p: {"prompt": p, "aspect_ratio": "16:9", "output_format": "png",
                         "num_outputs": 1}, "png", False),
    "video": ("minimax/video-01",
              lambda p: {"prompt": p}, "mp4", True),
}


def _run(model: str, inp: dict, key: str, poll: bool) -> str | None:
    """Run a Replicate model. Returns the first output URL or None."""
    if not _REQ or not key:
        return None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"https://api.replicate.com/v1/models/{model}/predictions"
    try:
        if not poll:
            headers["Prefer"] = "wait"
            r = requests.post(url, headers=headers, json={"input": inp}, timeout=90)
            r.raise_for_status()
            out = r.json().get("output")
            return out[0] if isinstance(out, list) and out else (out if isinstance(out, str) else None)
        # video: submit then poll
        r = requests.post(url, headers=headers, json={"input": inp}, timeout=30)
        r.raise_for_status()
        get_url = r.json().get("urls", {}).get("get")
        for _ in range(80):                       # up to ~4 min
            time.sleep(3)
            s = requests.get(get_url, headers=headers, timeout=20).json()
            st = s.get("status")
            if st == "succeeded":
                out = s.get("output")
                return out[0] if isinstance(out, list) and out else (out if isinstance(out, str) else None)
            if st in ("failed", "canceled"):
                return None
    except Exception as e:
        print(f"[Replicate] error: {e}")
    return None


def _download(url: str, path: Path) -> bool:
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
        return path.stat().st_size > 500
    except Exception as e:
        print(f"[Replicate] download failed: {e}")
        return False


def _engineer(brief: str, kind: str) -> str:
    """Alfred writes a strong prompt instead of using the raw brief."""
    try:
        from actions.prompt_engineer import prompt_engineer
        r = prompt_engineer({"request": brief, "target": "video" if kind == "video" else "image"})
        # strip the spoken preamble, keep the prompt body (after the blank line)
        return r.split("\n\n", 1)[-1].strip() if "\n\n" in r else brief
    except Exception:
        return brief


def replicate_generate(parameters: dict, response=None, player=None,
                       session_memory=None, jarvis=None, speak=None) -> str:
    params = parameters or {}
    brief  = (params.get("request") or params.get("prompt") or params.get("text") or "").strip()
    kind   = (params.get("type") or "image").lower().strip()
    if kind not in _MODELS:
        kind = "video" if "video" in brief.lower() else "image"
    if not brief:
        return "What should I create, sir?"

    key = _cfg().get("replicate_api_token", "").strip()
    if not key:
        return ("Replicate isn't connected yet, sir — add your 'replicate_api_token' to "
                "the config and I'll generate it.")

    model, build_input, ext, poll = _MODELS[kind]
    prompt = _engineer(brief, kind)         # Alfred writes the prompt himself

    if player:
        try: player.write_log(f"SYS: 🎨 Generating {kind} — {brief[:50]}…")
        except Exception: pass
    ui = (getattr(jarvis, "ui", None) if jarvis else None) or player
    if ui is not None and hasattr(ui, "show_visualization_loading"):
        ui.show_visualization_loading(brief.title(), f"GENERATING {kind.upper()}")

    def _do():
        out_url = _run(model, build_input(prompt), key, poll)
        if not out_url:
            return None
        gen_dir = _base() / "web_ui" / "generated"; gen_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{_slug(brief)}_{int(time.time())}.{ext}"
        path = gen_dir / fname
        if not _download(out_url, path):
            return None
        # drop a copy on the Desktop
        try:
            import shutil
            shutil.copy(path, Path.home() / "Desktop" / fname)
        except Exception:
            pass
        rel = f"generated/{fname}"
        if ui is not None and hasattr(ui, "show_visualization"):
            vtype = "video" if kind == "video" else "image"
            ui.show_visualization({"type": vtype, "title": brief.title(),
                                   "data": {"url": rel, "prompt": prompt}})
        return fname

    if poll:                                 # video: run in background, push when ready
        threading.Thread(target=_do, daemon=True).start()
        return f"Generating your {kind} now, sir — it'll appear on the dashboard shortly."
    fname = _do()
    if not fname:
        return f"I couldn't generate that {kind}, sir."
    return f"Done, sir — your {kind} is on the dashboard and saved to your Desktop."
