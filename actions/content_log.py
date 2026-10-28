"""
Echo's content log — a tiny local record of every draft written and post
published, so the command-centre pipeline (Drafts / Scheduled / Published /
Engagement) reflects REAL activity instead of mock numbers.

Stored at memory/content_log.json.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_LOG = _base_dir() / "memory" / "content_log.json"


def _load() -> list:
    try:
        return json.loads(_LOG.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        _LOG.write_text(json.dumps(items[-200:], indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[ContentLog] save failed: {e}")


def add_draft(content: str, platform: str = "X") -> str:
    """Record a draft Echo wrote. Returns its id."""
    items = _load()
    did = uuid.uuid4().hex[:8]
    items.append({"id": did, "ts": time.time(), "platform": platform,
                  "content": content, "status": "draft", "tweet_id": None})
    _save(items)
    return did


def mark_published(draft_id: str | None, tweet_id: str = "") -> None:
    """Flip the latest draft (or a specific id) to published."""
    items = _load()
    target = None
    if draft_id:
        target = next((i for i in items if i["id"] == draft_id), None)
    if target is None:
        target = next((i for i in reversed(items) if i["status"] == "draft"), None)
    if target:
        target["status"] = "published"
        target["tweet_id"] = tweet_id
        target["published_ts"] = time.time()
        _save(items)


def latest_draft() -> dict | None:
    for i in reversed(_load()):
        if i["status"] == "draft":
            return i
    return None


def stats() -> dict:
    items = _load()
    today = time.time() - 86400
    drafts    = sum(1 for i in items if i["status"] == "draft")
    published = sum(1 for i in items if i["status"] == "published")
    pub_today = sum(1 for i in items if i["status"] == "published"
                    and i.get("published_ts", 0) > today)
    return {"drafts": drafts, "scheduled": 0, "published": published,
            "published_today": pub_today, "engagement": "—"}
