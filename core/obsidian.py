"""
Obsidian vault → Alfred profile.

The vault can be millions of characters — far too large for the LLM context.
So we distill it ONCE into a compact, first-person profile of the user and
cache it to disk.  Alfred loads that cached profile on every launch (no API
call, no rate-limit risk) and injects it into his system prompt, so he
actually knows who you are.

Rebuild the profile with:  python -m core.obsidian --rebuild
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR     = Path(__file__).resolve().parent.parent
CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
PROFILE_PATH = BASE_DIR / "memory" / "obsidian_profile.json"

DEFAULT_VAULT = Path.home() / "Desktop" / "Brain"
MAX_INPUT_CHARS = 90_000     # cap fed to the summariser (recent/key notes)


def _vault_path() -> Path:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        p = cfg.get("obsidian_vault", "").strip()
        if p:
            return Path(p).expanduser()
    except Exception:
        pass
    return DEFAULT_VAULT


def _collect_notes(vault: Path) -> str:
    """Gather note text, prioritising 'about/profile/index' notes then most
    recently modified ones, up to MAX_INPUT_CHARS."""
    files = [f for f in vault.rglob("*.md") if ".obsidian" not in f.parts and ".trash" not in f.parts]
    if not files:
        return ""

    def _priority(f: Path):
        name = f.stem.lower()
        keyed = any(k in name for k in ("about", "profile", "me", "index", "bio", "personal", "goal"))
        return (0 if keyed else 1, -f.stat().st_mtime)

    files.sort(key=_priority)

    chunks, total = [], 0
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not txt:
            continue
        block = f"## {f.stem}\n{txt}\n"
        if total + len(block) > MAX_INPUT_CHARS:
            block = block[: MAX_INPUT_CHARS - total]
        chunks.append(block)
        total += len(block)
        if total >= MAX_INPUT_CHARS:
            break
    return "\n".join(chunks)


_SUMMARY_PROMPT = (
    "Below are notes from the user's personal Obsidian knowledge vault. "
    "Extract a concise profile of the USER as a person, written so an AI butler "
    "named Alfred can use it to serve them well. Cover, only where the notes "
    "support it: name, location, work/role, current projects, goals, "
    "preferences, important people, routines, and recurring interests. "
    "Be factual — do NOT invent anything not in the notes. Output 8-15 short "
    "bullet points, each one fact, no preamble.\n\n=== NOTES ===\n{notes}"
)


def build_profile(force: bool = False, log=print) -> dict:
    """Summarise the vault into a cached profile. Returns the profile dict."""
    vault = _vault_path()
    if not vault.exists():
        log(f"[Obsidian] Vault not found at {vault}")
        return {"bullets": [], "source": str(vault), "note_count": 0}

    notes = _collect_notes(vault)
    note_count = len([f for f in vault.rglob("*.md") if ".obsidian" not in f.parts])
    if not notes.strip():
        return {"bullets": [], "source": str(vault), "note_count": note_count}

    log(f"[Obsidian] Distilling {note_count} notes into a profile…")
    from core.llm_client import call_llm_text
    raw = call_llm_text(_SUMMARY_PROMPT.format(notes=notes), timeout=120)

    bullets = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if line and len(line) > 2:
            bullets.append(line)
    bullets = bullets[:15]

    profile = {"bullets": bullets, "source": str(vault), "note_count": note_count}
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[Obsidian] Profile saved: {len(bullets)} facts about the user.")
    return profile


def load_profile() -> dict:
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"bullets": [], "source": "", "note_count": 0}


def profile_for_prompt() -> str:
    """Compact block injected into Alfred's system prompt."""
    p = load_profile()
    if not p.get("bullets"):
        return ""
    facts = "\n".join(f"- {b}" for b in p["bullets"])
    return f"[WHAT YOU KNOW ABOUT THE USER]\n{facts}"


if __name__ == "__main__":
    force = "--rebuild" in sys.argv
    build_profile(force=force)
