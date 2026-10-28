"""
Alfred's growth layer — makes him learn from mistakes instead of starting cold
every turn. Two stores under memory/:

  • lessons.json  — corrections. When you tell Alfred he got something wrong, the
                    fix is captured and re-injected next time the same kind of
                    request comes up. ("learn from mistakes")

  • episodes.json — a memory stream of past (request → reply) turns. Before a new
                    request, the most similar past turns are recalled and given to
                    Alfred as context, so he gets sharper the more you use him.

Dependency-free: keyword overlap + recency (no embeddings needed for v1).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_LESSONS  = _base() / "memory" / "lessons.json"
_EPISODES = _base() / "memory" / "episodes.json"

_STOP = {"the", "a", "an", "to", "for", "of", "and", "or", "is", "it", "you",
         "me", "my", "i", "do", "did", "can", "could", "would", "please", "alfred",
         "that", "this", "with", "on", "in", "at", "be", "was", "not", "no", "yes",
         "what", "how", "why", "when", "are", "your", "have", "get", "got", "want"}

_CORRECTION_CUES = (
    "no, i meant", "no i meant", "i meant", "that's wrong", "thats wrong",
    "that's not", "thats not", "i didn't ask", "i did not ask", "don't do that",
    "dont do that", "i told you", "not that", "you're wrong", "youre wrong",
    "that's incorrect", "wrong answer", "stop doing", "never do", "not what i",
    "you got it wrong", "that's not what",
)


def _load(p: Path) -> list:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(p: Path, data: list) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data[-400:], indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Learning] save {p.name} failed: {e}")


def _keywords(text: str) -> set:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) >= 3 and t not in _STOP}


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── corrections ──────────────────────────────────────────────────────────────

def detect_correction(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(cue in t for cue in _CORRECTION_CUES)


def add_lesson(trigger: str, correction: str) -> None:
    """Store: when the user asked `trigger`, they corrected with `correction`."""
    if not (trigger and correction):
        return
    items = _load(_LESSONS)
    items.append({"trigger": trigger[:160], "kw": list(_keywords(trigger)),
                  "correction": correction[:200], "ts": time.time()})
    _save(_LESSONS, items)
    print(f"[Learning] 📝 lesson saved for: {trigger[:50]}")


def relevant_lessons(text: str, k: int = 3) -> list:
    kw = _keywords(text)
    scored = []
    for it in _load(_LESSONS):
        s = _overlap(kw, set(it.get("kw", [])))
        if s >= 0.2:
            scored.append((s, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:k]]


# ── episodic memory ──────────────────────────────────────────────────────────

def record_episode(user: str, reply: str) -> None:
    if not (user and reply):
        return
    items = _load(_EPISODES)
    items.append({"user": user[:200], "reply": reply[:300],
                  "kw": list(_keywords(user)), "ts": time.time()})
    _save(_EPISODES, items)
    # Also embed it into semantic memory (async, cloud, never blocks the turn).
    try:
        from core.semantic_memory import index_async
        index_async(user, "episode")
    except Exception:
        pass


def _recall_keyword(text: str, k: int) -> list:
    kw = _keywords(text)
    cur = (text or "").strip().lower()
    scored = []
    for it in _load(_EPISODES):
        if it.get("user", "").strip().lower() == cur:
            continue
        s = _overlap(kw, set(it.get("kw", [])))
        if s >= 0.34:
            scored.append((s, it))
    scored.sort(key=lambda x: (x[0], x[1].get("ts", 0)), reverse=True)
    return [it for _, it in scored[:k]]


def recall_episodes(text: str, k: int = 2) -> list:
    """Recall by MEANING (NIM embeddings) with a keyword fallback. Semantic search
    finds 'the neon jump game' ↔ 'geometry dash' that keyword overlap would miss."""
    cur = (text or "").strip().lower()
    try:
        from core.semantic_memory import search
        hits = search(text, k=k + 1, kinds=["episode"], min_sim=0.34)
        out = [{"user": h["text"], "reply": "", "ts": h.get("ts", 0), "score": h["score"]}
               for h in hits if h["text"].strip().lower() != cur]
        if out:
            return out[:k]
    except Exception:
        pass
    return _recall_keyword(text, k)


def recall_knowledge(text: str, k: int = 3) -> list:
    """Cross-source semantic recall over night-log events + Obsidian vault notes."""
    try:
        from core.semantic_memory import search
        return search(text, k=k, kinds=["nightlog", "vault"], min_sim=0.34)
    except Exception:
        return []


# ── prompt injection ─────────────────────────────────────────────────────────

def context_for_prompt(text: str) -> str:
    """Build the learning context block for the system prompt, or '' if nothing
    relevant. Kept small to avoid bloat."""
    parts = []
    lessons = relevant_lessons(text)
    if lessons:
        parts.append("[LEARNED CORRECTIONS — honour these]")
        for l in lessons:
            parts.append(f'• When asked "{l["trigger"]}", the user corrected: '
                         f'"{l["correction"]}" — do it that way.')
    eps = recall_episodes(text)
    if eps:
        parts.append("[SIMILAR PAST TURNS — for continuity]")
        for e in eps:
            r = (e.get("reply") or "").strip()
            parts.append(f'• You handled "{e["user"]}"' + (f' → "{r[:120]}"' if r else ""))
    # Cross-source semantic recall: relevant night-log events + vault notes.
    know = recall_knowledge(text)
    if know:
        parts.append("[RELEVANT FROM YOUR RECORDS]")
        for h in know:
            tag = "vault" if h.get("kind") == "vault" else "logged"
            parts.append(f'• ({tag}) {h["text"][:140]}')
    return "\n".join(parts)
