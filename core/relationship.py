"""
Persistent relationship — turns Alfred from a chatbot into a companion that
remembers what you're building, what you worked on, and where you left off.

Builds on the episodic memory (core/learning) + the fact memory (projects) +
the task tracker. Produces a 'continuity' recap for the morning briefing and for
"what was I working on / where did I leave off".
"""
from __future__ import annotations

import datetime as _dt

from core.learning import _load, _keywords, _EPISODES


def _day_window(offset_days: int):
    day = (_dt.datetime.now() - _dt.timedelta(days=offset_days)).date()
    start = _dt.datetime.combine(day, _dt.time.min).timestamp()
    end = _dt.datetime.combine(day, _dt.time.max).timestamp()
    return start, end


def _distinct_topics(episodes: list, limit: int = 5) -> list:
    """Dedup similar requests so the recap lists *distinct* threads of work."""
    topics, seen = [], []
    for e in episodes:
        u = (e.get("user") or "").strip()
        kw = _keywords(u)
        if not u or not kw:
            continue
        if any(len(kw & s) / max(1, len(kw | s)) >= 0.5 for s in seen):
            continue                       # too similar to one already listed
        seen.append(kw)
        topics.append(u)
        if len(topics) >= limit:
            break
    return topics


def _active_projects() -> list:
    try:
        from memory.memory_manager import load_memory
        proj = (load_memory() or {}).get("projects", {})
        out = []
        for k, v in list(proj.items())[:5]:
            val = v.get("value") if isinstance(v, dict) else v
            out.append(str(val or k))
        return out
    except Exception:
        return []


def _pending_tasks() -> str:
    try:
        from actions.tasks_tool import task_tracker
        r = task_tracker({"action": "list"})
        return r if r and "No active" not in r else ""
    except Exception:
        return ""


# Fragments that are conversation mechanics, not work — never worth recapping.
_JUNK = {"wait", "stop", "hold on", "hold up", "pause", "cancel", "nevermind", "never mind",
         "yes", "no", "okay", "ok", "yeah", "thanks", "thank you", "alfred"}


def _clean_topic(u: str) -> str:
    import re
    t = re.sub(r"^(?:hey\s+)?alfred[,\s]+", "", (u or "").strip(), flags=re.IGNORECASE)
    t = t.strip().rstrip(".?!")
    return "" if (not t or t.lower() in _JUNK or len(t) < 8) else t


def recap(spoken: bool = True) -> str:
    """A continuity briefing — SYNTHESISED butler prose, never a robotic replay of
    the user's raw voice commands."""
    eps = _load(_EPISODES)
    ys, ye = _day_window(1)
    y_eps = [e for e in eps if ys <= e.get("ts", 0) <= ye]
    # if nothing yesterday, fall back to the most recent day with activity
    if not y_eps:
        for off in range(2, 8):
            s, e = _day_window(off)
            y_eps = [ep for ep in eps if s <= ep.get("ts", 0) <= e]
            if y_eps:
                break

    topics = [t for t in (_clean_topic(x) for x in _distinct_topics(y_eps, limit=8)) if t]
    projects = _active_projects()
    tasks = _pending_tasks()
    if not topics and not projects and not tasks:
        return "We haven't logged any work yet, sir — once we get going I'll remember it."

    when = "yesterday" if any(ys <= e.get("ts", 0) <= ye for e in y_eps) else "last session"
    raw = ""
    if topics:
        raw += f"Voice-command fragments from {when} (raw, may be messy): {'; '.join(topics)}\n"
    if projects:
        raw += f"Project notes on file: {'; '.join(projects)}\n"
    if tasks:
        raw += f"Open tasks: {tasks}\n"

    try:
        from core.llm_client import call_llm_text
        body = (call_llm_text(
            raw + "\nWrite the continuity part of the user's morning briefing.",
            system=("You are Alfred, a composed British butler. From the raw fragments, infer "
                    "what the user was actually WORKING ON and write 2–3 short spoken sentences: "
                    "the threads of work, and where he left off. Natural prose — NEVER quote or "
                    "repeat the raw commands verbatim, never list them, no markdown. Address him "
                    "as 'sir' at most once."),
            tier="main", timeout=30, max_tokens=220) or "").strip()
    except Exception:
        body = ""
    if not body:
        first = topics[0] if topics else (projects[0] if projects else "the usual")
        body = f"You spent {when} working around {first}, sir."
    return (body + " Shall we pick up where we left off?") if spoken else body


def catch_up(spoken: bool = True) -> str:
    """'What did I miss / what happened while I was away' — a proper debrief: what
    we were working on, anything the night watch logged, and the latest world news.
    NOT a web search (that mis-routed to Perplexity)."""
    blocks = []

    # 1) continuity — what we worked on / where we left off
    try:
        r = recap(spoken=False)
        if r and "haven't logged" not in r:
            blocks.append(("Where we left off", r))
    except Exception:
        pass

    # 2) night watch debrief, if it ran
    try:
        from actions.nightwatch import todays_digest
        nd = todays_digest()
        if nd:
            blocks.append(("Overnight", nd))
    except Exception:
        pass

    # 3) latest world headlines
    try:
        from actions.world_model import world_feed
        feed = (world_feed() or {}).get("feed", [])[:4]
        if feed:
            news = " · ".join(f"{f['cat']}: {f['text']}" for f in feed)
            blocks.append(("In the world", news))
    except Exception:
        pass

    if not blocks:
        return "Not a great deal while you were away, sir — the desk was quiet."

    raw = "\n".join(f"{h}: {b}" for h, b in blocks)
    # Smooth it into a short spoken butler debrief.
    try:
        from core.llm_client import call_llm_text
        out = call_llm_text(
            raw + "\n\nBrief the user on what they missed while away.",
            system=("You are Alfred, a composed British butler welcoming the user back. Weave the "
                    "notes into 3-5 short spoken sentences: what you were working on together, "
                    "anything overnight, then a couple of notable world items. Warm, natural prose, "
                    "no markdown, address him as 'sir' once."),
            tier="main", timeout=30, max_tokens=320) or ""
        out = out.strip()
        if out:
            return ("Welcome back, sir. " + out) if not out.lower().startswith(("welcome", "good")) else out
    except Exception:
        pass
    return "Welcome back, sir. " + " ".join(f"{h}: {b}" for h, b in blocks)
