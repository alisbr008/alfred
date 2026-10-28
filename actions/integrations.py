"""
Integrations registry — the single source of truth for everything Alfred can
connect to, and whether it's currently connected (based on config keys). Powers
the INTEGRATIONS dashboard tab.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _cfg() -> dict:
    try:
        base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent)
        return json.loads((base / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# Apps only (real products with logos) — not internal tools/libraries.
# (category, name, domain-for-logo, what it does, [required config keys])
_REGISTRY = [
    ("Brain", "Anthropic", "anthropic.com", "Claude Haiku · Sonnet — the brain", ["anthropic_key"]),
    ("Brain", "NVIDIA NIM", "nvidia.com", "Qwen3 — runs the subagents", ["llm_api_key"]),
    ("Voice", "Fish Audio", "fish.audio", "Alfred's spoken voice", ["fish_api_key"]),
    ("Knowledge", "Tavily", "tavily.com", "Live, cited web research", ["tavily_key"]),
    ("Knowledge", "Obsidian", "obsidian.md", "Your second brain", ["obsidian_vault"]),
    ("Knowledge", "Gmail", "gmail.com", "Reads your unread mail", ["gmail_app_password"]),
    ("Brain", "Z.ai (GLM 5.1)", "z.ai", "Lead Engineer — coding & builds", ["llm_api_key"]),
    ("Creative", "Poly Pizza", "poly.pizza", "Real 3D models", ["polypizza_key"]),
    ("Creative", "Replicate", "replicate.com", "Image & video generation", ["replicate_api_token"]),
    ("Knowledge", "Firecrawl", "firecrawl.dev", "Read any website / competitors", ["firecrawl_api_key"]),
    ("Creative", "Higgsfield", "higgsfield.ai", "Cinematic AI video — Alfred writes the prompt", ["__paste__"]),
    ("Build", "Claude Code", "claude.com", "Autonomous coding agent — full builds", ["__local__"]),
    ("Social", "X", "x.com", "Echo posts to @veltharlab", ["x_api_key", "x_access_token"]),
    ("Social", "Instagram", "instagram.com", "Alfred posts photos + captions", ["instagram_username", "instagram_password"]),
    ("Remote", "Telegram", "telegram.org", "Command Alfred from your phone", ["telegram_bot_token"]),
]

# Keyless connectors that are "connected" by nature of being local / paste-driven.
_KEYLESS = {"__paste__": "prompt → paste", "__local__": "local agent"}


def integrations_status() -> dict:
    c = _cfg()
    cats: dict = {}
    connected = total = 0
    for cat, name, domain, desc, keys in _REGISTRY:
        total += 1
        keyless = len(keys) == 1 and keys[0] in _KEYLESS
        ok = keyless or (bool(keys) and all(str(c.get(k, "")).strip() for k in keys))
        if ok:
            connected += 1
        cats.setdefault(cat, []).append({
            "name": name, "domain": domain, "desc": desc,
            "connected": ok,
            "needs": _KEYLESS[keys[0]] if keyless else ("" if ok else "needs key"),
        })
    return {"categories": cats, "connected": connected, "total": total}
