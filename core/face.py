"""
Face familiarity — Alfred learns what you look like so he recognises you.

Not biometric verification (that needs heavy models Ali doesn't want on the Air).
Instead: on "remember how I look", Alfred captures a webcam frame, the vision model
writes a concise appearance profile, and it's stored + injected into his prompt. So
when he looks at you later he can say "yes sir, that's you" and reference how you
look — genuine familiarity, zero extra dependencies.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_FACE = _base() / "memory" / "user_appearance.json"
_REF  = _base() / "memory" / "user_face.jpg"


def enroll(description: str, b64: str | None = None) -> None:
    """Store the appearance profile (+ a reference frame) so Alfred knows you."""
    description = (description or "").strip()
    if not description:
        return
    try:
        _FACE.parent.mkdir(parents=True, exist_ok=True)
        _FACE.write_text(json.dumps({"description": description[:600],
                                     "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
        if b64:
            _REF.write_bytes(base64.b64decode(b64))
    except Exception:
        pass


def is_enrolled() -> bool:
    return _FACE.exists()


def appearance() -> str:
    try:
        return json.loads(_FACE.read_text(encoding="utf-8")).get("description", "")
    except Exception:
        return ""


def appearance_note() -> str:
    """One line for the system prompt so Alfred knows the user by sight."""
    d = appearance()
    if not d:
        return ""
    return ("[YOU KNOW THE USER BY SIGHT]\n"
            f"The user (Ali) looks like this: {d}\n"
            "When you look through the camera and see someone matching this, greet him warmly "
            "by acknowledging it's him. If it clearly is NOT him, say so politely.")
