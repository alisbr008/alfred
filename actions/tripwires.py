"""
TRIPWIRES — standing alerts, armed by voice, that fire even when Alfred is CLOSED.

  "alert me if anyone mentions Velthar"          → keyword watch on the news wires
  "alert me if bitcoin goes above 70000"         → price threshold (Yahoo, no key)
  "alert me when an email from the university arrives" → inbox sender/subject watch

Two runners share this module:
  • Alfred's awareness loop (while he's running)
  • `tripwire_daemon.py` via launchd every 10 min — TRUE AUTONOMY: launchd wakes the
    check even when the app is closed. No busy-poll, no resident process, zero heat.

When a wire trips → Telegram ping + native macOS notification. 6-hour cooldown per
wire so one story can't spam you.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_TW = _base() / "workspace" / "tripwires.json"
_COOLDOWN = 6 * 3600


def _load() -> list[dict]:
    try:
        return json.loads(_TW.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(tws: list[dict]) -> None:
    _TW.parent.mkdir(parents=True, exist_ok=True)
    _TW.write_text(json.dumps(tws, indent=2, ensure_ascii=False), encoding="utf-8")


def _cfg() -> dict:
    try:
        return json.loads((_base() / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _notify(msg: str) -> None:
    """Telegram + native macOS notification — reaches you with the app closed."""
    c = _cfg()
    tok, chat = c.get("telegram_bot_token", ""), c.get("telegram_chat_id", "")
    if tok and chat:
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": f"🚨 TRIPWIRE\n{msg}"}, timeout=10)
        except Exception:
            pass
    try:
        safe = msg.replace('"', "'")[:180]
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe}" with title "🦇 ALFRED TRIPWIRE" sound name "Sosumi"'],
                       capture_output=True, timeout=8)
    except Exception:
        pass


# ── arming (voice) ───────────────────────────────────────────────────────────
def add_tripwire(text: str) -> str:
    """Parse a natural 'alert me…' request into a wire."""
    t = (text or "").strip().rstrip(".")
    tl = t.lower()

    m = re.search(r"\b(?:if|when)\s+(bitcoin|btc|ethereum|eth|tesla|tsla|nvidia|nvda|s&p|spx)\s+"
                  r"(?:goes?|drops?|falls?|rises?|is)?\s*(above|over|below|under)\s+\$?([\d,\.]+)k?", tl)
    if m:
        sym_map = {"bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "eth": "ETH-USD",
                   "tesla": "TSLA", "tsla": "TSLA", "nvidia": "NVDA", "nvda": "NVDA",
                   "s&p": "^GSPC", "spx": "^GSPC"}
        val = float(m.group(3).replace(",", ""))
        if "k" in tl[m.end(3):m.end(3)+1]:
            val *= 1000
        wire = {"kind": "price", "symbol": sym_map[m.group(1)],
                "dir": "above" if m.group(2) in ("above", "over") else "below",
                "value": val, "label": f"{m.group(1).upper()} {m.group(2)} {val:g}"}
    elif re.search(r"\bemail\b|\bmail\b|\binbox\b", tl):
        m2 = re.search(r"from\s+(?:the\s+)?([a-z0-9 .@_-]+?)(?:\s+arrives?|\s+comes?|$)", tl)
        who = (m2.group(1).strip() if m2 else "").strip()
        if not who:
            return "Alert on email from whom, sir?"
        wire = {"kind": "email", "who": who, "label": f"email from {who}"}
    else:
        m3 = re.search(r"(?:mentions?|about|containing|on the news about|news about)\s+(.+)$", tl)
        kw = (m3.group(1).strip() if m3 else
              re.sub(r"^.*?(?:alert me|notify me|tell me|watch for|tripwire)\s*(?:if|when|about)?\s*",
                     "", tl).strip())
        if len(kw) < 2:
            return "What should I watch for, sir?"
        wire = {"kind": "keyword", "word": kw, "label": f"news mentioning “{kw}”"}

    tws = _load()
    wire.update({"id": (max((w["id"] for w in tws), default=0) + 1),
                 "armed": time.time(), "last_fired": 0.0})
    tws.append(wire)
    _save(tws)
    return f"Tripwire armed, sir — I'll alert you on {wire['label']}, even if I'm not running."


def list_tripwires() -> str:
    tws = _load()
    if not tws:
        return "No tripwires armed, sir."
    return "Armed, sir: " + "; ".join(f"{w['id']}: {w['label']}" for w in tws) + "."


def remove_tripwire(which: str) -> str:
    tws = _load()
    wl = (which or "").lower().strip()
    keep = [w for w in tws
            if not (str(w["id"]) == wl or wl in w["label"].lower())]
    if len(keep) == len(tws):
        return f"No tripwire matching '{which}', sir."
    _save(keep)
    return "Disarmed, sir."


# ── checking (Alfred's loop AND the detached daemon) ─────────────────────────
def _check_price(w) -> str | None:
    try:
        from actions.missions import _mkt_one
        q = _mkt_one(w["symbol"], w["symbol"])
        if not q or q.get("price") is None:
            return None
        p = q["price"]
        if (w["dir"] == "above" and p >= w["value"]) or (w["dir"] == "below" and p <= w["value"]):
            return f"{w['symbol']} is {p:,.0f} — {w['dir']} your {w['value']:g} line."
    except Exception:
        pass
    return None


def _check_keyword(w, feed) -> str | None:
    kw = w["word"].lower()
    for f in feed:
        if kw in (f.get("text", "") + " " + f.get("cat", "")).lower():
            return f"{f['cat']}: {f['text'][:140]}"
    return None


def _check_email(w) -> str | None:
    try:
        import imaplib, email as em
        from actions.gmail_tool import _creds, _decode, _short_sender
        addr, pw = _creds()
        if not addr:
            return None
        M = imaplib.IMAP4_SSL("imap.gmail.com"); M.login(addr, pw)
        M.select("INBOX", readonly=True)
        _, data = M.search(None, "UNSEEN")
        for i in data[0].split()[-10:]:
            _, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            msg = em.message_from_bytes(md[0][1])
            frm = f"{_short_sender(msg.get('From',''))} {msg.get('From','')}".lower()
            subj = _decode(msg.get("Subject", ""))
            if w["who"].lower() in frm or w["who"].lower() in subj.lower():
                M.logout()
                return f"Email from {_short_sender(msg.get('From',''))}: {subj[:100]}"
        M.logout()
    except Exception:
        pass
    return None


def check_all(source: str = "daemon") -> list[str]:
    """Run every armed wire once. Fires notifications; returns what fired."""
    tws = _load()
    if not tws:
        return []
    feed = []
    if any(w["kind"] == "keyword" for w in tws):
        try:
            from actions.world_model import world_feed
            feed = (world_feed() or {}).get("feed", [])
        except Exception:
            pass
    fired = []
    now = time.time()
    for w in tws:
        if now - w.get("last_fired", 0) < _COOLDOWN:
            continue
        hit = (_check_price(w) if w["kind"] == "price" else
               _check_keyword(w, feed) if w["kind"] == "keyword" else
               _check_email(w))
        if hit:
            w["last_fired"] = now
            msg = f"{w['label']} — {hit}"
            link = _related_case(w)                       # cross-feature: connect to a case
            if link:
                msg += f" · relates to your case “{link}”"
            _notify(msg)
            fired.append(msg)
    if fired:
        _save(tws)
    return fired


def _related_case(wire: dict) -> str | None:
    """If a fired wire's subject matches an open case, name it — so an alert points
    Alfred (and you) straight at the relevant investigation."""
    try:
        from actions.casefiles import _load as _cload
        term = (wire.get("word") or wire.get("who") or wire.get("symbol") or "").lower().strip()
        if len(term) < 3:
            return None
        for c in _cload().get("cases", {}).values():
            hay = (c.get("name", "") + " " +
                   " ".join(e.get("note", "") for e in c.get("evidence", []))).lower()
            if term in hay:
                return c["name"]
    except Exception:
        pass
    return None
