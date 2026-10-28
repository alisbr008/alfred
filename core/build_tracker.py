"""
Build tracker — lets multiple Developer-Mode builds run CONCURRENTLY and shows
them live on the mission board. Each build already runs on its own thread in its
own workspace dir, so they never collide; this just gives them visibility and a
shared "council" queue.

In-process, thread-safe. Cheap: pure dict updates. Recently-finished builds linger
briefly so the board can show the ✓/✗ before they fade.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_BUILDS: dict = {}      # id -> {name, phase, progress, status, started, ended, summary}
_SEQ = 0


def start(name: str) -> int:
    global _SEQ
    with _LOCK:
        _SEQ += 1
        bid = _SEQ
        _BUILDS[bid] = {"id": bid, "name": (name or "build")[:60], "phase": "planning",
                        "progress": 5, "status": "running", "started": time.time(),
                        "ended": 0.0, "summary": ""}
    return bid


def phase(bid: int, phase: str, progress: int | None = None) -> None:
    with _LOCK:
        b = _BUILDS.get(bid)
        if not b:
            return
        b["phase"] = phase[:40]
        if progress is not None:
            b["progress"] = max(0, min(99, int(progress)))
        else:
            b["progress"] = min(95, b["progress"] + 12)


def done(bid: int, ok: bool, summary: str = "") -> None:
    with _LOCK:
        b = _BUILDS.get(bid)
        if not b:
            return
        b.update({"status": "done" if ok else "failed", "phase": "complete" if ok else "failed",
                  "progress": 100, "ended": time.time(), "summary": (summary or "")[:80]})


def active(keep_done_sec: int = 60) -> list[dict]:
    """Running builds + very recently finished ones (so the ✓ is visible briefly)."""
    now = time.time()
    out = []
    with _LOCK:
        for bid in list(_BUILDS):
            b = _BUILDS[bid]
            if b["status"] == "running" or (now - b["ended"]) < keep_done_sec:
                out.append(dict(b))
            elif b["ended"] and (now - b["ended"]) >= keep_done_sec:
                _BUILDS.pop(bid, None)      # garbage-collect old ones
    out.sort(key=lambda x: x["started"])
    return out


def running_count() -> int:
    with _LOCK:
        return sum(1 for b in _BUILDS.values() if b["status"] == "running")
