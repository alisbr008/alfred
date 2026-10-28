"""
Runtime playtest — Alfred actually RUNS a web build in a headless browser and
LOOKS at it, so a black screen or a runtime error can never pass as "playable".

It captures:
  • uncaught JS exceptions (pageerror)  ← the usual cause of a black screen
  • console.error messages
  • whether the game canvas actually rendered anything (not a blank/near-black frame)
  • whether the frame changes over time (a live game animates; a crashed one freezes)

Returns a dict the executor feeds straight back to Claude Code as concrete,
reproducible bugs to fix — not opinions, the actual errors.
"""
from __future__ import annotations

from pathlib import Path


def _frame_stats(png_path: str):
    """(std, mean) of the screenshot — near-zero std ⇒ uniform/blank frame."""
    try:
        from PIL import Image
        import numpy as np
        im = Image.open(png_path).convert("RGB")
        a = np.asarray(im, dtype=np.float32)
        return float(a.std()), float(a.mean())
    except Exception:
        return None, None


# Harmless errors that only appear because we drive the page headless/without a real
# user gesture — NOT game bugs. Filtered so they never trigger a false repair loop.
_BENIGN = (
    "pointer lock", "pointerlock", "requestpointerlock", "not valid for pointer lock",
    "audiocontext", "was not allowed to start", "play() failed", "the play() request",
    "user gesture", "user activation", "favicon", "resizeobserver loop",
    "net::err_", "failed to load resource: the server responded with a status of 404",
    "preload", "is not allowed by access-control",
)


def _real_errors(seq):
    seen, out = set(), []
    for s in seq:
        s = (s or "").strip()
        low = s.lower()
        if not s or s in seen or any(b in low for b in _BENIGN):
            continue
        seen.add(s); out.append(s)
    return out


def _dedupe(seq):
    seen, out = set(), []
    for s in seq:
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s); out.append(s)
    return out


def playtest(index_html: Path, timeout_ms: int = 9000) -> dict:
    """Load index_html headless, try to start the game, and report what's broken."""
    res = {"ran": False, "errors": [], "blank": False, "frozen": False, "has_canvas": False,
           "shot": None, "summary": ""}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        res["errors"] = [f"playwright unavailable: {e}"]
        return res

    errors: list = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--use-gl=swiftshader",
                                                             "--enable-webgl", "--ignore-gpu-blocklist"])
            page = browser.new_page(viewport={"width": 1024, "height": 700})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                page.goto(index_html.as_uri(), wait_until="load", timeout=timeout_ms)
            except Exception as e:
                errors.append(f"page failed to load: {e}")
            page.wait_for_timeout(700)

            # Kick the game into motion — many games wait for a click / Space / Enter to
            # start (Geometry Dash, Flappy, runners). Try hard: click start controls, then
            # hammer the common start/jump keys with the canvas focused.
            for sel in ("button", "#start", ".start-btn", ".start", "[data-start]",
                        "#play", ".play", "canvas", "body"):
                try:
                    el = page.query_selector(sel)
                    if el:
                        el.click(timeout=500)
                        page.wait_for_timeout(150)
                        break
                except Exception:
                    pass
            try:
                page.mouse.move(512, 350)
                page.mouse.click(512, 350)
                for _ in range(2):
                    for key in ("Space", "Enter", "ArrowUp", "w"):
                        page.keyboard.press(key)
                        page.wait_for_timeout(60)
                page.mouse.click(512, 350)      # click again (jump / confirm)
            except Exception:
                pass

            res["has_canvas"] = bool(page.query_selector("canvas"))
            target = page.query_selector("canvas")

            shot1 = index_html.parent / ".alfred_pt1.png"
            shot2 = index_html.parent / ".alfred_pt2.png"
            page.wait_for_timeout(400)          # let the first frame settle after starting
            try:
                (target or page).screenshot(path=str(shot1))
            except Exception:
                page.screenshot(path=str(shot1))
            page.mouse.click(512, 350)          # keep it moving between the two frames
            page.wait_for_timeout(1100)         # let the animation advance
            try:
                (target or page).screenshot(path=str(shot2))
            except Exception:
                page.screenshot(path=str(shot2))
            res["shot"] = str(shot2)

            # Blank / frozen analysis on the game canvas (if any).
            if res["has_canvas"]:
                s1, _ = _frame_stats(str(shot1))
                s2, m2 = _frame_stats(str(shot2))
                if s2 is not None:
                    res["blank"] = s2 < 7.0                      # near-uniform = nothing drawn
                    if s1 is not None:
                        # near-identical frames on a game canvas after input = frozen loop
                        res["frozen"] = (abs(s1 - s2) < 0.4 and s2 < 16.0)
            res["ran"] = True
            browser.close()
    except Exception as e:
        errors.append(f"playtest harness error: {e}")

    res["errors"] = _real_errors(errors)[:8]
    bits = []
    if res["errors"]: bits.append(f"{len(res['errors'])} JS error(s)")
    if res["blank"]:  bits.append("canvas is blank/black")
    if res["frozen"]: bits.append("canvas never changes (loop not running)")
    res["summary"] = "; ".join(bits) if bits else "renders and runs"
    return res


def is_broken(res: dict) -> bool:
    return bool(res.get("errors") or res.get("blank") or res.get("frozen"))


def repair_brief(res: dict) -> str:
    """Turn playtest findings into a concrete fix instruction for the build agent."""
    lines = ["The game does NOT work when opened in a browser. A real headless playtest found:"]
    if res.get("blank"):
        lines.append("- The canvas/play-area is BLANK/black — nothing is being rendered.")
    if res.get("frozen"):
        lines.append("- The canvas never changes after input — the game/render loop isn't running.")
    if res.get("errors"):
        lines.append("- Uncaught JavaScript errors in the console (fix the root cause of each):")
        for e in res["errors"]:
            lines.append(f"    • {e}")
    lines.append("Debug and FIX every one of these so the game actually renders and plays. Verify by "
                 "reasoning through the game loop and draw calls. Keep it a single self-contained "
                 "index.html with no build step.")
    return "\n".join(lines)
