"""
MISSION BOARD — persistent operations tracking, batcomputer style.

Missions live in workspace/missions.json and survive restarts. Voice CRUD:
  "add mission <title>"                → new mission
  "update mission <name>: <note>"     → log progress (auto-bumps progress a little)
  "complete mission <name>"           → done
  "mission status"                    → spoken summary
  "mission board / show missions"     → the fullscreen batcomputer wall

The board payload also carries live intel (world feed), system telemetry and the
Sentinel line, so the wall looks alive — like the batcave, not a todo app.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_MB   = _BASE / "workspace" / "missions.json"


def _load() -> list[dict]:
    try:
        return json.loads(_MB.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(ms: list[dict]) -> None:
    _MB.parent.mkdir(parents=True, exist_ok=True)
    _MB.write_text(json.dumps(ms, indent=2, ensure_ascii=False), encoding="utf-8")


def _find(ms: list[dict], q: str) -> dict | None:
    ql = (q or "").lower().strip()
    if not ql:
        return None
    for m in ms:                                   # exact-ish first
        if ql == m["title"].lower():
            return m
    words = set(re.findall(r"[a-z0-9]+", ql))
    best, score = None, 0
    for m in ms:
        s = len(words & set(re.findall(r"[a-z0-9]+", m["title"].lower())))
        if s > score:
            best, score = m, s
    return best if score else None


def _now() -> str:
    return f"{datetime.now():%Y-%m-%d %H:%M}"


# ── voice CRUD ───────────────────────────────────────────────────────────────
def add_mission(title: str, deadline: str = "") -> str:
    title = (title or "").strip().rstrip(".")
    if not title:
        return "What's the mission, sir?"
    # "apply to unis by aug 15" → pull a deadline if one is phrased
    m = re.search(r"\b(?:by|before|until|due)\s+(.+)$", title, re.IGNORECASE)
    if m and not deadline:
        deadline = m.group(1).strip()
        title = title[:m.start()].strip()
    ms = _load()
    ms.append({"title": title, "status": "active", "progress": 0,
               "deadline": deadline, "notes": [], "updated": _now()})
    _save(ms)
    return f"Mission logged, sir — {title}" + (f", due {deadline}." if deadline else ".")


def update_mission(query: str, note: str | None = None, progress: int | None = None) -> str:
    ms = _load()
    m = _find(ms, query)
    if not m:
        return f"I couldn't find a mission matching '{query}', sir."
    if note:
        m["notes"] = (m.get("notes") or [])[-9:] + [{"t": _now(), "note": note}]
        pm = re.search(r"(\d{1,3})\s*%", note)
        if pm:
            progress = int(pm.group(1))
    if progress is not None:
        m["progress"] = max(0, min(100, int(progress)))
    elif note:
        m["progress"] = min(95, int(m.get("progress", 0)) + 10)   # motion implies progress
    m["updated"] = _now()
    _save(ms)
    return f"Noted on {m['title']}, sir — {m['progress']}% along."


def complete_mission(query: str) -> str:
    ms = _load()
    m = _find(ms, query)
    if not m:
        return f"I couldn't find a mission matching '{query}', sir."
    m["status"] = "done"
    m["progress"] = 100
    m["updated"] = _now()
    _save(ms)
    return f"Mission accomplished, sir — {m['title']} is closed."


def speak_status() -> str:
    active = [m for m in _load() if m.get("status") == "active"]
    if not active:
        return "The board is clear, sir — no active missions."
    bits = []
    for m in active[:5]:
        b = f"{m['title']} at {m.get('progress', 0)} percent"
        if m.get("deadline"):
            b += f", due {m['deadline']}"
        bits.append(b)
    return f"{len(active)} active mission{'s' if len(active) != 1 else ''}, sir: " + "; ".join(bits) + "."


# ── live markets (Yahoo Finance chart API — no key needed) ──────────────────
_MKT_SYMBOLS = [("^GSPC", "S&P 500"), ("BTC-USD", "BTC"), ("NVDA", "NVDA"), ("TSLA", "TSLA")]
_MKT_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _mkt_one(sym: str, label: str) -> dict | None:
    try:
        import requests
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         params={"interval": "15m", "range": "1d"},
                         headers=_MKT_UA, timeout=8)
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res:
            return None
        meta = res.get("meta", {})
        price = meta.get("regularMarketPrice")
        prev  = meta.get("chartPreviousClose") or meta.get("previousClose") or price
        closes = [c for c in (res.get("indicators", {}).get("quote") or [{}])[0]
                  .get("close", []) if c is not None]
        chg = ((price - prev) / prev * 100) if (price and prev) else 0.0
        return {"sym": label, "price": price, "chg": round(chg, 2), "series": closes[-48:]}
    except Exception:
        return None


def markets_snapshot() -> dict:
    """Quotes + an intraday series for the board's stock chart. BTC charts when
    equity markets are closed (it never sleeps — fitting, for a night creature)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        rows = [q for q in ex.map(lambda s: _mkt_one(*s), _MKT_SYMBOLS) if q]
    chart = next((r for r in rows if len(r.get("series", [])) >= 12), None)
    if not chart and rows:
        chart = max(rows, key=lambda r: len(r.get("series", [])))
    return {"quotes": [{k: r[k] for k in ("sym", "price", "chg", "series")} for r in rows],
            "chart": {"label": chart["sym"], "series": chart["series"],
                      "chg": chart["chg"]} if chart else None}


# ── broadcast desk — freshest news bulletin per channel (YouTube RSS, no key) ─
# WHY VODs, not live streams: YouTube serves live ONLY in H.264, and the PyQt6
# WebEngine build has no H.264 decoder. Bulletins fall back to VP9 → they play.
_CAST_CHANNELS = [("AL JAZEERA", "UCNye-wNBqNL5ZzHSJj3l8Bg"),
                  ("DW", "UCknLrEdhRCp1aegoMqRaCZg"),
                  ("FRANCE 24", "UCQfwfsi5VrQ8yKZ-UWmAEFg"),
                  ("BLOOMBERG", "UCIALMKvObZNtJ6AmdCLP7Lg")]
_cast_cache: dict = {"t": 0.0, "v": {}}


def _latest_bulletin(name: str, cid: str) -> tuple[str, str] | None:
    try:
        import requests
        xml = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                           headers=_MKT_UA, timeout=8).text
        entries = re.findall(r"<entry>.*?</entry>", xml, re.DOTALL)
        for ent in entries[:12]:
            vid = (re.search(r"<yt:videoId>([^<]+)", ent) or [None, ""])[1]
            title = (re.search(r"<title>([^<]+)", ent) or [None, ""])[1]
            if vid and "#short" not in title.lower():
                return name, vid
    except Exception:
        pass
    return None


def latest_bulletins() -> dict:
    """{channel name → newest non-short video id}, cached for 30 minutes."""
    if time.time() - _cast_cache["t"] < 1800 and _cast_cache["v"]:
        return _cast_cache["v"]
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(lambda c: _latest_bulletin(*c), _CAST_CHANNELS):
            if r:
                out[r[0]] = r[1]
    if out:
        _cast_cache.update({"t": time.time(), "v": out})
    return out


# ── the wall ─────────────────────────────────────────────────────────────────
def board_payload(jarvis=None) -> dict:
    ms = _load()
    active = [m for m in ms if m.get("status") == "active"]
    done   = [m for m in ms if m.get("status") == "done"][-4:]

    sysinfo = {}
    try:
        import psutil
        sysinfo = {"cpu": round(psutil.cpu_percent(interval=0.2)),
                   "mem": round(psutil.virtual_memory().percent),
                   "disk": round(psutil.disk_usage("/").percent),
                   "up": int(time.time() - psutil.boot_time()) // 3600}
    except Exception:
        pass

    sentinel = "perimeter quiet · all processes nominal"
    try:
        from actions.security import fs_check
        alert = (fs_check() or {}).get("alert")
        if alert:
            sentinel = f"⚠ {alert}"
    except Exception:
        pass

    intel = []
    try:
        w = getattr(jarvis, "_last_world", None) if jarvis else None
        intel = (w or {}).get("feed", [])[:8]
    except Exception:
        pass

    activity = []
    try:
        from actions.nightwatch import _log_path
        lines = _log_path().read_text(encoding="utf-8").strip().splitlines()[-6:]
        for l in lines:
            r = json.loads(l)
            activity.append(f"{r.get('t','')} {r.get('text','')[:70]}")
    except Exception:
        pass

    market = {}
    try:
        market = markets_snapshot()
    except Exception:
        pass

    casts = {}
    try:
        casts = latest_bulletins()
    except Exception:
        pass

    # self-telemetry: real API burn + models used today; watchers = night-watch state
    burn, models = "$0.00", 0
    try:
        from core.llm_client import telemetry_snapshot
        t = telemetry_snapshot()
        burn, models = f"${t['cost']:.2f}", t["models"]
    except Exception:
        pass
    watchers = 0
    try:
        from actions.nightwatch import is_active
        watchers = 4 if is_active() else 0
    except Exception:
        pass

    builds = []
    try:
        from core.build_tracker import active as _builds_active
        builds = _builds_active()
    except Exception:
        pass

    return {"missions": active, "done": done, "system": sysinfo,
            "sentinel": sentinel, "intel": intel, "activity": activity,
            "market": market, "casts": casts, "burn": burn,
            "models": models, "watchers": watchers, "builds": builds,
            "stamp": f"{datetime.now():%a %d %b · %H:%M}"}
