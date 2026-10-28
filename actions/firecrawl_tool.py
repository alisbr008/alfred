"""
Firecrawl — Alfred reads any website (clean text), so he can research pages and
keep an eye on competitors' public profiles/sites. Returns a concise summary.

Config: firecrawl_api_key
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import requests
    _REQ = True
except ImportError:
    _REQ = False


def _cfg() -> dict:
    try:
        base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent)
        return json.loads((base / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _norm_url(s: str) -> str | None:
    s = (s or "").strip().strip("'\"")
    m = re.search(r"(https?://\S+)", s)
    if m:
        return m.group(1)
    m = re.search(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)", s, re.I)
    return f"https://{m.group(1)}" if m else None


def _scrape(url: str, key: str) -> str | None:
    if not (_REQ and key):
        return None
    try:
        r = requests.post("https://api.firecrawl.dev/v1/scrape",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                          timeout=60)
        r.raise_for_status()
        return (r.json().get("data", {}) or {}).get("markdown", "")
    except Exception as e:
        print(f"[Firecrawl] {e}")
        return None


def firecrawl_read(parameters: dict, response=None, player=None,
                   session_memory=None, jarvis=None, speak=None) -> str:
    params = parameters or {}
    raw = (params.get("url") or params.get("request") or params.get("query")
           or params.get("text") or "").strip()
    url = _norm_url(raw)
    if not url:
        return "Which site should I read, sir? Give me a URL."

    key = _cfg().get("firecrawl_api_key", "").strip()
    if not key:
        return "Firecrawl isn't connected yet, sir — add 'firecrawl_api_key' to the config."

    if player:
        try: player.write_log(f"SYS: 🌐 Reading {url}…")
        except Exception: pass

    md = _scrape(url, key)
    if not md:
        return f"I couldn't read that page, sir."

    # Alfred distils it (intent-aware) instead of dumping raw text.
    want = params.get("request") or "the key points"
    try:
        from core.llm_client import call_llm_text
        summary = call_llm_text(
            f"From this page content, give Ali a concise, useful brief on {want}. "
            f"Be specific, no fluff, address him as 'sir'.\n\nPAGE:\n{md[:6000]}",
            system="You are Alfred. Summarise web content sharply.", tier="main")
        return summary or md[:500]
    except Exception:
        return md[:600]
