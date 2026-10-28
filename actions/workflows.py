"""
Alfred workflows — high-level routines that compose the existing tools.

Exposed to the LLM as a single `workflow` tool with a `name` parameter, so the
underlying tools (calendar, gmail, tasks, notes, vault, web) stay untouched.

Workflows:
  morning_briefing · mission_control · research · project_memory · university_mode
  meeting_assistant · content_factory · coding_companion · deep_work_guardian
  personal_crm · email_commander · revenue_monitor
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

BASE_DIR    = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
VAULT       = Path.home() / "Desktop" / "Brain"


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _vault() -> Path:
    p = _cfg().get("obsidian_vault", "").strip()
    return Path(p).expanduser() if p else VAULT


def _osa(script: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _llm(prompt: str, system: str = "", timeout: int = 60, tier: str = "main") -> str:
    """tier: 'main' (Haiku — fast/cheap) or 'heavy' (Sonnet — top reasoning)."""
    from core.llm_client import call_llm_text
    try:
        return call_llm_text(prompt, system=system or None, timeout=timeout, tier=tier).strip()
    except Exception as e:
        return f"(LLM unavailable: {e})"


RELIABLE_NEWS = ["reuters.com", "apnews.com", "bbc.com", "bloomberg.com", "cnbc.com",
                 "ft.com", "theguardian.com", "aljazeera.com", "npr.org", "wsj.com",
                 "espn.com", "nytimes.com"]


def _tavily(query: str, topic: str = "general", days: int = None,
            include_domains=None, max_results: int = 6) -> dict | None:
    """Real-time search via Tavily — returns a cited answer synthesised from quality sources."""
    key = _cfg().get("tavily_key", "").strip()
    if not key:
        return None
    import requests
    body = {"api_key": key, "query": query, "search_depth": "advanced",
            "topic": topic, "max_results": max_results, "include_answer": "advanced"}
    if days:
        body["days"] = days
    if include_domains:
        body["include_domains"] = include_domains
    try:
        r = requests.post("https://api.tavily.com/search", json=body, timeout=25)
        if r.status_code == 200:
            d = r.json()
            srcs = []
            for x in d.get("results", []):
                try:
                    srcs.append(x["url"].split("/")[2].replace("www.", ""))
                except Exception:
                    pass
            return {"answer": (d.get("answer") or "").strip(), "sources": srcs}
        print(f"[Tavily] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Tavily] {e}")
    return None


def _perplexity_api(query: str) -> str | None:
    """Real-time, cited answer from Perplexity's sonar model. Needs perplexity_key."""
    key = _cfg().get("perplexity_key", "").strip()
    if not key:
        return None
    import requests
    try:
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "sonar", "messages": [
                {"role": "system", "content": "Answer with the most current, precise facts. "
                 "Be concise (3-5 sentences). Include dates. If it just happened, say so."},
                {"role": "user", "content": query}]},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        print(f"[Perplexity] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Perplexity] {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. Morning Executive Briefing
# ─────────────────────────────────────────────────────────────────────────────
def _weather(city: str) -> str:
    import requests
    try:
        r = requests.get(f"https://wttr.in/{quote_plus(city)}?format=%C,+%t,+rain+%p", timeout=8)
        return r.text.strip() if r.status_code == 200 else ""
    except Exception:
        return ""


def _first_sentences(text: str, n: int = 2) -> str:
    import re
    return " ".join(re.split(r"(?<=[.!?])\s+", (text or "").strip())[:n])


def morning_briefing(query, player, jarvis) -> str:
    from concurrent.futures import ThreadPoolExecutor
    from actions.calendar_tool import calendar_tool
    from actions.gmail_tool import gmail_tool
    from actions.tasks_tool import task_tracker

    from datetime import datetime

    name  = _cfg().get("user_name", "Ali")
    city  = _cfg().get("city", "Kuala Lumpur")
    today = datetime.now().strftime("%A, %B %d, %Y")

    # Run the slow, independent lookups in parallel (was ~15s sequential → ~max one).
    # The news query is date-stamped and limited to the last 24h so the brief is
    # GUARANTEED fresh every single day — never yesterday's headlines.
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_cal  = ex.submit(calendar_tool, {"action": "today"})
        f_mail = ex.submit(gmail_tool, {"action": "unread", "count": 4})
        f_wx   = ex.submit(_weather, city)
        _nowt = datetime.now().strftime("%A %B %d at %I %p")
        f_news = ex.submit(_tavily,
                           f"breaking news and the newest developments in the last few hours "
                           f"as of {_nowt} — world events, markets, AI and politics that just "
                           f"happened, not older stories", "news", 1, RELIABLE_NEWS, 8)
        tasks = task_tracker({"action": "list"})

        def _safe(f, t):
            try:
                return f.result(timeout=t)
            except Exception:
                return None
        cal, mail, wx, news = _safe(f_cal, 7), _safe(f_mail, 8), _safe(f_wx, 6), _safe(f_news, 8)

    parts = [f"Good morning, {name}."]
    # Night watch debrief — what happened while he slept (if the watch ran).
    try:
        from actions.nightwatch import todays_digest
        _nd = todays_digest()
        if _nd:
            parts.append(_nd)
    except Exception:
        pass
    # Persistent relationship — remind Ali what he was building / where he stopped.
    try:
        from core.relationship import recap
        _cont = recap(spoken=False)
        if _cont and "haven't logged" not in _cont:
            parts.append(_cont)
    except Exception:
        pass
    if cal is not None:
        parts.append("Your calendar is clear." if "clear" in cal.lower()
                     else cal.replace("Today's schedule, sir:\n", "Schedule: "))
    if mail:
        parts.append(mail)
    if tasks and "No active" not in tasks:
        parts.append(tasks.replace("Your tasks, sir:\n", "Tasks: "))
    if wx:
        parts.append(f"Weather: {wx}.")
    if news and news.get("answer"):
        parts.append("Headlines: " + _first_sentences(news["answer"], 3))
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# 12. Mission Control — prepare workspace
# ─────────────────────────────────────────────────────────────────────────────
def mission_control(query, player, jarvis) -> str:
    # Open the work apps, and quit the distractions.
    open_apps  = _cfg().get("workspace_apps", ["Visual Studio Code", "Claude", "Terminal"])
    close_apps = _cfg().get("workspace_close", ["Google Chrome", "Obsidian"])
    opened, closed = [], []
    for app in open_apps:
        try:
            subprocess.Popen(["open", "-a", app]); opened.append(app)
        except Exception:
            pass
    for app in close_apps:
        try:
            subprocess.run(["osascript", "-e", f'tell application "{app}" to quit'],
                           capture_output=True, timeout=5); closed.append(app)
        except Exception:
            pass
    proj = _cfg().get("project_dir", "")
    if proj and Path(proj).expanduser().exists():
        subprocess.Popen(["open", str(Path(proj).expanduser())])
    if opened:
        return f"Workspace ready, sir — {', '.join(opened)} are up and the distractions are away."
    return "Sir, I couldn't set up the workspace."


# ─────────────────────────────────────────────────────────────────────────────
# 5. Research Agent
# ─────────────────────────────────────────────────────────────────────────────
def research(query, player, jarvis) -> str:
    if not query:
        return "Sir, what should I research?"
    if player:
        try: player.write_log(f"SYS: 🔎 Researching: {query[:50]}")
        except Exception: pass
    # Tavily is fast, real-time and cited — it is the whole path now. The old
    # Perplexity browser-open + scrape was the source of the 10-20s delay
    # (Perplexity is Cloudflare-blocked, so the scrape just hangs). DDG is the
    # only fallback.
    report, source = None, ""
    tav = _tavily(query, topic="news", days=14, max_results=6)
    if tav and tav.get("answer"):
        report = tav["answer"]
        _uniq = list(dict.fromkeys(tav["sources"]))[:3]
        source = "Tavily" + (f" ({', '.join(_uniq)})" if _uniq else "")
    if not report:
        source = "the web"
        findings = ""
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                try:
                    news = list(ddgs.news(query, max_results=6))
                except Exception:
                    news = []
                text = list(ddgs.text(query, max_results=6))
            parts = [f"- [{r.get('date','')}] {r.get('title','')}: {r.get('body','')} ({r.get('source','')})"
                     for r in news]
            parts += [f"- {r.get('title','')}: {r.get('body','')}" for r in text]
            findings = "\n".join(parts) if parts else ""
        except Exception as e:
            findings = ""
        if not findings.strip():
            # DDG gave nothing (it rate-limits hard) → free Google News RSS, no key.
            try:
                import requests as _rq, re as _re
                q = _rq.utils.quote(f"{query} when:7d")
                xml = _rq.get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
                              headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
                items = _re.findall(r"<item>(.*?)</item>", xml, _re.DOTALL)[:6]
                lines = []
                for it in items:
                    m = _re.search(r"<title>(.*?)</title>", it, _re.DOTALL)
                    d = _re.search(r"<pubDate>(.*?)</pubDate>", it)
                    if m:
                        t_ = _re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip()
                        lines.append(f"- [{(d.group(1)[:16] if d else '')}] {t_}")
                findings = "\n".join(lines)
                source = "the news wire"
            except Exception:
                findings = ""
        if not findings.strip():
            # Last resort: answer from knowledge, honestly labelled — NEVER a dead end.
            ans = _llm(f"Answer this precisely and concisely: {query}",
                       system="You are Alfred, a knowledgeable British butler. Answer directly "
                              "in 3-5 spoken sentences. No markdown.")
            if ans:
                return (ans + " That's from my own knowledge, sir — the live search wires "
                              "gave me nothing current on it.")
            return "The search wires gave me nothing on that, sir — ask me again shortly."
        report = _llm(
            f"User wants the LATEST on: {query}\n\nLive web results (most recent first):\n{findings}\n\n"
            "Summarize using ONLY these web results — your own training knowledge is outdated and must "
            "be ignored. Give 4-6 bullets of the most recent and important facts. If results don't "
            "cover it, say so.",
            system="You are a real-time research analyst. Use ONLY the provided web results, never prior knowledge.",
        )
    # Save to vault
    try:
        folder = _vault() / "Alfred"; folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"Research - {query[:50]}.md"
        f.write_text(f"# Research: {query}\n_{datetime.now():%Y-%m-%d %H:%M}_\n\n{report}\n", encoding="utf-8")
    except Exception:
        pass
    if player:
        try: player.write_log(f"SYS: 🔬 Research saved to vault: {query[:40]}")
        except Exception: pass
    return f"Here's the latest, sir — via {source}:\n{report}"


# ─────────────────────────────────────────────────────────────────────────────
# Show scheme — Alfred displays his own workflow map (n8n-style) in the UI
# ─────────────────────────────────────────────────────────────────────────────
SCHEME_NARRATION = (
    "Here's how I operate, sir. Your voice enters my core — a model router that picks Claude "
    "Haiku for speed and Sonnet for depth — which decides whether to answer or run a workflow. "
    "I have twelve, in three families. "
    "Productivity gives you a morning briefing of calendar, mail, tasks and weather, a university "
    "mode, a deep-work guardian that nudges you off distractions, and a deadline tracker. "
    "Knowledge runs a research agent over the live web and Perplexity, searches your Obsidian "
    "vault for past work, drafts content, and prepares meeting briefs. "
    "Operations handles your inbox, a personal CRM, revenue monitoring, mission control to open "
    "your whole workspace, and a coding companion. "
    "Every workflow draws on my shared tools — calendar, Gmail, Obsidian, web, notes, browser and "
    "code — and replies through this dashboard in voice."
)


def show_scheme(query, player, jarvis) -> str:
    try:
        if jarvis is not None and hasattr(jarvis.ui, "show_scheme"):
            jarvis.ui.show_scheme()
    except Exception:
        pass
    return SCHEME_NARRATION


# ─────────────────────────────────────────────────────────────────────────────
# 8. Project Memory — search the vault
# ─────────────────────────────────────────────────────────────────────────────
def project_memory(query, player, jarvis) -> str:
    q = (query or "").lower()
    if not q:
        return "Sir, what should I look up?"
    hits = []
    for f in _vault().rglob("*.md"):
        if ".obsidian" in f.parts:
            continue
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if q in txt.lower() or q in f.stem.lower():
            idx = txt.lower().find(q)
            snippet = txt[max(0, idx - 60): idx + 120].replace("\n", " ") if idx >= 0 else txt[:150]
            hits.append(f"- **{f.stem}**: …{snippet.strip()}…")
        if len(hits) >= 8:
            break
    if not hits:
        return f"Sir, I found nothing about '{query}' in your vault."
    summary = _llm(
        f"The user asked: '{query}'. Here are matching notes from their vault:\n" + "\n".join(hits) +
        "\n\nGive a 2-3 sentence summary of what they were working on.",
        system="You summarise the user's own notes back to them. Be concise.",
    )
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# 9. University Mode
# ─────────────────────────────────────────────────────────────────────────────
def university_mode(query, player, jarvis) -> str:
    from actions.tasks_tool import task_tracker
    from actions.calendar_tool import calendar_tool
    tasks = task_tracker({"action": "list"})
    cal   = calendar_tool({"action": "today"})
    out = ["University status, sir."]
    if tasks and "No active" not in tasks:
        out.append(tasks.replace("Your tasks, sir:\n", "Deadlines & tasks: "))
    else:
        out.append("No tracked assignments.")
    if cal and "clear" not in cal.lower():
        out.append(cal.replace("Today's schedule, sir:\n", "Today: "))
    return " ".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Meeting Assistant
# ─────────────────────────────────────────────────────────────────────────────
def meeting_assistant(query, player, jarvis) -> str:
    from actions.calendar_tool import calendar_tool
    from actions.gmail_tool import gmail_tool
    cal = calendar_tool({"action": "today"})
    topic = query or "the meeting"
    mail = gmail_tool({"action": "search", "query": query, "count": 4}) if query else ""
    brief = _llm(
        f"Prepare a meeting brief for: {topic}.\nToday's calendar:\n{cal}\nRelated emails:\n{mail}\n\n"
        "Output sections: Participants (if known), Context, Open items, 3 suggested talking points.",
        system="You are an executive assistant preparing a crisp meeting brief.",
        tier="heavy",
    )
    try:
        folder = _vault() / "Alfred"; folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Meeting Brief - {topic[:40]}.md").write_text(
            f"# Meeting Brief: {topic}\n_{datetime.now():%Y-%m-%d %H:%M}_\n\n{brief}\n", encoding="utf-8")
    except Exception:
        pass
    return f"Meeting brief ready, sir:\n{brief}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Content Factory
# ─────────────────────────────────────────────────────────────────────────────
def content_factory(query, player, jarvis) -> str:
    topic = query or "today's progress"
    posts = _llm(
        f"Create social content about: {topic}.\nProduce:\n"
        "1) An X thread (3-5 tweets).\n2) A LinkedIn post.\n3) A short Discord community update.\n"
        "Keep the founder's voice: direct, no fluff.",
        system="You are a sharp ghostwriter for a solo AI founder.",
        tier="heavy",
    )
    try:
        folder = _vault() / "Alfred"; folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Content - {datetime.now():%Y-%m-%d}.md").write_text(
            f"# Content: {topic}\n_{datetime.now():%Y-%m-%d %H:%M}_\n\n{posts}\n", encoding="utf-8")
    except Exception:
        pass
    return f"Content drafted and saved to your vault, sir:\n{posts[:400]}…"


# ─────────────────────────────────────────────────────────────────────────────
# 10. Coding Companion
# ─────────────────────────────────────────────────────────────────────────────
def coding_companion(query, player, jarvis) -> str:
    repo = _cfg().get("project_dir", str(Path(__file__).resolve().parent.parent))
    p = Path(repo).expanduser()
    if not p.exists():
        return f"Sir, I couldn't find the project at {repo}."
    files = [str(f.relative_to(p)) for f in p.rglob("*.py") if ".venv" not in f.parts][:60]
    readme = ""
    for n in ("README.md", "readme.md"):
        if (p / n).exists():
            readme = (p / n).read_text(encoding="utf-8", errors="ignore")[:2000]; break
    summary = _llm(
        f"Project: {p.name}. Task/question: {query or 'summarise the project and suggest next steps'}.\n"
        f"README:\n{readme}\n\nPython files:\n" + "\n".join(files) +
        "\n\nGive: what the project does, the files relevant to the task, and 3 concrete next steps.",
        system="You are a senior engineer pair-programming with the user.",
        tier="heavy",
    )
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# 2. Deep Work Guardian (background monitor)
# ─────────────────────────────────────────────────────────────────────────────
_GUARDIAN = {"on": False}
_DISTRACTING = ("youtube", "safari", "chrome", "tiktok", "instagram", "twitter", "x", "netflix", "discord")


def _frontmost() -> str:
    return _osa('tell application "System Events" to get name of first process whose frontmost is true').lower()


def deep_work_guardian(query, player, jarvis) -> str:
    action = (query or "on").lower()
    if "off" in action or "stop" in action:
        _GUARDIAN["on"] = False
        return "Deep Work Guardian off, sir."
    if _GUARDIAN["on"]:
        return "Guardian already watching, sir."
    _GUARDIAN["on"] = True

    def _loop():
        from actions.tasks_tool import task_tracker, _load, _days_until
        spent = 0
        while _GUARDIAN["on"]:
            time.sleep(60)
            app = _frontmost()
            if any(d in app for d in _DISTRACTING):
                spent += 1
            else:
                spent = 0
            if spent and spent % 30 == 0:   # every 30 min on a distraction
                soon = [t for t in _load() if t.get("status") == "active"
                        and (_days_until(t.get("deadline", "")) or 99) <= 3]
                if soon and jarvis:
                    t = soon[0]
                    try:
                        jarvis.speak(f"{_cfg().get('user_name','Ali')}, you've been on {app} for {spent} minutes. "
                                     f"Your '{t['title']}' deadline is close, sir.")
                    except Exception:
                        pass
    threading.Thread(target=_loop, daemon=True).start()
    return "Deep Work Guardian active, sir — I'll keep an eye on your focus."


# ─────────────────────────────────────────────────────────────────────────────
# 11. Personal CRM
# ─────────────────────────────────────────────────────────────────────────────
_CRM = BASE_DIR / "memory" / "crm.json"


def _crm_load() -> list:
    try:
        return json.loads(_CRM.read_text(encoding="utf-8"))
    except Exception:
        return []


def personal_crm(query, player, jarvis) -> str:
    import re
    people = _crm_load()
    q = (query or "list").strip()
    low = q.lower()

    if low.startswith("add ") or low.startswith("track "):
        name = q.split(" ", 1)[1].strip()
        people.append({"name": name, "last_contact": datetime.now().isoformat(timespec="seconds"), "notes": ""})
        _CRM.write_text(json.dumps(people, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"Tracking {name} in your CRM, sir."

    m = re.match(r"(?:talked to|contacted|replied to)\s+(.+)", low)
    if m:
        name = m.group(1).strip()
        for p in people:
            if name in p["name"].lower():
                p["last_contact"] = datetime.now().isoformat(timespec="seconds")
                _CRM.write_text(json.dumps(people, ensure_ascii=False, indent=2), encoding="utf-8")
                return f"Logged your contact with {p['name']}, sir."
        return f"Sir, {name} isn't in your CRM yet."

    if not people:
        return "Your CRM is empty, sir — say 'add [name]' to start."
    stale = []
    for p in people:
        try:
            days = (datetime.now() - datetime.fromisoformat(p["last_contact"])).days
        except Exception:
            days = 0
        if days >= 5:
            stale.append(f"• {p['name']} — {days}d since contact")
    if stale:
        return "People you've gone quiet on, sir:\n" + "\n".join(stale)
    return f"You're on top of your {len(people)} contacts, sir."


# ─────────────────────────────────────────────────────────────────────────────
# 4. AI Email Commander (read + classify + draft; sending is a separate OK step)
# ─────────────────────────────────────────────────────────────────────────────
def email_commander(query, player, jarvis) -> str:
    from actions.gmail_tool import gmail_tool
    inbox = gmail_tool({"action": "unread", "count": 8})
    if "unread" not in inbox.lower() and "•" not in inbox:
        return inbox  # error or no mail
    plan = _llm(
        f"Here is the user's unread inbox:\n{inbox}\n\n"
        "Triage it: (1) group into Urgent / Important / Can-wait / Ignore, "
        "(2) for the top 2 important ones, write a short suggested reply draft. "
        "End with: 'Say \"send it\" to approve a draft.'",
        system="You are a ruthless executive email triager. Be brief.",
    )
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# 6. Revenue Monitor (Velthar Lab) — needs Stripe/Whop/Gumroad keys
# ─────────────────────────────────────────────────────────────────────────────
def revenue_monitor(query, player, jarvis) -> str:
    import requests
    cfg = _cfg()
    lines, total = [], 0.0

    sk = cfg.get("stripe_key", "").strip()
    if sk:
        try:
            r = requests.get("https://api.stripe.com/v1/balance", auth=(sk, ""), timeout=12)
            if r.status_code == 200:
                avail = sum(b["amount"] for b in r.json().get("available", [])) / 100
                lines.append(f"Stripe available: ${avail:,.2f}")
        except Exception as e:
            lines.append(f"Stripe error: {e}")

    if cfg.get("gumroad_key", "").strip():
        try:
            r = requests.get("https://api.gumroad.com/v2/sales",
                             params={"access_token": cfg["gumroad_key"]}, timeout=12)
            if r.status_code == 200:
                sales = r.json().get("sales", [])
                day = sum(float(s.get("price", 0)) for s in sales
                          if s.get("created_at", "").startswith(datetime.now().strftime("%Y-%m-%d"))) / 100
                lines.append(f"Gumroad today: ${day:,.2f}"); total += day
        except Exception as e:
            lines.append(f"Gumroad error: {e}")

    if not cfg.get("stripe_key") and not cfg.get("gumroad_key") and not cfg.get("whop_key"):
        return ("Sir, revenue monitoring needs your keys — add stripe_key, gumroad_key, and/or "
                "whop_key to config/api_keys.json and I'll track Velthar Lab live.")
    return "Velthar Lab revenue, sir: " + (" · ".join(lines) if lines else "no data returned.")


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────────────────
_WORKFLOWS = {
    "morning_briefing": morning_briefing, "briefing": morning_briefing,
    "mission_control": mission_control, "workspace": mission_control,
    "research": research,
    "project_memory": project_memory, "memory": project_memory,
    "university_mode": university_mode, "university": university_mode,
    "meeting_assistant": meeting_assistant, "meeting": meeting_assistant,
    "content_factory": content_factory, "content": content_factory,
    "coding_companion": coding_companion, "coding": coding_companion,
    "deep_work_guardian": deep_work_guardian, "deep_work": deep_work_guardian, "focus": deep_work_guardian,
    "personal_crm": personal_crm, "crm": personal_crm,
    "email_commander": email_commander, "email": email_commander, "inbox": email_commander,
    "revenue_monitor": revenue_monitor, "revenue": revenue_monitor,
    "show_scheme": show_scheme, "show_workflows": show_scheme, "how_you_work": show_scheme,
    "capabilities": show_scheme, "operate": show_scheme,
}


def run_workflow(parameters: dict, player=None, jarvis=None) -> str:
    name = (parameters.get("name") or "").strip().lower().replace(" ", "_")
    query = (parameters.get("query") or "").strip()
    fn = _WORKFLOWS.get(name)
    if not fn:
        return f"Sir, I don't have a workflow called '{name}'."
    try:
        return fn(query, player, jarvis)
    except Exception as e:
        return f"Sir, the {name} workflow hit an error: {e}"
