"""
CASE FILES / EVIDENCE BOARD — the batcomputer's soul.

  "open a case on the product launch"           → a case file is created
  "add to the case: landing page is live"       → evidence pinned to the open case
  "show the evidence board"                     → force-graph of the case: every
     piece of evidence + auto-pulled related memories, linked around the case node

Storage: workspace/cases.json. The board reuses the D3 architecture renderer.
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


_CASES = _base() / "workspace" / "cases.json"


def _load() -> dict:
    try:
        return json.loads(_CASES.read_text(encoding="utf-8"))
    except Exception:
        return {"open": "", "cases": {}}


def _save(d: dict) -> None:
    _CASES.parent.mkdir(parents=True, exist_ok=True)
    _CASES.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def open_case(name: str) -> str:
    name = (name or "").strip().rstrip(".?!").title()
    if len(name) < 3:
        return "A case on what, sir?"
    d = _load()
    key = name.lower()
    if key not in d["cases"]:
        d["cases"][key] = {"name": name, "opened": time.time(), "evidence": []}
    d["open"] = key
    _save(d)
    n = len(d["cases"][key]["evidence"])
    return (f"Case file opened, sir — “{name}”. "
            + (f"{n} pieces of evidence on the board." if n else
               "The board is empty; say “add to the case” as things develop."))


def add_evidence(note: str, jarvis=None) -> str:
    d = _load()
    if not d.get("open") or d["open"] not in d["cases"]:
        return "No case is open, sir — say “open a case on …” first."
    note = (note or "").strip()
    if not note and jarvis is not None:
        # "add that to the case" — pin the previous exchange
        note = next((m["content"] for m in reversed(getattr(jarvis, "_conversation", []))
                     if m.get("role") == "assistant" and m.get("content")), "")[:180]
    if not note:
        return "Add what to the case, sir?"
    case = d["cases"][d["open"]]
    case["evidence"].append({"t": time.time(), "note": note[:200]})
    _save(d)
    try:
        from core.semantic_memory import index_async
        index_async(f"[case {case['name']}] {note}", "case")
    except Exception:
        pass
    return f"Pinned to the {case['name']} board, sir — {len(case['evidence'])} pieces now."


def add_evidence_file(filename: str, data_url: str) -> str:
    """Attach a real file to the open case. Saved under workspace/case_files/;
    images keep their data URL so the evidence board shows a true thumbnail."""
    import base64
    d = _load()
    if not d.get("open") or d["open"] not in d["cases"]:
        return "No case is open, sir."
    case = d["cases"][d["open"]]
    try:
        header, _, b64 = (data_url or "").partition(",")
        blob = base64.b64decode(b64)
        mime = header.split(":", 1)[-1].split(";")[0] if ":" in header else ""
        safe = "".join(c for c in (filename or "file") if c.isalnum() or c in "._- ")[:80] or "file"
        folder = _base() / "workspace" / "case_files" / d["open"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe
        path.write_bytes(blob)
    except Exception as e:
        return f"Couldn't save that file, sir: {e}"

    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
    kind = ("img" if mime.startswith("image") or ext in ("png", "jpg", "jpeg", "gif", "webp") else
            "video" if mime.startswith("video") or ext in ("mp4", "mov", "m4v") else
            "audio" if mime.startswith("audio") or ext in ("wav", "mp3", "m4a") else
            "pdf" if ext == "pdf" else "doc")
    item = {"t": time.time(), "note": safe, "file": str(path), "kind": kind,
            "size": len(blob)}
    if kind == "img" and len(blob) < 900_000:          # small images inline as thumbs
        item["thumb"] = data_url
    case.setdefault("files", []).append(item)
    _save(d)
    return f"Filed under {case['name']}, sir — {safe}."


def close_case(name: str = "") -> str:
    d = _load()
    key = (name or "").strip().lower() or d.get("open", "")
    if key in d["cases"]:
        n = d["cases"][key]["name"]
        d["cases"].pop(key)
        if d.get("open") == key:
            d["open"] = ""
        _save(d)
        return f"Case closed and filed, sir — {n}."
    return "No such case, sir."


def _photo_for(query: str) -> str:
    """A real image for a subject/place: Wikipedia thumbnail first (free, reliable),
    then Wikimedia Commons image search. Returns '' if nothing solid is found."""
    q = (query or "").strip()
    if len(q) < 3:
        return ""
    try:
        from actions.dossiers import _wiki
        w = _wiki(q)
        if w.get("photo"):
            return w["photo"]
    except Exception:
        pass
    try:                                             # Wikimedia Commons image search
        import requests
        r = requests.get("https://commons.wikimedia.org/w/api.php",
                         params={"action": "query", "generator": "search",
                                 "gsrsearch": f"{q} filetype:bitmap", "gsrlimit": 1,
                                 "gsrnamespace": 6, "prop": "imageinfo",
                                 "iiprop": "url", "iiurlwidth": 200, "format": "json"},
                         headers={"User-Agent": "Alfred/2.1"}, timeout=8)
        pages = (r.json().get("query", {}) or {}).get("pages", {}) or {}
        for p in pages.values():
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("thumburl"):
                return ii["thumburl"]
    except Exception:
        pass
    return ""


def _geocode(place: str) -> tuple[float, float] | None:
    """Real place name → (lat, lng) via OpenStreetMap Nominatim (free, no key)."""
    try:
        import requests
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": place, "format": "json", "limit": 1},
                         headers={"User-Agent": "Alfred/2.1 (personal intelligence assistant)"},
                         timeout=8)
        j = r.json()
        if j:
            return round(float(j[0]["lat"]), 5), round(float(j[0]["lon"]), 5)
    except Exception:
        pass
    return None


def _locate(points: list[dict]) -> list[dict]:
    """Geocode each {name, leads} location; keep only the ones that resolve to a
    real place. Empty result → the renderer falls back to the stylised map."""
    from concurrent.futures import ThreadPoolExecutor
    if not points:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(points))) as ex:
        coords = list(ex.map(lambda p: _geocode(p.get("name", "")), points))
    out = []
    for p, c in zip(points, coords):
        if c:
            out.append({"name": p.get("name", ""), "leads": int(p.get("leads", 0) or 0),
                        "lat": c[0], "lng": c[1]})
    return out


def _attach_photos(items: list[dict], field: str = "photo", key: str = "name") -> None:
    """Fill each item's photo in parallel (skips ones that already have one)."""
    from concurrent.futures import ThreadPoolExecutor
    todo = [it for it in items if not it.get(field) and it.get(key)]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=min(5, len(todo))) as ex:
        for it, url in zip(todo, ex.map(lambda x: _photo_for(x[key]), todo)):
            if url:
                it[field] = url


def _extract_json(text: str) -> dict:
    """Pull the first balanced {...} object out of an LLM reply, tolerating ```json
    fences and trailing truncation (closes any unbalanced braces before parsing)."""
    if not text:
        return {}
    s = text.strip()
    if "```" in s:
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.replace("```", "")
        s = s[4:] if s.lstrip().lower().startswith("json") else s
    i = s.find("{")
    if i < 0:
        return {}
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        c = s[j]
        if in_str:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    break
    frag = s[i:]                                    # truncated: close it and retry
    frag += '"' if in_str else ""
    frag += "}" * max(0, depth)
    try:
        return json.loads(frag)
    except Exception:
        return {}


def casefile_payload(name: str = "", jarvis=None) -> dict | None:
    """The full Mission-Control case file (matches the RED LANTERN layout). Alfred
    synthesises summary, subjects, timeline, tags and an AI analysis from whatever
    evidence has been pinned — so any case renders like a real intelligence file."""
    import time as _t
    from datetime import datetime
    d = _load()
    key = (name or "").strip().lower() or d.get("open", "")
    if key not in d["cases"]:
        return None
    case = d["cases"][key]
    ev = case.get("evidence", [])
    ev_lines = [e["note"] for e in ev]

    # RESEARCH the case subject in parallel with any pinned evidence — so opening a
    # case on "Joker" or a real person auto-fills with real intel + a real photo,
    # never a blank board.
    from concurrent.futures import ThreadPoolExecutor
    from actions.dossiers import _wiki, _news
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_wiki, f_news = ex.submit(_wiki, case["name"]), ex.submit(_news, case["name"])
        wiki, news = f_wiki.result(), f_news.result()

    card = {}
    try:
        from core.llm_client import call_llm_text
        raw = f"Case subject: {case['name']}\n"
        if wiki.get("extract"): raw += f"Public record: {wiki['extract']}\n"
        if news:      raw += "Recent news:\n" + "\n".join(f"- {h}" for h in news) + "\n"
        if ev_lines:  raw += "Evidence pinned:\n" + "\n".join(f"- {x}" for x in ev_lines) + "\n"
        out = call_llm_text(
            raw + "\nBuild the intelligence case file on this subject.",
            system=("You are Alfred building an intelligence case file. STRICT JSON only: "
                    '{"summary":"2-3 sentence case overview","tags":["3-5 short tags"],'
                    '"progress":0-100,"subjects":[{"name":"real person/org tied to the case","alias":"role or alias","last_seen":"e.g. on file / 3d ago","risk":"HIGH RISK|MEDIUM|LOW|UNKNOWN"}],'
                    '"timeline":[{"time":"","title":"","detail":""}],"notes":["key facts / takeaways"],'
                    '"locations":[{"name":"REAL geocodable place tied to the subject (real city/district/landmark)","leads":int}],'
                    '"ai":{"analysis":"1-2 sentence insight","probability":0-100,'
                    '"actions":[{"text":"recommended action","done":false}]}} '
                    "Always include the main subject as the first entry in subjects. "
                    "Populate timeline from the news/record. For locations use ONLY real mappable "
                    "places connected to the subject (birthplace, HQ, base of operations, key cities). "
                    "Ground everything in the record; do not fabricate."),
            tier="heavy", timeout=50, max_tokens=1500)
        card = _extract_json(out)
    except Exception:
        card = {}

    subjects = card.get("subjects") or [{"name": case["name"], "alias": wiki.get("desc", ""),
                                         "last_seen": "on file", "risk": "SUBJECT"}]
    # make sure the lead subject carries the subject's own real photo
    if subjects and wiki.get("photo") and not subjects[0].get("photo"):
        subjects[0]["photo"] = wiki["photo"]
    _attach_photos(subjects)                          # real portraits for the rest
    map_points = _locate(card.get("locations") or [])  # real geocoded map markers

    # deterministic fallbacks so the file always looks alive
    opened = datetime.fromtimestamp(case.get("opened", _t.time()))
    cid = f"{''.join(w[0] for w in case['name'].split()[:2]).upper() or 'CF'}-{opened:%y%m}-{int(case.get('opened',0))%1000:03d}"
    timeline = card.get("timeline") or (
        [{"time": "recent", "title": h[:44], "detail": h} for h in news[:5]] or
        [{"time": datetime.fromtimestamp(e["t"]).strftime("%d %b %Y %H:%M"),
          "title": "Evidence logged", "detail": e["note"][:90]} for e in reversed(ev[-5:])])

    # sys telemetry for the status bar
    cpu = ram = mem = None
    try:
        import psutil
        cpu = round(psutil.cpu_percent(interval=0.1)); ram = round(psutil.virtual_memory().percent)
        mem = ram
    except Exception:
        pass

    related = _related_cases(key, subjects, card.get("tags") or [])

    return {"type": "casefile", "title": case["name"], "data": {
        "name": case["name"], "case_id": cid, "priority": card.get("priority", "High"),
        "created": f"{opened:%d %b %Y}".upper(),
        "updated": f"{datetime.now():%d %b %Y, %H:%M}".upper(),
        "status": "ACTIVE", "progress": int(card.get("progress", min(90, 15 + len(ev) * 12))),
        "summary": card.get("summary") or wiki.get("extract") or f"Active investigation: {case['name']}.",
        "tags": card.get("tags") or ([wiki["desc"]] if wiki.get("desc") else ["Investigation"]),
        "subjects": subjects,
        "hero_photo": wiki.get("hero") or wiki.get("photo", ""),
        "timeline": timeline,
        # real attached files first (with true thumbnails), then dictated notes
        "evidence": ([{"name": f["note"], "kind": f.get("kind", "doc"),
                       "thumb": f.get("thumb", "")} for f in reversed(case.get("files", [])[-5:])] +
                     [{"name": (e["note"][:26] + "…") if len(e["note"]) > 26 else e["note"],
                       "kind": "doc"} for e in reversed(ev[-5:])])[:5],
        "notes": card.get("notes") or ev_lines[-4:] or ([wiki["extract"]] if wiki.get("extract") else []),
        "ai": card.get("ai") or {"analysis": wiki.get("extract", "Gathering intelligence, sir.")[:180],
                                  "probability": None, "actions": []},
        "map_points": map_points,                     # real coords → live map markers
        "map_label": card.get("map_label", "OPERATIONS MAP"),
        "related": related,                           # cross-linked cases (shared subjects/tags)
        "cpu": cpu, "ram": ram, "mem": mem,
        "user": (jarvis and getattr(jarvis, "_user_name", None)) or "OPERATOR"}}


def _related_cases(this_key: str, subjects: list[dict], tags: list) -> list[dict]:
    """Find OTHER open cases that share a subject name or tag — the batcomputer
    connecting the dots across investigations."""
    d = _load()
    terms = set()
    for s in subjects:
        for w in str(s.get("name", "")).lower().split():
            if len(w) > 3: terms.add(w)
    for tg in tags:
        for w in str(tg).lower().split():
            if len(w) > 3: terms.add(w)
    out = []
    for k, c in d.get("cases", {}).items():
        if k == this_key:
            continue
        hay = (c.get("name", "") + " " + " ".join(e.get("note", "") for e in c.get("evidence", []))).lower()
        if any(term in hay for term in terms):
            cid = f"{''.join(w[0] for w in c['name'].split()[:2]).upper() or 'CF'}-{int(c.get('opened',0))%1000:03d}"
            out.append({"name": "Case: " + c["name"], "id": cid})
    return out[:4]


def board_graph(name: str = "", jarvis=None) -> dict | None:
    """Evidence board as a force-graph: case → evidence + related memory pulls."""
    d = _load()
    key = (name or "").strip().lower() or d.get("open", "")
    if key not in d["cases"]:
        return None
    case = d["cases"][key]
    nodes = [{"id": "case", "label": f"CASE · {case['name']}", "group": 0}]
    links = []
    for i, ev in enumerate(case["evidence"][-14:]):
        nid = f"e{i}"
        nodes.append({"id": nid, "label": ev["note"][:60], "group": 1})
        links.append({"source": "case", "target": nid, "label": ""})
    # auto-pull related memories around the case (the batcomputer connects the dots)
    try:
        from core.semantic_memory import search
        for j, h in enumerate(search(case["name"], k=5, min_sim=0.32)):
            if h.get("kind") == "case":
                continue
            nid = f"m{j}"
            nodes.append({"id": nid, "label": f"[{h['kind']}] " + h["text"][:55], "group": 2})
            links.append({"source": "case", "target": nid, "label": "related"})
    except Exception:
        pass
    return {"type": "architecture", "title": f"EVIDENCE BOARD — {case['name']}",
            "data": {"nodes": nodes, "links": links}}
