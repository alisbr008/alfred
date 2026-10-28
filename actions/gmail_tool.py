"""Gmail (read-only) for Alfred via IMAP + app password. No OAuth, no sending."""
import email
import imaplib
import json
from email.header import decode_header
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
IMAP_HOST   = "imap.gmail.com"


def _creds() -> tuple[str, str]:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("gmail_address", "").strip(), cfg.get("gmail_app_password", "").replace(" ", "").strip()
    except Exception:
        return "", ""


def _decode(s) -> str:
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        out.append(part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part)
    return "".join(out).strip()


def _short_sender(frm: str) -> str:
    frm = _decode(frm)
    if "<" in frm:
        return frm.split("<")[0].strip().strip('"') or frm
    return frm


def gmail_tool(parameters: dict, player=None) -> str:
    addr, pwd = _creds()
    if not addr or not pwd:
        return "Sir, Gmail isn't configured — I need a Gmail address and app password."

    action = (parameters.get("action") or "unread").strip().lower()
    try:
        count = int(parameters.get("count", 5))
    except Exception:
        count = 5
    count = max(1, min(count, 10))
    query = (parameters.get("query") or "").strip()

    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(addr, pwd)
        M.select("INBOX", readonly=True)

        if action == "search" and query:
            crit = ("SUBJECT", query)
        elif action == "recent":
            crit = ("ALL",)
        else:
            crit = ("UNSEEN",)

        typ, data = M.search(None, *crit)
        ids = data[0].split()
        total = len(ids)
        ids = ids[-count:][::-1]   # newest first

        if not ids:
            M.logout()
            return "No unread mail, sir." if action == "unread" else "No matching emails, sir."

        senders, lines = [], []
        for i in ids:
            typ, msg_data = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            msg = email.message_from_bytes(msg_data[0][1])
            senders.append(_short_sender(msg.get("From")))
            lines.append(f"• {senders[-1]}: {_decode(msg.get('Subject')) or '(no subject)'}")
        M.logout()

        if action == "unread":
            # Just who wrote — no subjects/context.
            return f"You have {total} unread, sir — from {', '.join(senders)}."
        return f"{len(ids)} emails, sir:\n" + "\n".join(lines)
    except imaplib.IMAP4.error as e:
        return f"Sir, Gmail login failed — check the app password. ({e})"
    except Exception as e:
        return f"Sir, I couldn't reach Gmail: {e}"
