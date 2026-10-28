"""
CONTACT DOSSIERS — "pull up the file on Marcus."

Alfred assembles an intelligence file on a person from everything he already has:
semantic memory (episodes · vault · night-logs), the Gmail inbox (recent headers
from/about them), and any prior dossier. Haiku synthesises the card; it's spoken,
logged as a classified-file block, and cached in workspace/dossiers.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_DOSSIERS = _base() / "workspace" / "dossiers.json"


def _load() -> dict:
    try:
        return json.loads(_DOSSIERS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _DOSSIERS.parent.mkdir(parents=True, exist_ok=True)
    _DOSSIERS.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def _mail_trace(name: str, limit: int = 8) -> list[str]:
    """Recent inbox headers from/about this person (read-only IMAP)."""
    out = []
    try:
        import imaplib, email as em
        from actions.gmail_tool import _creds, _decode, _short_sender
        addr, pw = _creds()
        if not addr:
            return []
        M = imaplib.IMAP4_SSL("imap.gmail.com"); M.login(addr, pw)
        M.select("INBOX", readonly=True)
        _, data = M.search(None, f'(OR FROM "{name}" SUBJECT "{name}")')
        for i in (data[0].split() or [])[-limit:]:
            _, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            msg = em.message_from_bytes(md[0][1])
            out.append(f"{msg.get('Date','')[:16]} · {_short_sender(msg.get('From',''))}: "
                       f"{_decode(msg.get('Subject',''))[:70]}")
        M.logout()
    except Exception:
        pass
    return out


def _wiki_title(name: str) -> str:
    """Resolve a (possibly misheard/misspelled) name to a real Wikipedia page title.
    Uses Wikipedia's own spell-correction ('did you mean') so 'anrew garfield' →
    'Andrew Garfield', 'tayler swift' → 'Taylor Swift'. Plain full-text search alone
    returns garbage on typos, so we always prefer the suggestion when there is one."""
    try:
        import requests
        api = "https://en.wikipedia.org/w/api.php"
        ua = {"User-Agent": "Alfred/2.1"}
        def _search(q):
            r = requests.get(api, params={"action": "query", "list": "search", "srsearch": q,
                                          "srlimit": 1, "srinfo": "suggestion", "format": "json"},
                             headers=ua, timeout=8)
            data = (r.json().get("query", {}) or {})
            return data.get("searchinfo", {}).get("suggestion"), data.get("search", [])
        sugg, hits = _search(name)
        if sugg:                                             # spell-corrected → re-resolve
            _, hits2 = _search(sugg)
            if hits2:
                return hits2[0]["title"]
            return sugg
        # No spell suggestion → accept the top hit ONLY if it shares a name-token
        # with the query (guards against irrelevant full-text matches).
        if hits:
            title = hits[0]["title"]
            q_tokens = {w for w in name.lower().split() if len(w) > 3}
            t_tokens = {w for w in title.lower().split() if len(w) > 3}
            if q_tokens & t_tokens:                          # a real surname/token in common
                return title
        return ""
    except Exception:
        return ""


def _wiki(name: str) -> dict:
    """Public-figure intel: bio + photo from Wikipedia (free, no key). Search-first,
    so small transcription errors in the name still resolve to the right person."""
    try:
        import requests
        def _summary(title: str):
            return requests.get("https://en.wikipedia.org/api/rest_v1/page/summary/"
                                + requests.utils.quote(title.replace(" ", "_")),
                                headers={"User-Agent": "Alfred/2.1"}, timeout=10)
        r = _summary(name)                                   # try the literal name first
        d = r.json() if r.status_code == 200 else {}
        if r.status_code != 200 or d.get("type") == "disambiguation" or not d.get("extract"):
            title = _wiki_title(name)                         # fall back to fuzzy search
            if not title:
                return {}
            r = _summary(title)
            if r.status_code != 200:
                return {}
            d = r.json()
            if d.get("type") == "disambiguation":
                return {}
        return {"title": d.get("title", name),
                "desc": d.get("description", ""), "extract": (d.get("extract") or "")[:600],
                "photo": (d.get("thumbnail") or {}).get("source", ""),
                "hero": ((d.get("originalimage") or {}).get("source", "")
                         or (d.get("thumbnail") or {}).get("source", ""))}
    except Exception:
        return {}


def _news(name: str, n: int = 4) -> list[str]:
    """Freshest headlines about them (Google News RSS, free)."""
    try:
        import requests, re
        q = requests.utils.quote(f'"{name}" when:14d')
        xml = requests.get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
        out = []
        for m in re.findall(r"<item>.*?<title>(.*?)</title>", xml, re.DOTALL)[:n]:
            t = re.sub(r"<!\[CDATA\[|\]\]>", "", m).strip()
            t = re.sub(r"\s+-\s+[^-]+$", "", t)
            out.append(t[:120])
        return out
    except Exception:
        return []


def pull_dossier(name: str, jarvis=None, player=None) -> str:
    name = (name or "").strip().rstrip("?.!").title()
    if len(name) < 2:
        return "A file on whom, sir?"
    ui = (getattr(jarvis, "ui", None) if jarvis else None) or player
    if ui:
        try: ui.write_log(f"SYS: 🗂 Pulling the file on {name}…")
        except Exception: pass

    # gather intelligence — local (memory/inbox) AND public (wiki/news) in parallel
    from concurrent.futures import ThreadPoolExecutor
    mem_hits = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_wiki, f_news, f_mail = ex.submit(_wiki, name), ex.submit(_news, name), ex.submit(_mail_trace, name)
        try:
            from core.semantic_memory import search
            mem_hits = [f"({h['kind']}) {h['text'][:110]}" for h in search(name, k=6, min_sim=0.30)]
        except Exception:
            pass
        wiki, news, mails = f_wiki.result(), f_news.result(), f_mail.result()
    prior = _load().get(name.lower(), {}).get("summary", "")

    if not any((mem_hits, mails, prior, wiki)):
        return f"The file on {name} is empty, sir — no trace in the memory, the vault, the inbox or the public record."

    raw = ""
    if wiki.get("extract"): raw += f"Public record: {wiki['extract']}\n"
    if news:     raw += "Recent news:\n" + "\n".join(f"- {h}" for h in news) + "\n"
    if prior:    raw += f"Prior dossier: {prior}\n"
    if mem_hits: raw += "Memory traces:\n" + "\n".join(f"- {m}" for m in mem_hits) + "\n"
    if mails:    raw += "Inbox trace:\n" + "\n".join(f"- {m}" for m in mails) + "\n"

    card = {}
    try:
        from core.llm_client import call_llm_text
        import re
        out = call_llm_text(
            raw + f"\nCompile the intelligence dossier on {name}.",
            system=("You are Alfred compiling a concise intelligence dossier. STRICT JSON: "
                    '{"who":"one line — who they are","role":"title/affiliation or their tie to the user",'
                    '"status":"current situation / last contact","facts":["notable facts, 3-5"],'
                    '"threads":["open items or things to watch, 0-3"]} '
                    "Use the traces; never invent specifics."),
            tier="heavy", timeout=40, max_tokens=460)
        m = re.search(r"\{.*\}", out or "", re.DOTALL)
        card = json.loads(m.group(0)) if m else {}
    except Exception:
        card = {}

    who     = card.get("who") or wiki.get("desc") or f"{name} — traces on file."
    role    = card.get("role") or wiki.get("desc") or ""
    status  = card.get("status") or ""
    facts   = [str(x) for x in (card.get("facts") or [])][:5]
    threads = [str(x) for x in (card.get("threads") or [])][:3]

    # persist
    d = _load()
    d[name.lower()] = {"name": name, "summary": who, "role": role, "status": status,
                       "facts": facts, "threads": threads,
                       "photo": wiki.get("photo", ""), "news": news, "ts": time.time()}
    _save(d)

    # render in the Mission-Control CASE-FILE layout, person as central subject
    from datetime import datetime
    cpu = ram = None
    try:
        import psutil
        cpu = round(psutil.cpu_percent(interval=0.1)); ram = round(psutil.virtual_memory().percent)
    except Exception:
        pass
    payload = {"type": "casefile", "title": name.upper(), "data": {
        "layout": "dossier",                          # no side panels, portrait not map
        "hero_photo": wiki.get("hero") or wiki.get("photo", ""),
        "name": name, "case_id": f"DSR-{''.join(w[0] for w in name.split()[:2]).upper()}",
        "priority": "Profile", "status": "DOSSIER",
        "updated": f"{datetime.now():%d %b %Y, %H:%M}".upper(),
        "progress": min(95, 30 + len(facts) * 12 + len(news) * 6),
        "summary": (who + ((" " + status) if status else "")),
        "tags": ([role] if role else []) + (["Public Figure"] if wiki.get("desc") else []),
        "subjects": [{"name": name, "alias": role, "last_seen": "on file",
                      "risk": "SUBJECT", "photo": wiki.get("photo", "")}],
        "timeline": [{"time": "recent", "title": h[:40], "detail": h} for h in news[:5]],
        "evidence": [{"name": m[:26], "kind": "doc"} for m in mails[:5]],
        "notes": facts,
        "ai": {"analysis": status or who,
               "probability": None,
               "actions": [{"text": th, "done": False} for th in threads]},
        "map_label": "KNOWN ASSOCIATIONS",
        "cpu": cpu, "ram": ram, "mem": ram,
        "user": (jarvis and getattr(jarvis, "_user_name", None)) or "OPERATOR"}}
    if ui and hasattr(ui, "show_visualization"):
        try: ui.show_visualization(payload)
        except Exception: pass
    elif ui:
        try:
            block = [f"╔═ DOSSIER · {name.upper()} ═╗", f"  {who}"]
            for f in facts: block.append(f"  ▸ {f}")
            ui.write_log("\n".join(block))
        except Exception:
            pass

    spoken = f"The file on {name}, sir. {who}"
    if status:  spoken += f" {status}"
    if threads: spoken += f" One to watch: {threads[0]}."
    return spoken
