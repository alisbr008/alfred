"""save_to_vault — Alfred captures info into the user's Obsidian vault in the background."""
import json
from datetime import datetime
from pathlib import Path

CONFIG_PATH   = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
DEFAULT_VAULT = Path.home() / "Desktop" / "Brain"


def _vault() -> Path:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        p = cfg.get("obsidian_vault", "").strip()
        if p:
            return Path(p).expanduser()
    except Exception:
        pass
    return DEFAULT_VAULT


def save_to_vault(parameters: dict, player=None) -> str:
    content = (parameters.get("content") or "").strip()
    title   = (parameters.get("title") or "").strip()
    if not content:
        return "Sir, there's nothing to note down."

    vault = _vault()
    if not vault.exists():
        return f"Sir, I couldn't find your vault at {vault}."

    folder = vault / "Alfred"
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now()

    try:
        if title:
            # Dedicated note for a titled capture.
            safe = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60] or "Note"
            f = folder / f"{safe}.md"
            header = "" if f.exists() else f"# {title}\n"
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"{header}\n- {now:%Y-%m-%d %H:%M} — {content}\n")
            target = f.name
        else:
            # Append to today's daily capture note.
            f = folder / f"{now:%Y-%m-%d}.md"
            header = "" if f.exists() else f"# {now:%A, %B %d, %Y}\n\nCaptured by Alfred:\n"
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"{header}- {now:%H:%M} — {content}\n")
            target = f.name
        if player:
            try:
                player.write_log(f"SYS: 🧠 Saved to vault → Alfred/{target}")
            except Exception:
                pass
        return "Noted in your vault, sir."
    except Exception as e:
        return f"Sir, I couldn't write to the vault: {e}"
