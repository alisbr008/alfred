"""
Alfred — Visualization Engine (router)
─────────────────────────────────────────────────────────────────────────────
Turns a spoken request into an interactive visual rendered INSIDE Alfred's own
webview (web_ui/index.html) — no browser, no external site.

Four routes:
  • 3D        — procedural Three.js scenes (solar system, atom, DNA, molecule,
                or a generic labelled object). Instant, offline, always works.
  • diagram   — D3 force node-graph ("explain transformers"). One Haiku call.
  • timeline  — D3 animated timeline ("explain World War 2"). One Haiku call.
  • architecture — force-graph of Alfred's own codebase. Instant, scanned live.

Returns a short spoken narration; pushes the payload to the webview via
`jarvis.ui.show_visualization(payload)`.
"""
from __future__ import annotations

import json
import re
import sys
import time
import threading
from pathlib import Path

try:
    import requests
    _REQ = True
except ImportError:
    _REQ = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = _base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:48] or "model"


def _models_dir() -> Path:
    d = BASE_DIR / "web_ui" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download(url: str, path: Path) -> bool:
    if not _REQ:
        return False
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        path.write_bytes(r.content)
        return path.stat().st_size > 200
    except Exception as e:
        print(f"[Viz] model download failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Real 3D models — Poly Pizza (instant, free) + Meshy (AI text-to-3D, ~40s)
# ─────────────────────────────────────────────────────────────────────────────

def _poly_pizza(topic: str, key: str) -> str | None:
    """Search Poly Pizza for a real GLB model. Returns a direct .glb URL or None."""
    if not _REQ or not key:
        return None
    try:
        r = requests.get(f"https://api.poly.pizza/v1.1/search/{requests.utils.quote(topic)}",
                          headers={"x-auth-token": key}, timeout=15)
        r.raise_for_status()
        for item in (r.json().get("results") or []):
            url = item.get("Download") or item.get("download")
            if url and ".glb" in url.lower():
                return url
    except Exception as e:
        print(f"[Viz] Poly Pizza failed: {e}")
    return None


def _meshy(topic: str, key: str) -> str | None:
    """Generate a real model with Meshy text-to-3D (preview mode). Blocks ~40-60s.
    Returns a .glb URL or None."""
    if not _REQ or not key:
        return None
    H = {"Authorization": f"Bearer {key}"}
    try:
        r = requests.post("https://api.meshy.ai/openapi/v2/text-to-3d", headers=H,
                          json={"mode": "preview", "prompt": topic, "art_style": "realistic",
                                "should_remesh": True}, timeout=20)
        r.raise_for_status()
        task = r.json().get("result")
        if not task:
            return None
        for _ in range(60):                      # poll up to ~3 min
            time.sleep(3)
            s = requests.get(f"https://api.meshy.ai/openapi/v2/text-to-3d/{task}",
                             headers=H, timeout=20)
            s.raise_for_status()
            j = s.json()
            if j.get("status") == "SUCCEEDED":
                return (j.get("model_urls") or {}).get("glb")
            if j.get("status") in ("FAILED", "CANCELED"):
                return None
    except Exception as e:
        print(f"[Viz] Meshy failed: {e}")
    return None


def _geocode(place: str) -> dict | None:
    """Free geocoding via OpenStreetMap Nominatim. Returns {center:[lng,lat], zoom}."""
    if not _REQ or not place:
        return None
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": place, "format": "json", "limit": 1},
                         headers={"User-Agent": "Alfred-Assistant/1.0"}, timeout=15)
        r.raise_for_status()
        arr = r.json()
        if not arr:
            return None
        hit = arr[0]
        lng, lat = float(hit["lon"]), float(hit["lat"])
        # derive a zoom from the bounding box span (bigger place → wider view)
        zoom = 6
        bb = hit.get("boundingbox") or []
        if len(bb) == 4:
            try:
                s, n, w, e = map(float, bb)
                span = max(abs(n - s), abs(e - w))
                zoom = (4 if span > 20 else 5 if span > 8 else 6 if span > 3
                        else 7 if span > 1.2 else 9 if span > 0.4 else 11)
            except Exception:
                pass
        return {"center": [lng, lat], "zoom": zoom}
    except Exception as ex:
        print(f"[Viz] geocode failed: {ex}")
        return None


def _gen_map(place: str) -> dict:
    geo = _geocode(place) or {"center": [0, 20], "zoom": 2}
    return {"type": "map", "title": place.title(),
            "data": {"center": geo["center"], "zoom": geo["zoom"], "place": place.title()}}


def _resolve_model_sync(topic: str) -> str | None:
    """Instant path: local cache, then Poly Pizza. Returns a webview-relative URL.
    Caching means the library GROWS with every new object the user asks for."""
    slug = _slug(topic)
    path = _models_dir() / f"{slug}.glb"
    if path.exists():
        return f"models/{slug}.glb"
    key = _cfg().get("polypizza_key", "")
    # Try the full phrase first, then the head noun ("vintage sports car" → "car").
    candidates = [topic]
    words = topic.split()
    if len(words) > 1:
        candidates.append(words[-1])
    for cand in candidates:
        url = _poly_pizza(cand, key)
        if url and _download(url, path):
            return f"models/{slug}.glb"
    return None


def _spawn_meshy(topic: str, key: str, ui) -> None:
    """Background: generate via Meshy, cache, then push the model to the webview."""
    def _run():
        url = _meshy(topic, key)
        slug = _slug(topic)
        path = _models_dir() / f"{slug}.glb"
        if url and _download(url, path):
            payload = {"type": "3d", "title": topic.title(),
                       "data": {"modelUrl": f"models/{slug}.glb", "label": topic.title()}}
        else:
            payload = _gen_3d(topic)   # orb fallback
        try:
            if ui is not None and hasattr(ui, "show_visualization"):
                ui.show_visualization(payload)
        except Exception as e:
            print(f"[Viz] Meshy push failed: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Route classification (keyword-first — deterministic & instant)
# ─────────────────────────────────────────────────────────────────────────────

_OWN_ARCH = ("your architecture", "my architecture", "alfred architecture", "your codebase",
             "how you're built", "how you are built", "your structure",
             "your brain", "your code", "how you work internally",
             "your dependency", "your modules")

# Procedural science scenes — matched as WHOLE WORDS/phrases only, so "space" can
# never hijack "spaceship". Anything not here becomes a real 3D model search.
_SCENE_PHRASES = {
    "solar_system":   ("solar system", "the planets", "milky way"),
    "alpha_centauri": ("alpha centauri", "proxima centauri", "centauri system",
                       "binary star", "triple star", "star system"),
    "atom":           ("atom", "atomic structure", "electron", "nucleus"),
    "dna":            ("dna", "double helix", "genome"),
    "molecule":       ("water molecule", "h2o", "h₂o"),
}

_TIMELINE_HINT = ("timeline", "history of", "world war", "ww2", "ww1",
                  "evolution of", "rise and fall", "chronology",
                  "throughout history", " era ", "the cold war")

# EXPLICIT explanation intent → node-graph. A physical object never matches these.
_DIAGRAM_HINT = ("explain ", "how does ", "how do ", "diagram of", "break down",
                 "architecture of", "how it works", "how does it work", "concept of",
                 "walk me through", "visualize how", "why does ", "the process of",
                 "difference between", "compare ")

# words/phrases stripped to recover the bare subject for a model search
_OBJ_LEADS = ("show me a 3d model of", "show me the 3d model of", "a 3d model of",
              "3d model of", "show me a model of", "model of", "show me a", "show me an",
              "show me the", "show me", "render me a", "render a", "render an", "render the",
              "render", "draw me a", "draw a", "draw the", "draw", "visualize a", "visualize",
              "can you show me a", "can you show me", "i want to see a", "i want to see",
              "let me see a", "let me see", "a 3d", "3d", "a real 3d model of", "the")


def _match_scene(t: str):
    for scene, phrases in _SCENE_PHRASES.items():
        for p in phrases:
            if re.search(rf"\b{re.escape(p)}\b", t):
                return scene
    return None


def _classify(text: str) -> tuple[str, str]:
    """Return (route, topic). route ∈ 3d|map|diagram|timeline|architecture.
    DEFAULT for any physical thing is a real 3D model search — only explicit
    explain / history / map intent routes elsewhere."""
    t = text.lower().strip()
    t = re.sub(r"^(alfred[,\s]+)?(please\s+)?", "", t)   # strip wake-word / filler

    if any(k in t for k in _OWN_ARCH):
        return "architecture", "alfred"

    # Geographic maps — "3d map of Scotland", "map of X", "fly to X",
    # "take me to X", "go to X", "travel to X".
    if ("map of" in t or "3d map" in t or "on a map" in t or "on the map" in t
            or t.startswith("fly to") or t.startswith("map ")
            or t.startswith("take me to") or t.startswith("take me ")
            or t.startswith("go to ") or t.startswith("travel to")
            or t.startswith("bring me to") or t.startswith("show me where")):
        place = _strip_lead(t, ("show me a 3d map of", "show me the 3d map of",
                                "show me a map of", "show me the map of", "a 3d map of",
                                "the 3d map of", "3d map of", "3d map", "map of", "fly to",
                                "take me to the", "take me to", "take me", "travel to",
                                "bring me to the", "bring me to", "go to the", "go to",
                                "show me where", "show me", "on a map", "on the map",
                                "where is", "map"))
        return "map", place

    # Explicit history/sequence → timeline
    if any(k in t for k in _TIMELINE_HINT):
        return "timeline", _strip_lead(t, ("explain", "timeline of", "the history of",
                                           "history of", "show me", "visualize"))

    # Explicit "explain how X works / concept of X" → node-graph diagram
    if any(h in t for h in _DIAGRAM_HINT):
        return "diagram", _strip_lead(t, ("explain", "how does", "how do", "why does",
                                          "diagram of", "break down", "walk me through",
                                          "visualize how", "the concept of", "concept of",
                                          "the process of", "difference between", "compare"))

    # Precise procedural science scene
    scene = _match_scene(t)
    if scene:
        return "3d", scene

    # DEFAULT — it's a thing to SEE → search/render a real 3D model.
    return "3d", _strip_lead(t, _OBJ_LEADS)


def _strip_lead(t: str, leads) -> str:
    t = t.strip()
    for lead in sorted(leads, key=len, reverse=True):
        if t.startswith(lead):
            t = t[len(lead):]
            break
    t = re.sub(r"^(a|an|the)\s+", "", t.strip())          # drop a leading article
    t = re.sub(r"\b(work|works|working)\b\s*$", "", t).strip(" ?.!,")
    return t.strip() or "this"


# ─────────────────────────────────────────────────────────────────────────────
# LLM content generation (diagram / timeline)
# ─────────────────────────────────────────────────────────────────────────────

def _llm_json(prompt: str, system: str) -> dict | None:
    try:
        from core.llm_client import call_llm_text
        raw = call_llm_text(prompt, system=system) or ""
    except Exception as e:
        print(f"[Viz] LLM error: {e}")
        return None
    raw = raw.strip()
    # pull the first {...} block out of any prose / code fences
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        # last-ditch: strip trailing commas
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            return None


def _gen_diagram(topic: str) -> dict:
    system = (
        "You produce JSON for an interactive concept node-graph. "
        "Return ONLY raw JSON, no prose, no markdown. Schema:\n"
        '{"nodes":[{"id":"short_id","label":"Human Label","group":1}],'
        '"links":[{"source":"id_a","target":"id_b","label":"relation"}]}\n'
        "Rules: 6-11 nodes. 'group' is an integer 1-5 clustering related nodes. "
        "Every link source/target MUST be an existing node id. "
        "Labels are concise (1-3 words). Make it genuinely explain the concept."
    )
    data = _llm_json(f"Concept to explain as a node graph: {topic}", system)
    if not data or not data.get("nodes"):
        # safe fallback
        data = {"nodes": [{"id": "topic", "label": topic.title(), "group": 1}],
                "links": []}
    return {"type": "diagram", "title": topic.title(), "data": data}


def _gen_timeline(topic: str) -> dict:
    system = (
        "You produce JSON for an animated historical timeline. "
        "Return ONLY raw JSON, no prose, no markdown. Schema:\n"
        '{"events":[{"date":"1939","title":"Short Title","desc":"One concise sentence."}]}\n'
        "Rules: 5-9 chronological events. 'date' is a year or short label. "
        "Titles 1-4 words. Descriptions one sentence."
    )
    data = _llm_json(f"Build a timeline for: {topic}", system)
    if not data or not data.get("events"):
        data = {"events": [{"date": "—", "title": topic.title(),
                            "desc": "No timeline data available."}]}
    return {"type": "timeline", "title": topic.title(), "data": data}


# ─────────────────────────────────────────────────────────────────────────────
# Architecture (Alfred's own codebase — scanned live, no LLM)
# ─────────────────────────────────────────────────────────────────────────────

def _gen_architecture() -> dict:
    nodes = [
        {"id": "you",    "label": "You (voice/text)", "group": 1},
        {"id": "stt",    "label": "Whisper STT",       "group": 1},
        {"id": "core",   "label": "Alfred Core",       "group": 2},
        {"id": "router", "label": "Model Router",      "group": 2},
        {"id": "haiku",  "label": "Claude Haiku",      "group": 3},
        {"id": "sonnet", "label": "Claude Sonnet",     "group": 3},
        {"id": "glm",    "label": "GLM 5.1",           "group": 3},
        {"id": "qwen",   "label": "Qwen3 (free)",      "group": 3},
        {"id": "memory", "label": "Memory + Vault",    "group": 4},
        {"id": "tools",  "label": "Tools / Actions",   "group": 4},
        {"id": "wf",     "label": "12 Workflows",      "group": 4},
        {"id": "tts",    "label": "Fish Audio TTS",    "group": 5},
        {"id": "ui",     "label": "Webview UI",        "group": 5},
    ]
    links = [
        ("you", "stt", "speak"), ("stt", "core", "text"),
        ("core", "router", "route"),
        ("router", "haiku", "fast"), ("router", "sonnet", "architect"),
        ("router", "glm", "engineer"), ("router", "qwen", "agents"),
        ("core", "memory", "recall"), ("core", "tools", "act"),
        ("tools", "wf", "run"),
        ("core", "tts", "reply"), ("tts", "you", "voice"),
        ("core", "ui", "render"),
    ]
    # enrich tool count from the live actions/ folder
    try:
        n = len(list((BASE_DIR / "actions").glob("*.py")))
        for nd in nodes:
            if nd["id"] == "tools":
                nd["label"] = f"Tools / Actions ({n})"
    except Exception:
        pass
    data = {"nodes": nodes,
            "links": [{"source": s, "target": t, "label": l} for s, t, l in links]}
    return {"type": "architecture", "title": "Alfred — System Architecture",
            "data": data}


# ─────────────────────────────────────────────────────────────────────────────
# 3D scenes (procedural — no LLM, no assets)
# ─────────────────────────────────────────────────────────────────────────────

def _gen_3d(scene_or_topic: str) -> dict:
    known = {"solar_system", "alpha_centauri", "atom", "dna", "molecule"}
    if scene_or_topic in known:
        scene, label = scene_or_topic, scene_or_topic.replace("_", " ").title()
    else:
        scene, label = "object", scene_or_topic.title()
    return {"type": "3d", "title": label,
            "data": {"scene": scene, "label": label}}


# ─────────────────────────────────────────────────────────────────────────────
# Narration
# ─────────────────────────────────────────────────────────────────────────────

_NARRATION = {
    "3d":           "Rendering {title} now, sir. You can rotate it by dragging.",
    "map":          "Taking you to {title}, sir — real terrain. Drag to explore.",
    "diagram":      "Here's how {title} works, sir — each node is a component.",
    "timeline":     "Here's the timeline of {title}, sir.",
    "architecture": "This is my architecture, sir — voice in, router, tools, and voice out.",
}


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def visualize(parameters: dict, response=None, player=None,
              session_memory=None, jarvis=None, speak=None) -> str:
    params  = parameters or {}
    request = (params.get("request") or params.get("query")
               or params.get("topic") or params.get("text") or "").strip()
    route   = (params.get("type") or "").lower().strip()
    topic   = ""

    if route in ("3d", "diagram", "timeline", "architecture"):
        # Honour the explicit type, but still clean the phrasing so the label is
        # "Boeing 747", never "Show Me A Boeing 747".
        _, topic = _classify(request)
        if route == "architecture":
            topic = "alfred"
        elif route == "3d":
            # re-map cleaned topic onto a known scene if it matches
            r2, t2 = _classify(request)
            topic = t2 or "object"
    else:
        route, topic = _classify(request)

    if player:
        player.write_log(f"SYS: Visualize [{route}] — {topic or request}")
    print(f"[Viz] route={route} topic={topic!r}")

    ui = (getattr(jarvis, "ui", None) if jarvis is not None else None) or player

    if route == "architecture":
        payload = _gen_architecture()
    elif route == "map":
        payload = _gen_map(topic or request)
    elif route == "timeline":
        payload = _gen_timeline(topic or request)
    elif route == "3d":
        scene_topic = topic or request
        if scene_topic in ("solar_system", "alpha_centauri", "atom", "dna", "molecule"):
            payload = _gen_3d(scene_topic)            # procedural — instant
        else:
            url = _resolve_model_sync(scene_topic)    # cache or Poly Pizza — instant
            if url:
                payload = {"type": "3d", "title": scene_topic.title(),
                           "data": {"modelUrl": url, "label": scene_topic.title()}}
            else:
                meshy_key = _cfg().get("meshy_key", "")
                if meshy_key and ui is not None and hasattr(ui, "show_visualization_loading"):
                    ui.show_visualization_loading(scene_topic.title(), scene_topic.title())
                    _spawn_meshy(scene_topic, meshy_key, ui)   # pushes when ready (~40s)
                    return (f"Modeling {scene_topic.title()} now, sir — building a real 3D "
                            "model. One moment.")
                payload = _gen_3d(scene_topic)        # orb fallback (no keys configured)
    else:
        payload = _gen_diagram(topic or request)

    # Push to the webview
    try:
        if jarvis is not None and getattr(jarvis, "ui", None) is not None:
            jarvis.ui.show_visualization(payload)
        elif player is not None and hasattr(player, "show_visualization"):
            player.show_visualization(payload)
    except Exception as e:
        print(f"[Viz] render push failed: {e}")
        return f"I built the visualization but couldn't display it, sir: {e}"

    title = payload.get("title", topic or "that")
    return _NARRATION.get(route, "Done, sir.").format(title=title)
