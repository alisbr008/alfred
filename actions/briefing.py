"""
MORNING BRIEFING — "Good morning, sir." The batcomputer waking with you.

On command (or on the first wake of the day), Alfred assembles one spoken brief from
everything he already watches: the day's calendar, what the tripwires caught overnight,
the markets, the top of the news, and where your missions stand. Sources are gathered
in parallel and any that fail are simply left out — the brief never blocks on one wire.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


def _greeting() -> str:
    h = datetime.now().hour
    return ("Good morning" if 4 <= h < 12 else
            "Good afternoon" if 12 <= h < 18 else "Good evening")


def _calendar() -> str:
    try:
        from actions.calendar_tool import calendar_tool
        out = (calendar_tool({"action": "today"}) or "").strip()
        low = out.lower()
        if any(x in low for x in ("clear today", "couldn't read", "couldn't", "error",
                                  "isn't running", "took too long")):
            return "your calendar is clear" if "clear" in low else ""
        # keep it to the first few events, spoken-friendly
        lines = [l.strip("•- ").strip() for l in out.splitlines()
                 if l.strip() and "schedule" not in l.lower()]
        if not lines:
            return "your calendar is clear"
        n = len(lines)
        head = "; ".join(lines[:3])
        return f"{n} thing{'s' if n != 1 else ''} on today — {head}" + (", and more" if n > 3 else "")
    except Exception:
        return ""


def _tripwires() -> str:
    try:
        from actions.tripwires import _load, check_all
        wires = _load()
        if not wires:
            return ""
        fired = check_all(source="briefing")            # also delivers any live hits
        armed = len(wires)
        if fired:
            return f"{len(fired)} tripwire{'s' if len(fired) != 1 else ''} tripped — {fired[0]}"
        return f"{armed} tripwire{'s' if armed != 1 else ''} armed, all quiet overnight"
    except Exception:
        return ""


def _markets() -> str:
    try:
        from actions.missions import markets_snapshot
        q = markets_snapshot().get("quotes", [])[:3]
        if not q:
            return ""
        bits = []
        for x in q:
            d = "up" if (x.get("chg") or 0) >= 0 else "down"
            p = x.get("price") or 0
            ps = f"{p:,.0f}" if p >= 1000 else f"{p:.0f}"
            bits.append(f"{x['sym']} {ps}, {d} {abs(x.get('chg') or 0):.1f}")
        return "markets — " + "; ".join(bits)
    except Exception:
        return ""


def _news() -> str:
    try:
        import html, re
        from actions.world_model import world_feed
        feed = (world_feed() or {}).get("feed", [])[:2]
        if not feed:
            return ""
        heads = []
        for f in feed:
            txt = html.unescape(f.get("text", "")).strip()
            txt = re.sub(r"\s+-\s+[A-Z][\w .&]+$", "", txt)   # drop trailing " - Source"
            if txt:
                heads.append(txt[:90])
        return "top of the wire — " + "; ".join(heads) if heads else ""
    except Exception:
        return ""


def _missions() -> str:
    try:
        from actions.missions import speak_status
        s = speak_status()
        return "" if "board is clear" in s.lower() else s.rstrip(".")
    except Exception:
        return ""


def morning_brief(jarvis=None) -> str:
    """Assemble and return the spoken brief. Marks today as briefed."""
    if jarvis is not None:
        jarvis._briefed_day = datetime.now().date()
    ui = getattr(jarvis, "ui", None) if jarvis else None
    if ui:
        try: ui.write_log("SYS: 📋 Assembling your brief…")
        except Exception: pass

    with ThreadPoolExecutor(max_workers=6) as ex:
        cal = ex.submit(_calendar); trip = ex.submit(_tripwires)
        mkt = ex.submit(_markets); news = ex.submit(_news); mis = ex.submit(_missions)
        cal, trip, mkt, news, mis = cal.result(), trip.result(), mkt.result(), news.result(), mis.result()

    name = (jarvis and getattr(jarvis, "_user_name", None)) or "sir"
    parts = [f"{_greeting()}, {name}."]
    if cal:  parts.append(cal.capitalize() + ".")
    if mis:  parts.append(mis + ".")
    if trip: parts.append(trip.capitalize() + ".")
    if mkt:  parts.append(mkt.capitalize() + ".")
    if news: parts.append(news.capitalize() + ".")
    if len(parts) == 1:
        parts.append("All quiet — nothing on the calendar, the wires, or the board.")
    else:
        parts.append("That's your morning, sir.")
    return " ".join(parts)


def maybe_auto_brief(jarvis) -> str | None:
    """First interaction of a new day → offer the brief once (used by the awareness
    loop). Returns the brief text if it should be spoken now, else None."""
    try:
        today = datetime.now().date()
        if getattr(jarvis, "_briefed_day", None) == today:
            return None
        if not (6 <= datetime.now().hour < 12):          # only in the morning window
            return None
        return morning_brief(jarvis)
    except Exception:
        return None
