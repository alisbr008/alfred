"""
Nightly self-reflection — Alfred distils the day into lessons.

At the end of each day (fired from Night-Watch stand-down, or on demand), he reads
the day's episodes, and Haiku distils them into: a one-line summary, concrete
lessons ("the user prefers X"), and open threads to pick up. Lessons feed straight
back into the learning layer so he genuinely gets sharper over time; the whole
reflection is embedded into semantic memory so he can recall "what did I learn."

Cheap: one Haiku call per day. Safe: pure read + write to memory, no side effects.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_REFLECTIONS = _base() / "memory" / "reflections.jsonl"

_SYS = (
    "You are Alfred, a butler reflecting privately on your day serving the user. From the day's "
    "request/reply episodes, produce STRICT JSON:\n"
    '{"summary":"one sentence on what the day was about",'
    '"lessons":["durable preferences or facts about the user worth remembering, imperative"],'
    '"open_threads":["unfinished work to pick up next"]}\n'
    "Infer real patterns — never quote raw commands. 2-4 lessons max, 0-3 threads. JSON only."
)


def _today_episodes() -> list[dict]:
    from core.learning import _load, _EPISODES
    now = _dt.datetime.now()
    start = _dt.datetime.combine(now.date(), _dt.time.min).timestamp()
    return [e for e in _load(_EPISODES) if e.get("ts", 0) >= start]


def reflect_on_day(jarvis=None, spoken: bool = False) -> str:
    eps = _today_episodes()
    if len(eps) < 2:
        return "Too quiet a day to reflect on, sir." if spoken else ""

    convo = "\n".join(f"- {(e.get('user') or '')[:120]}" for e in eps[-40:])
    data = {}
    try:
        from core.llm_client import call_llm_text
        raw = call_llm_text(f"Today's episodes:\n{convo}", system=_SYS,
                            tier="main", timeout=40, max_tokens=500)
        import re
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}

    summary = (data.get("summary") or "").strip()
    lessons = [str(x).strip() for x in (data.get("lessons") or []) if str(x).strip()][:4]
    threads = [str(x).strip() for x in (data.get("open_threads") or []) if str(x).strip()][:3]
    if not summary and not lessons:
        return "Nothing worth noting today, sir." if spoken else ""

    rec = {"date": f"{_dt.date.today()}", "summary": summary,
           "lessons": lessons, "open_threads": threads, "episodes": len(eps)}
    try:
        _REFLECTIONS.parent.mkdir(parents=True, exist_ok=True)
        with _REFLECTIONS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # Feed durable lessons into the learning layer + embed the reflection.
    try:
        from core.learning import add_lesson
        for l in lessons:
            add_lesson(f"[reflection {rec['date']}]", l)
    except Exception:
        pass
    try:
        from core.semantic_memory import index_async
        index_async(f"Reflection {rec['date']}: {summary} " + " ".join(lessons), "reflection")
    except Exception:
        pass

    if jarvis is not None:
        try:
            jarvis.ui.write_log(f"SYS: 🌙 Reflected on the day — {len(lessons)} lesson(s) learned.")
        except Exception:
            pass

    if spoken:
        parts = [summary] if summary else []
        if lessons:
            parts.append("I've noted that " + lessons[0].rstrip("."))
        if threads:
            parts.append("Still open: " + threads[0].rstrip("."))
        return " ".join(parts) + "." if parts else "Reflected, sir."
    return summary


def latest_reflection() -> dict:
    try:
        lines = _REFLECTIONS.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}
