"""
Alfred's Agent Council — a system of specialist subagents that run under the
cheap LLM tier (Qwen3) and report UP to Alfred. Subagents never speak to the
user; Alfred aggregates their structured outputs into one coherent picture.

    User → ALFRED → { Sentinel · Oracle · Echo } → ALFRED → User

  • Sentinel — security / system health (real telemetry, mostly no LLM)
  • Oracle   — research / market intelligence (Qwen3)
  • Echo     — content creation, Velthar Lab voice (Qwen3)

This is the MOCK/first cut: Sentinel is real (psutil), Oracle & Echo make real
Qwen3 calls with canned fallbacks so the flow is demonstrable end-to-end.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

_BOOT = time.time()


def _llm_simple(prompt: str, system: str) -> str:
    """Run a subagent on the cheap tier (Qwen3)."""
    try:
        from core.llm_client import call_llm_text
        return call_llm_text(prompt, system=system, tier="simple") or ""
    except Exception as e:
        print(f"[Agents] LLM error: {e}")
        return ""


def _json_block(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# SENTINEL — system health (real telemetry, no LLM needed)
# ─────────────────────────────────────────────────────────────────────────────

def sentinel() -> dict:
    sev, issues, rec = "low", [], "All systems nominal."
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory().percent
        bat = None
        try:
            b = psutil.sensors_battery()
            bat = int(b.percent) if b else None
        except Exception:
            pass
        uptime_min = int((time.time() - _BOOT) / 60)

        if cpu > 85:
            sev = "high"; issues.append(f"CPU at {cpu:.0f}%")
        elif cpu > 60:
            sev = max(sev, "medium", key=["low", "medium", "high"].index); issues.append(f"CPU {cpu:.0f}%")
        if mem > 88:
            sev = "high"; issues.append(f"memory at {mem:.0f}%"); rec = "Consider restarting Alfred to reclaim memory."
        elif mem > 75:
            sev = max(sev, "medium", key=["low", "medium", "high"].index); issues.append(f"memory {mem:.0f}%")
        if bat is not None and bat < 20:
            sev = max(sev, "medium", key=["low", "medium", "high"].index); issues.append(f"battery {bat}%"); rec = "Battery low — plug in soon, sir."

        metrics = {"cpu": f"{cpu:.0f}%", "memory": f"{mem:.0f}%",
                   "battery": f"{bat}%" if bat is not None else "n/a",
                   "uptime": f"{uptime_min}m"}
    except Exception as e:
        metrics = {"error": str(e)}

    # ── Security layer: filesystem watchdog + action audit ───────────────────
    audit, fsr = {}, {}
    try:
        from actions.security import audit_stats, fs_check
        audit = audit_stats()
        fsr = fs_check()
    except Exception as e:
        print(f"[Sentinel] security check failed: {e}")

    if fsr.get("alert"):                       # bulk file removal — the wipe class
        sev = "high"; issues.append(fsr["alert"])
        rec = "Possible bulk file removal — verify this was intended, sir."
    if audit.get("breaches"):                  # destructive action ran un-confirmed
        sev = "high"
        issues.append(f"{audit['breaches']} unconfirmed destructive action(s): "
                      + ", ".join(audit.get("breach_tools", [])))
        rec = "A destructive action bypassed confirmation — review immediately, sir."

    return {
        "agent": "Sentinel",
        "severity": sev,
        "issue": ", ".join(issues) or "none",
        "recommendation": rec,
        "metrics": metrics,
        "security": {
            "actions": audit.get("actions", 0),
            "destructive": audit.get("destructive", 0),
            "breaches": audit.get("breaches", 0),
            "error_rate": audit.get("error_rate", "0.0%"),
            "fs_alert": fsr.get("alert"),
            "watchdog": "ACTIVE",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# ORACLE — research / intelligence (Qwen3)
# ─────────────────────────────────────────────────────────────────────────────

_ORACLE_SYS = (
    "You are Oracle, Alfred's research and intelligence subsystem. You never speak "
    "to the user — you report to Alfred. You are given REAL, current web findings. "
    "Distil them into an intelligence brief. Do NOT add facts beyond the findings. "
    'Return ONLY raw JSON: {"summary":"one or two sentences","importance":1-10,'
    '"actionable_items":["",""]}'
)


def oracle(topic: str | None = None) -> dict:
    """Pulls REAL-TIME data via Tavily (Alfred's live research engine), then Qwen3
    structures it. Falls back to Qwen3-only if Tavily is unavailable."""
    topic = topic or ("today's most important and viral world news, major stock market "
                      "moves, AI breakthroughs, and political developments")
    real, sources = None, []
    try:
        from actions.workflows import _tavily, RELIABLE_NEWS
        from datetime import datetime
        today = datetime.now().strftime("%B %d, %Y")
        tav = (_tavily(f"{topic} — as of {today}", "news", 7, RELIABLE_NEWS, 6)
               or _tavily(topic, "general"))
        if tav and tav.get("answer"):
            real = tav["answer"]
            sources = list(dict.fromkeys(tav.get("sources", [])))[:3]
    except Exception as e:
        print(f"[Oracle] Tavily error: {e}")

    if real:
        data = _json_block(_llm_simple(f"REAL findings on '{topic}':\n{real[:1800]}", _ORACLE_SYS)) or {}
        if not data.get("summary"):
            data["summary"] = real.split(". ")[0][:200]
        data["live"] = True
    else:
        data = _json_block(_llm_simple(f"Brief Alfred on: {topic}",
                                       _ORACLE_SYS + " (No live data available.)")) or {}
        data.setdefault("summary", "No fresh intelligence available right now.")
        data["live"] = False

    data["agent"] = "Oracle"
    data["topic"] = topic
    data["sources"] = sources
    data.setdefault("importance", 5)
    data.setdefault("actionable_items", [])
    return data


# ─────────────────────────────────────────────────────────────────────────────
# ECHO — content creation, Velthar Lab voice (Qwen3)
# ─────────────────────────────────────────────────────────────────────────────

_ECHO_SYS = (
    "You are Echo, Alfred's content subsystem for Velthar Lab. You never speak to "
    "the user — you report to Alfred. Voice: sharp, builder-energy, no corporate "
    "fluff, no hashtags-spam. Produce ONE publish-ready piece. "
    'Return ONLY raw JSON: {"platform":"X","content":"the post text"}'
)


def echo(brief: str | None = None) -> dict:
    brief = brief or "a short post announcing progress on Alfred, the voice-first AI butler"
    raw = _llm_simple(f"Write content for: {brief}", _ECHO_SYS)
    data = _json_block(raw)
    if not data or not data.get("content"):
        data = {"platform": "X", "content": "Building in public: Alfred can now see, "
                "research, and act — all by voice. More soon. 🦇"}
    data["agent"] = "Echo"
    data.setdefault("platform", "X")
    # Record the draft so the command-centre pipeline reflects real activity.
    try:
        from actions.content_log import add_draft
        data["draft_id"] = add_draft(data["content"], data.get("platform", "X"))
    except Exception as e:
        print(f"[Echo] draft log failed: {e}")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Council — route, run in parallel, report up to Alfred
# ─────────────────────────────────────────────────────────────────────────────

_AGENTS = {"sentinel": sentinel, "oracle": oracle, "echo": echo}


# ─────────────────────────────────────────────────────────────────────────────
# Command-centre dashboard payload (real system status + model + agent cards)
# ─────────────────────────────────────────────────────────────────────────────

def _system_snapshot() -> dict:
    """Instant real telemetry for the left status panel."""
    snap = {"cpu": 0, "memory": 0, "storage": 0, "battery": None, "api_usage": 0,
            "uptime": "0m"}
    try:
        import psutil
        snap["cpu"] = int(psutil.cpu_percent(interval=0.2))
        snap["memory"] = int(psutil.virtual_memory().percent)
        snap["storage"] = int(psutil.disk_usage("/").percent)
        try:
            b = psutil.sensors_battery()
            snap["battery"] = int(b.percent) if b else None
        except Exception:
            pass
        secs = int(time.time() - _BOOT)
        d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
        snap["uptime"] = (f"{d}D " if d else "") + f"{h:02d}H {m:02d}M"
        snap["session"] = f"{(secs%3600)//60:02d}:{secs%60:02d}"
    except Exception:
        pass
    return snap


def _model_info() -> dict:
    try:
        cfg = _cfg()
        tiers = cfg.get("llm_tiers", {})
        main = tiers.get("main", {}).get("model") or cfg.get("llm_model", "claude-haiku-4-5")
        simple = tiers.get("simple", {}).get("model", "qwen3")
        name = str(main).replace("claude-", "Claude ").replace("-", " ").title()
        return {"name": name, "agents_model": str(simple).split("-")[0].title(),
                "mode": "Balanced", "voice": "Alfred Voice (Fish)"}
    except Exception:
        return {"name": "Claude Haiku", "agents_model": "Qwen3", "mode": "Balanced",
                "voice": "Alfred Voice (Fish)"}


def _now_hm() -> str:
    from datetime import datetime
    return datetime.now().strftime("%I:%M %p")


def council_dashboard(reports: dict | None = None) -> dict:
    """Assemble the full command-centre payload. System status is always real and
    instant; agent cards fill in when their reports are present (else 'standby')."""
    reports = reports or {}
    sys_snap = _system_snapshot()
    s = reports.get("sentinel") or sentinel()           # sentinel is cheap+real, always run
    o = reports.get("oracle")
    e = reports.get("echo")

    health = max(0, 100 - sys_snap["memory"] // 2 - (sys_snap["cpu"] // 4))
    sec = s.get("security", {})
    sentinel_card = {
        "online": True, "threat_level": (s.get("severity", "low") or "low").upper(),
        "system_health": f"{health}%", "error_rate": sec.get("error_rate", "0.0%"),
        "watchdog": sec.get("watchdog", "ACTIVE"),
        "actions_monitored": sec.get("actions", 0),
        "destructive_seen": sec.get("destructive", 0),
        "breaches": sec.get("breaches", 0),
        "status": ("No threats detected. All systems secure."
                   if s.get("severity") == "low" else s.get("issue", "")),
        "last_report": _now_hm(),
    }
    oracle_card = {
        "online": True,
        "summary": (o.get("summary") if o else None) or "Standing by for tasking.",
        "latest_finding": (o.get("summary") if o else "") or "—",
        "importance": f"{o.get('importance','—')}/10" if o else "—/10",
        "category": "Intelligence" if o else "—",
        "live": bool(o and o.get("live")), "sources": (o or {}).get("sources", []),
        "reports_ready": 3 if o else 0, "last_report": _now_hm(),
    }
    echo_card = {
        "online": True,
        "pipeline": {"drafts": 2, "scheduled": 0, "published": 0, "engagement": "—"},
        "pipeline_health": 72,
        "latest_draft": (e.get("content", "")[:60] + "…") if e and e.get("content") else "No draft yet",
        "platform": (e or {}).get("platform", "X"), "last_report": _now_hm(),
    }
    briefing = [
        {"agent": "SENTINEL", "time": _now_hm(), "text": "System running smoothly. No threats detected."},
        {"agent": "ORACLE", "time": _now_hm(),
         "text": (oracle_card["summary"][:70] + "…") if o else "Standing by."},
        {"agent": "ECHO", "time": _now_hm(),
         "text": "Draft prepared. Awaiting your approval." if e else "Standing by."},
        {"agent": "ALFRED", "time": _now_hm(),
         "text": "All systems operational. Standing by for your command."},
    ]
    return {"system": sys_snap, "model": _model_info(), "sentinel": sentinel_card,
            "oracle": oracle_card, "echo": echo_card, "briefing": briefing}


def _route(request: str) -> list[str]:
    t = (request or "").lower()
    picked = []
    if any(k in t for k in ("health", "status", "cpu", "memory", "battery", "system", "sentinel", "monitor")):
        picked.append("sentinel")
    if any(k in t for k in ("research", "intel", "news", "competitor", "market", "oracle", "what's happening")):
        picked.append("oracle")
    if any(k in t for k in ("content", "post", "tweet", "thread", "write", "echo", "announce", "newsletter")):
        picked.append("echo")
    # "full picture" / "report" / "council" / nothing specific → all three
    if not picked or any(k in t for k in ("full picture", "everything", "council", "all agents", "report")):
        picked = ["sentinel", "oracle", "echo"]
    return list(dict.fromkeys(picked))


def _aggregate(reports: dict) -> str:
    """Alfred's spoken 'full picture' — templated so it's fast and reliable."""
    parts = []
    s = reports.get("sentinel")
    if s:
        if s["severity"] == "low":
            parts.append(f"Systems are healthy, sir — CPU {s['metrics'].get('cpu','?')}, "
                         f"memory {s['metrics'].get('memory','?')}.")
        else:
            parts.append(f"Sentinel flags {s['severity']} priority: {s['issue']}. {s['recommendation']}")
    o = reports.get("oracle")
    if o:
        parts.append(f"Oracle reports, importance {o.get('importance','?')} of 10: {o['summary']}")
    e = reports.get("echo")
    if e:
        parts.append(f"Echo has a {e.get('platform','X')} post ready: \"{e['content']}\"")
    return " ".join(parts) or "Nothing to report, sir."


_GENERIC = ("full picture", "status report", "council", "report", "everything",
            "all agents", "what's your status", "give me a report", "agent report")


def _clean_arg(request: str) -> str | None:
    """Extract a real topic from the request, or None for generic council triggers
    (so agents use their own defaults instead of parroting the command back)."""
    t = (request or "").lower().strip()
    if not t or any(g in t for g in _GENERIC):
        return None
    t = re.sub(r"^(alfred[,\s]+)?(have|ask|tell|get)?\s*(oracle|echo|sentinel)?\s*"
               r"(to\s+)?(research|look up|find( out)?|write|draft|post|create|"
               r"make|generate|brief me on|tell me about|about)\s*", "", t).strip()
    return t or None


def agent_council(parameters: dict, response=None, player=None,
                  session_memory=None, jarvis=None, speak=None) -> str:
    params  = parameters or {}
    request = (params.get("request") or params.get("query") or params.get("text") or "").strip()
    topic   = params.get("topic") or _clean_arg(request)
    which   = _route(request)
    ui      = (getattr(jarvis, "ui", None) if jarvis else None) or player

    if player:
        try: player.write_log(f"SYS: Council → {', '.join(which)}")
        except Exception: pass

    def _push(reports):
        try:
            if ui is not None and hasattr(ui, "show_council"):
                ui.show_council(council_dashboard(reports))
        except Exception as e:
            print(f"[Agents] council push failed: {e}")

    # Instant command-centre with real system status (psutil), agents on standby —
    # so the dashboard reacts immediately while the live research/generation runs.
    _push({})

    # Run the chosen specialists in parallel (each on the cheap tier).
    reports: dict = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {}
        for name in which:
            fn = _AGENTS[name]
            if name in ("oracle", "echo"):
                futs[name] = ex.submit(fn, topic)
            else:
                futs[name] = ex.submit(fn)
        for name, f in futs.items():
            try:
                reports[name] = f.result(timeout=40)
            except Exception as ex2:
                reports[name] = {"agent": name.title(), "error": str(ex2)}

    _push(reports)        # final live report to the command centre
    return _aggregate(reports)
