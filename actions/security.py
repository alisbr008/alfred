"""
Sentinel's security backbone — gives the System Guardian real teeth:

  1. ACTION AUDIT TRAIL — every tool Alfred runs is logged with whether it was
     destructive and whether it was confirmed. Sentinel reads this to flag unusual
     patterns and (critically) any destructive action that ran WITHOUT a confirm.

  2. FILESYSTEM WATCHDOG — snapshots file counts of sensitive folders (Desktop,
     Documents, the vault). A sudden bulk drop (the desktop-wipe class) is flagged
     as a high-severity threat.

Both are dependency-free (JSON + os.scandir polling). Stored under memory/.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_MEM   = _base_dir() / "memory"
_AUDIT = _MEM / "audit_log.json"
_FSSNAP = _MEM / "fs_snapshot.json"


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(p: Path, data) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Security] write {p.name} failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Action audit trail
# ─────────────────────────────────────────────────────────────────────────────

def log_action(tool: str, action: str = "", destructive: bool = False,
               confirmed: bool = False, ok: bool = True, summary: str = "") -> None:
    """Record one tool execution. Called from the tool dispatcher."""
    items = _read(_AUDIT, [])
    items.append({"ts": time.time(), "tool": tool, "action": action,
                  "destructive": bool(destructive), "confirmed": bool(confirmed),
                  "ok": bool(ok), "summary": str(summary)[:120]})
    _write(_AUDIT, items[-500:])


def audit_stats(window_sec: int = 3600) -> dict:
    """Summarise recent tool activity for Sentinel."""
    items = _read(_AUDIT, [])
    since = time.time() - window_sec
    recent = [i for i in items if i.get("ts", 0) > since]
    destructive = [i for i in recent if i.get("destructive")]
    # the critical signal: a destructive action that ran WITHOUT confirmation
    breaches = [i for i in destructive if not i.get("confirmed")]
    failed = [i for i in recent if not i.get("ok", True)]
    total = max(1, len(recent))
    return {
        "actions": len(recent),
        "destructive": len(destructive),
        "breaches": len(breaches),
        "breach_tools": [i["tool"] for i in breaches][:5],
        "error_rate": f"{100*len(failed)/total:.1f}%",
        "last": recent[-1]["tool"] if recent else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Filesystem watchdog
# ─────────────────────────────────────────────────────────────────────────────

def _watched_dirs() -> dict:
    home = Path.home()
    dirs = {"Desktop": home / "Desktop", "Documents": home / "Documents"}
    try:
        cfg = _read(_base_dir() / "config" / "api_keys.json", {})
        v = cfg.get("obsidian_vault", "")
        if v:
            dirs["Vault"] = Path(v)
    except Exception:
        pass
    return dirs


def _count(path: Path) -> int:
    try:
        return sum(1 for _ in os.scandir(path))
    except Exception:
        return -1


def fs_check() -> dict:
    """Compare current folder counts to the last snapshot. Flags a bulk drop
    (>=8 files or >=40% of a folder removed since last check)."""
    prev = _read(_FSSNAP, {})
    cur, alerts = {}, []
    for name, path in _watched_dirs().items():
        n = _count(path)
        if n < 0:
            continue
        cur[name] = n
        p = prev.get(name)
        if isinstance(p, int) and p > 0:
            drop = p - n
            if drop >= 8 or (drop > 0 and drop / p >= 0.40):
                alerts.append(f"{drop} items removed from {name}")
    _write(_FSSNAP, cur)
    return {"alert": "; ".join(alerts) if alerts else None, "counts": cur}
