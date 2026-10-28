"""
Prompt Engineer — Alfred WRITES the prompt himself instead of parroting your
brief. You give him a rough idea + a target tool (Higgsfield video, Lovable app
builder, an image generator, etc.) and he expands it into a detailed, optimised,
paste-ready prompt — then copies it to your clipboard.
"""
from __future__ import annotations

import subprocess
import sys


def _to_clipboard(text: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        p = subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5)
        return p.returncode == 0
    except Exception:
        return False


# target → (label, expert system prompt)
_TARGETS = {
    "video": ("Higgsfield / AI video", (
        "You are a world-class AI VIDEO prompt engineer (Higgsfield, Runway, Kling). "
        "Expand the brief into ONE vivid, cinematic prompt. Specify: subject, action, "
        "camera movement and angle, lens, lighting, mood, colour palette, environment, "
        "and visual style. Be concrete and filmic. Do NOT restate the brief verbatim — "
        "engineer it. Output ONLY the final prompt, no preamble.")),
    "app": ("Lovable / AI app builder", (
        "You are an expert at briefing AI app/website builders (Lovable, v0, Bolt). "
        "Turn the brief into ONE clear, structured build prompt covering: what the product "
        "does and for whom, the key pages/screens, the core features and user flows, the "
        "data it needs, and the design direction (style, vibe, colours, feel). Be specific "
        "enough to build from. Output ONLY the prompt, ready to paste.")),
    "image": ("image generation", (
        "You are a world-class IMAGE prompt engineer (Midjourney, DALL·E, SDXL). Expand the "
        "brief into ONE detailed visual prompt: subject, composition, style/medium, lighting, "
        "colour, mood, lens/detail modifiers. Concrete and evocative. Do NOT just restate the "
        "brief. Output ONLY the prompt.")),
    "general": ("general", (
        "You are a world-class prompt engineer. Turn the brief into ONE precise, detailed, "
        "well-structured prompt that will get excellent results from a capable AI. Add the "
        "specifics, constraints and framing that make a prompt work. NEVER simply restate the "
        "request. Output ONLY the engineered prompt.")),
}


def _detect_target(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("higgsfield", "video", "cinematic", "clip", "runway", "kling", "reel", "animation")):
        return "video"
    if any(k in t for k in ("lovable", "website", "web app", "webapp", "landing page", "app builder",
                            "build a site", "build an app", "v0", "bolt", "saas")):
        return "app"
    if any(k in t for k in ("image", "midjourney", "dall", "photo", "logo", "poster", "illustration",
                            "thumbnail", "art", "stable diffusion")):
        return "image"
    return "general"


def prompt_engineer(parameters: dict, response=None, player=None,
                    session_memory=None, jarvis=None, speak=None) -> str:
    params = parameters or {}
    brief  = (params.get("request") or params.get("brief") or params.get("query")
              or params.get("text") or "").strip()
    target = (params.get("target") or "").lower().strip() or _detect_target(brief)
    if target not in _TARGETS:
        target = _detect_target(target + " " + brief)
    if not brief:
        return "What should I write a prompt for, sir?"

    label, system = _TARGETS[target]
    if player:
        try: player.write_log(f"SYS: ✍️ Engineering a {label} prompt…")
        except Exception: pass

    try:
        from core.llm_client import call_llm_text
        crafted = (call_llm_text(f"Brief to engineer into a {label} prompt:\n{brief}",
                                 system=system, tier="heavy") or "").strip()
    except Exception as e:
        return f"I couldn't write that prompt, sir: {e}"
    if not crafted:
        return "I couldn't compose a prompt for that, sir."

    crafted = crafted.strip().strip('"')
    copied = _to_clipboard(crafted)
    if player:
        try: player.write_log(f"Alfred: [{label} prompt]\n{crafted}")
        except Exception: pass

    # The spoken line stays short; the full prompt is on the clipboard + on screen.
    where = " I've copied it to your clipboard — paste it straight in." if copied else ""
    return f"Here's a {label} prompt I wrote for you, sir.{where}\n\n{crafted}"
