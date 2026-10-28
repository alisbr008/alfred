"""Task & deadline tracker for Alfred. Stored in memory/tasks.json."""
import json
import uuid
from datetime import datetime, date
from pathlib import Path

STORE = Path(__file__).resolve().parent.parent / "memory" / "tasks.json"


def _load() -> list:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(tasks: list) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def _days_until(deadline: str):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(deadline.strip(), fmt).date()
            return (d - date.today()).days
        except Exception:
            continue
    return None


def _fmt(t: dict) -> str:
    du = _days_until(t.get("deadline", ""))
    when = ""
    if du is not None:
        when = " — today" if du == 0 else (f" — in {du}d" if du > 0 else f" — {abs(du)}d OVERDUE")
    elif t.get("deadline"):
        when = f" — {t['deadline']}"
    return f"• {t['title']}{when}"


def task_tracker(parameters: dict, player=None) -> str:
    action = (parameters.get("action") or "list").strip().lower()
    tasks = _load()

    if action == "add":
        title = (parameters.get("title") or "").strip()
        if not title:
            return "Sir, what's the task?"
        t = {"id": uuid.uuid4().hex[:8], "title": title,
             "deadline": (parameters.get("deadline") or "").strip(),
             "priority": (parameters.get("priority") or "medium").strip(),
             "status": "active", "created": datetime.now().isoformat(timespec="seconds")}
        tasks.append(t); _save(tasks)
        du = _days_until(t["deadline"])
        tail = f", due in {du} days" if du and du > 0 else ""
        return f"Tracking '{title}'{tail}, sir."

    if action in ("complete", "done"):
        key = (parameters.get("title") or parameters.get("id") or "").strip().lower()
        for t in tasks:
            if key and (key in t["title"].lower() or key == t["id"]):
                t["status"] = "done"; _save(tasks)
                return f"Marked '{t['title']}' done, sir."
        return "Sir, I couldn't find that task."

    active = [t for t in tasks if t.get("status") == "active"]
    if action == "overdue":
        od = [t for t in active if (_days_until(t.get("deadline", "")) or 99) < 0]
        return ("Overdue, sir:\n" + "\n".join(_fmt(t) for t in od)) if od else "Nothing overdue, sir."

    if not active:
        return "No active tasks, sir."
    active.sort(key=lambda t: (_days_until(t.get("deadline", "")) if _days_until(t.get("deadline", "")) is not None else 9999))
    return "Your tasks, sir:\n" + "\n".join(_fmt(t) for t in active)
