"""
NIGHT WATCH — Alfred's autonomous overnight mode. The batcave runs while you sleep.

Watchers (one cycle ~ every 25 min, cheap tiers only):
  📧 Gmail    — unread scan → triage (urgent / needs-reply / noise) → reply DRAFTS
  🐦 X        — mentions, 2 checks per night MAX (free API tier ≈ 100 reads/month)
  🌍 World    — markets/AI/world refresh hourly (feeds the globe)
  🛡  Sentinel — filesystem watchdog + system health every cycle

Everything lands in a night log (workspace/nightlog/YYYY-MM-DD.jsonl). At exit,
Sonnet reads the log ONCE and writes the morning digest → Telegram + morning
briefing. Urgent pings (security / money / VIP senders) go to Telegram instantly.

HARD SAFETY RAIL: this module is OBSERVE + DRAFT ONLY. It never imports the tool
executor, never sends, deletes, posts or executes anything. Drafts queue for the
user's explicit morning approval.
"""
from __future__ import annotations

import email
import imaplib
import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

_BASE   = Path(__file__).resolve().parent.parent
_NL_DIR = _BASE / "workspace" / "nightlog"

_URGENT_KEYWORDS = ("payment", "invoice", "bank", "stripe", "paypal", "fraud", "suspicious",
                    "security alert", "sign-in", "password", "verify your", "declined",
                    "chargeback", "refund", "overdue", "wire", "urgent")

# module state
_state = {"active": False, "stop": None, "thread": None, "caffeinate": None,
          "seen_mail": set(), "x_reads": 0, "cycles": 0, "started": 0.0}


# ── plumbing ─────────────────────────────────────────────────────────────────
def _cfg() -> dict:
    try:
        return json.loads((_BASE / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _log_path() -> Path:
    _NL_DIR.mkdir(parents=True, exist_ok=True)
    return _NL_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"


def _nightlog(kind: str, text: str, extra: dict | None = None) -> None:
    rec = {"t": f"{datetime.now():%H:%M}", "kind": kind, "text": text}
    if extra:
        rec.update(extra)
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _ui(jarvis, line: str) -> None:
    try:
        jarvis.ui.write_log(f"NIGHT: {line}")
    except Exception:
        pass


def _ping(text: str) -> None:
    """Urgent Telegram ping — high bar only (security / money / VIP)."""
    c = _cfg()
    tok, chat = c.get("telegram_bot_token", ""), c.get("telegram_chat_id", "")
    if not tok or not chat:
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": f"🌙 ALFRED NIGHT WATCH\n{text}"}, timeout=10)
    except Exception:
        pass


def _llm(prompt: str, system: str, tier: str = "main", max_tokens: int = 500) -> str:
    try:
        from core.llm_client import call_llm_text
        return (call_llm_text(prompt, system=system, tier=tier,
                              timeout=60, max_tokens=max_tokens) or "").strip()
    except Exception:
        return ""


# ── watchers (READ-ONLY) ─────────────────────────────────────────────────────
def _gmail_scan() -> list[dict]:
    """Unread mail: sender, address, subject, first ~600 chars of body. Read-only."""
    from actions.gmail_tool import _creds, _decode, _short_sender
    addr, pwd = _creds()
    if not addr or not pwd:
        return []
    out = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(addr, pwd)
        M.select("INBOX", readonly=True)
        _, data = M.search(None, "UNSEEN")
        for i in data[0].split()[-15:]:
            key = i.decode() if isinstance(i, bytes) else str(i)
            if key in _state["seen_mail"]:
                continue
            _, md = M.fetch(i, "(BODY.PEEK[])")
            msg = email.message_from_bytes(md[0][1])
            body = ""
            for part in (msg.walk() if msg.is_multipart() else [msg]):
                if part.get_content_type() == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode(errors="ignore")[:600]
                    except Exception:
                        pass
                    break
            frm = msg.get("From", "")
            out.append({"id": key, "sender": _short_sender(frm),
                        "email": (frm.split("<")[-1].rstrip(">") if "<" in frm else frm).lower(),
                        "subject": _decode(msg.get("Subject")) or "(no subject)", "body": body})
            _state["seen_mail"].add(key)
        M.logout()
    except Exception as e:
        _nightlog("error", f"gmail scan failed: {e}")
    return out


def _mail_urgent(m: dict, vips: list) -> bool:
    blob = f"{m['sender']} {m['email']} {m['subject']} {m['body'][:200]}".lower()
    if any(v and v.lower() in blob for v in vips):
        return True
    return any(k in blob for k in _URGENT_KEYWORDS)


def _triage_and_draft(mails: list[dict], jarvis) -> None:
    vips = _cfg().get("vip_senders", []) or []
    for m in mails:
        urgent = _mail_urgent(m, vips)
        _nightlog("mail", f"{m['sender']}: {m['subject']}", {"urgent": urgent})
        _ui(jarvis, f"📧 {m['sender']} — {m['subject'][:60]}" + (" ⚠️ URGENT" if urgent else ""))
        if urgent:
            _ping(f"⚠️ Email from {m['sender']}: {m['subject']}")
        # needs-reply triage on the cheap tier; DRAFT only, never send
        verdict = _llm(f"From: {m['sender']}\nSubject: {m['subject']}\n{m['body'][:400]}\n\n"
                       "Does this personal/business email need a reply from the recipient? "
                       "Answer only YES or NO.", "You triage email.", tier="simple", max_tokens=5)
        if verdict.upper().startswith("YES"):
            draft = _llm(f"Draft a short, courteous reply (3-5 sentences) the user could send.\n"
                         f"From: {m['sender']}\nSubject: {m['subject']}\n{m['body'][:500]}",
                         "You draft email replies for the user's morning review. Output only the reply.",
                         tier="simple", max_tokens=300)   # Qwen — free tier, runs all night
            if draft:
                _nightlog("draft", f"Reply to {m['sender']} — {m['subject']}", {"draft": draft})
                _ui(jarvis, f"✍️ drafted a reply to {m['sender']} (for your morning approval)")


def _x_mentions(jarvis) -> None:
    """2 reads/night MAX — the free X tier is ~100 reads/month."""
    if _state["x_reads"] >= 2:
        return
    try:
        from actions.twitter_tool import _auth
        import requests
        auth = _auth()
        if not auth:
            return
        c = _cfg()
        uid = c.get("x_user_id", "")
        if not uid:
            r = requests.get("https://api.twitter.com/2/users/me", auth=auth, timeout=15)
            uid = (r.json().get("data") or {}).get("id", "")
            if uid:   # cache so we never burn this read again
                c["x_user_id"] = uid
                (_BASE / "config" / "api_keys.json").write_text(json.dumps(c, indent=2), encoding="utf-8")
        if not uid:
            return
        _state["x_reads"] += 1
        r = requests.get(f"https://api.twitter.com/2/users/{uid}/mentions?max_results=5",
                         auth=auth, timeout=15)
        if r.status_code == 429:
            _nightlog("x", "mentions rate-limited (free tier)")
            return
        for tw in (r.json().get("data") or []):
            _nightlog("x", f"mention: {tw.get('text','')[:150]}")
            _ui(jarvis, f"🐦 mention: {tw.get('text','')[:70]}")
    except Exception as e:
        _nightlog("error", f"x check failed: {e}")


def _sentinel(jarvis) -> None:
    try:
        from actions.security import fs_check
        alert = (fs_check() or {}).get("alert")
        if alert:
            _nightlog("security", alert)
            _ui(jarvis, f"🛡 SENTINEL: {alert}")
            _ping(f"🛡 Sentinel flag: {alert}")       # security always pings
    except Exception:
        pass
    try:
        import psutil
        if psutil.virtual_memory().percent > 92 or psutil.disk_usage("/").percent > 95:
            _nightlog("system", "resource pressure high")
    except Exception:
        pass


# ── the loop ─────────────────────────────────────────────────────────────────
def _loop(jarvis, stop: threading.Event) -> None:
    cycle_s = 25 * 60
    while not stop.is_set():
        _state["cycles"] += 1
        n = _state["cycles"]
        _ui(jarvis, f"cycle {n} — sweeping…")
        mails = _gmail_scan()
        if mails:
            _triage_and_draft(mails, jarvis)
        else:
            _nightlog("mail", "no new unread")
        _sentinel(jarvis)
        if n % 2 == 0:                                # ~hourly
            try:
                jarvis._refresh_world()
                _nightlog("world", "globe refreshed")
            except Exception:
                pass
        hour = datetime.now().hour
        if hour in (2, 5):                            # 2 budgeted X reads a night
            _x_mentions(jarvis)
        _nightlog("cycle", f"cycle {n} complete")
        stop.wait(cycle_s)


# ── digest ───────────────────────────────────────────────────────────────────
def make_digest(spoken: bool = False) -> str:
    try:
        lines = _log_path().read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        lines = []
    if not lines:
        return "A quiet night, sir — nothing needed your attention."
    events = "\n".join(lines[-120:])
    digest = _llm(
        f"Night log (JSONL):\n{events}\n\n"
        "Write the butler's morning debrief for the user: what happened overnight, what's "
        "urgent, which reply drafts are waiting for approval, notable mentions/news. Warm, "
        "concise, spoken prose (5-8 sentences), address him as 'sir'. No markdown.",
        "You are Alfred, a composed British butler summarising the night watch.",
        tier="main", max_tokens=600) or "The night log is saved, sir — nothing urgent stood out."
        # Haiku reads the whole log — ~12x cheaper than Sonnet, once per morning.
    try:
        (_NL_DIR / f"digest-{datetime.now():%Y-%m-%d}.txt").write_text(digest, encoding="utf-8")
    except Exception:
        pass
    return digest


def todays_digest() -> str:
    try:
        p = _NL_DIR / f"digest-{datetime.now():%Y-%m-%d}.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""


# ── public control ───────────────────────────────────────────────────────────
def is_active() -> bool:
    return bool(_state["active"])


def start_night(jarvis) -> str:
    if _state["active"]:
        return "The night watch is already running, sir."
    _state.update({"active": True, "cycles": 0, "x_reads": 0, "started": time.time()})
    _state["seen_mail"] = set()
    try:   # keep the Mac awake, display allowed to sleep
        _state["caffeinate"] = subprocess.Popen(["caffeinate", "-is"])
    except Exception:
        _state["caffeinate"] = None
    stop = threading.Event()
    _state["stop"] = stop
    t = threading.Thread(target=_loop, args=(jarvis, stop), daemon=True)
    _state["thread"] = t
    t.start()
    jarvis._night_mode = True
    try:
        jarvis.ui.set_state("NIGHT WATCH")
        if hasattr(jarvis.ui, "set_night"):
            jarvis.ui.set_night(True)
    except Exception:
        pass
    _nightlog("mode", "night watch engaged")
    _ping("Night watch engaged. I have the house, sir. 🦇")
    return ("Night watch engaged, sir. I'll mind the inbox, the wires and the perimeter — "
            "wake me by name if you need me. Sleep well.")


def end_night(jarvis, reason: str = "morning", with_digest: bool = True) -> str:
    if not _state["active"]:
        return ""
    _state["active"] = False
    try:
        if _state["stop"]:
            _state["stop"].set()
    except Exception:
        pass
    try:
        if _state["caffeinate"]:
            _state["caffeinate"].terminate()
    except Exception:
        pass
    jarvis._night_mode = False
    try:
        jarvis.ui.set_state("LISTENING")
        if hasattr(jarvis.ui, "set_night"):
            jarvis.ui.set_night(False)
    except Exception:
        pass
    _nightlog("mode", f"night watch ended ({reason})")
    if not with_digest:
        return "Welcome back, sir."
    digest = make_digest()
    _ping(digest)                                    # debrief lands on your phone too
    return digest
