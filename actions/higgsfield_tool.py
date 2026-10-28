"""
Higgsfield connector — two ways, best first.

  1. REAL generation via MCP: your Higgsfield MCP is connected inside Claude Code
     (https://mcp.higgsfield.ai/mcp). Since Alfred drives Claude Code, he engineers
     a cinematic prompt and asks Claude Code to generate the video THROUGH the
     Higgsfield MCP, then reports the resulting link. No copy-paste.

  2. Fallback (prompt-paste): if the MCP route isn't available, Alfred writes the
     prompt, copies it to your clipboard, and opens higgsfield.ai so you just paste.

Alfred always writes the prompt himself — you never hand it a raw brief.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

_HIGGSFIELD_URL = "https://higgsfield.ai/"

_SYS = (
    "You are a world-class AI VIDEO/MOTION prompt engineer writing for Higgsfield. "
    "Expand the brief into ONE vivid, cinematic, camera-aware prompt. Specify: subject, "
    "action, camera movement/angle (dolly, orbit, crash-zoom, FPV, etc.), lens, lighting, "
    "mood, colour palette, environment, motion dynamics, and visual style. Be concrete and "
    "filmic. Do NOT restate the brief verbatim. Output ONLY the final prompt, no preamble."
)


def _to_clipboard(text: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5).returncode == 0
    except Exception:
        return False


def _claude_bin() -> str:
    return shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def _show_in_app(ui, url: str, prompt: str) -> bool:
    """Download the generated media and render it inside Alfred's viewer tab."""
    if not (ui and hasattr(ui, "show_visualization")):
        return False
    try:
        import requests
        ext = (re.search(r"\.(mp4|mov|webm|png|jpe?g|webp|gif)", url, re.I) or [None, "mp4"])
        ext = (ext[1] if isinstance(ext, list) else ext.group(1)).lower()
        vtype = "video" if ext in ("mp4", "mov", "webm") else "image"
        base = Path(__file__).resolve().parent.parent
        gen = base / "web_ui" / "generated"; gen.mkdir(parents=True, exist_ok=True)
        fname = f"higgsfield_{int(time.time())}.{ext}"
        data = requests.get(url, timeout=180).content
        (gen / fname).write_bytes(data)
        ui.show_visualization({"type": vtype, "title": "Higgsfield",
                               "data": {"url": f"generated/{fname}", "prompt": prompt}})
        return True
    except Exception as e:
        print(f"[Higgsfield] in-app render failed: {e}")
        return False


def _via_mcp(jarvis) -> bool:
    try:
        cfg = getattr(jarvis, "config", None) or {}
        if str(cfg.get("higgsfield_via_mcp", "true")).lower() in ("false", "0", "no"):
            return False
    except Exception:
        pass
    return Path(_claude_bin()).exists() or bool(shutil.which("claude"))


def _engineer(brief: str) -> str:
    try:
        from core.llm_client import call_llm_text
        crafted = (call_llm_text(f"Brief to engineer into a Higgsfield video prompt:\n{brief}",
                                 system=_SYS, tier="heavy", timeout=60) or "").strip()
        return crafted.strip().strip('"')
    except Exception:
        return brief


def higgsfield_tool(parameters: dict, response=None, player=None,
                    session_memory=None, jarvis=None, speak=None) -> str:
    params = parameters or {}
    brief = (params.get("request") or params.get("brief") or params.get("query")
             or params.get("text") or "").strip()
    if not brief:
        return "What should I create on Higgsfield, sir?"

    ui = (getattr(jarvis, "ui", None) if jarvis else None) or player
    if ui:
        try: ui.write_log("SYS: ✍️ Engineering a Higgsfield prompt…")
        except Exception: pass

    crafted = _engineer(brief)
    if not crafted:
        return "I couldn't compose a Higgsfield prompt for that, sir."
    if ui:
        try: ui.write_log(f"Alfred: [Higgsfield prompt]\n{crafted}")
        except Exception: pass

    # ── Path 1: real generation through Claude Code's Higgsfield MCP ──────────
    if _via_mcp(jarvis) and jarvis is not None:
        _to_clipboard(crafted)   # keep a paste-ready copy as backup

        def _generate():
            if ui:
                try: ui.write_log("SYS: 🎬 Generating on Higgsfield via MCP — this takes a minute…")
                except Exception: pass
            instruction = (
                "Use the Higgsfield MCP tools to generate a video from this exact prompt. "
                "Pick sensible defaults for any required parameters, start the generation, "
                "and report back the resulting video URL (or job link) on its own line, "
                "prefixed with 'RESULT:'.\n\nPROMPT:\n" + crafted
            )
            url = ""
            try:
                r = subprocess.run([_claude_bin(), "-p", instruction,
                                    "--permission-mode", "bypassPermissions"],
                                   capture_output=True, text=True, timeout=600)
                out = (r.stdout or r.stderr or "").strip()
                m = re.search(r"RESULT:\s*(\S+)", out) or re.search(r"https?://\S+", out)
                url = (m.group(1) if m and m.lastindex else (m.group(0) if m else "")).strip()
                if ui:
                    try: ui.write_log(f"Alfred: {out[-800:]}")
                    except Exception: pass
            except Exception as e:
                if ui:
                    try: ui.write_log(f"SYS: ⚠️ Higgsfield MCP run failed: {e}")
                    except Exception: pass
            shown = False
            if url:
                shown = _show_in_app(ui, url, crafted)   # render in Alfred's tab
                if not shown:
                    try: webbrowser.open(url)             # fallback: external
                    except Exception: pass
            jarvis._responding = True
            if shown:
                jarvis.speak("Done, sir — your Higgsfield result is on the dashboard.")
            elif url:
                jarvis.speak("Your Higgsfield result is ready, sir — I've opened the link.")
            else:
                jarvis.speak("I sent it to Higgsfield, sir — the prompt's on your clipboard if "
                             "you'd rather run it on the site.")
            jarvis._finish_turn()

        threading.Thread(target=_generate, daemon=True).start()
        return "On it, sir — Claude Code is generating it on Higgsfield through the MCP now."

    # ── Path 2: prompt-paste fallback ────────────────────────────────────────
    copied = _to_clipboard(crafted)
    try: webbrowser.open(_HIGGSFIELD_URL)
    except Exception: pass
    where = " It's on your clipboard and Higgsfield is open — just paste and generate." if copied \
            else " Higgsfield is open — here's the prompt to paste."
    return f"Here's the Higgsfield prompt I wrote for you, sir.{where}\n\n{crafted}"
