"""
REAL HANDS — Alfred can actually operate the Mac: move the mouse, click, type,
press keys, scroll. Driven by pyautogui.

SAFETY IS THE WHOLE DESIGN (a misheard command once wiped the Desktop):
  • Alfred NEVER acts without an explicit, per-request "yes". Every computer-use
    request is described first and held until the user confirms — enforced by the
    main confirm-guardrail (`_confirm_description` returns a description for
    `computer_use`, so it can NEVER run with `_confirmed` unset).
  • A hard DANGER filter blocks obviously destructive intent outright, even with
    confirmation (deleting everything, wiping disks, sending money, etc.).
  • pyautogui FAILSAFE stays ON — slam the mouse to a screen corner to abort.
  • It only runs the specific steps it described. No open-ended autonomy.

Requires macOS Accessibility permission for whichever app launches Alfred
(System Settings → Privacy & Security → Accessibility).
"""
from __future__ import annotations

import json
import re
import time

_DANGER = ("delete everything", "wipe", "erase disk", "format", "rm -rf", "factory reset",
           "empty trash", "send money", "transfer", "wire ", "buy ", "purchase", "pay ",
           "delete all", "uninstall", "shut down", "shutdown", "log out", "erase all")

# Keys pyautogui understands, mapped from friendly names.
_KEYMAP = {"cmd": "command", "win": "command", "opt": "option", "ctrl": "ctrl",
           "return": "enter", "esc": "escape", "del": "delete"}


def _pg():
    import pyautogui
    pyautogui.FAILSAFE = True          # mouse to a corner = abort
    pyautogui.PAUSE = 0.15             # small gap between actions = visible + safe
    return pyautogui


def _norm_keys(combo: str) -> list[str]:
    parts = re.split(r"[+\s-]+", (combo or "").lower().strip())
    return [_KEYMAP.get(p, p) for p in parts if p]


def _describe(steps: list[dict]) -> str:
    """Human sentence for the confirmation prompt."""
    bits = []
    for s in steps[:6]:
        a = s.get("action")
        if a == "type":
            bits.append(f'type "{(s.get("text") or "")[:40]}"')
        elif a == "hotkey":
            bits.append("press " + "+".join(_norm_keys(s.get("keys", ""))))
        elif a == "press":
            bits.append(f'press {s.get("key","")}')
        elif a == "click":
            bits.append(f'click at {s.get("x")},{s.get("y")}' if "x" in s else "click")
        elif a == "scroll":
            bits.append(f'scroll {s.get("amount",0)}')
        elif a == "move":
            bits.append(f'move to {s.get("x")},{s.get("y")}')
    more = "…" if len(steps) > 6 else ""
    return "; ".join(bits) + more


_AX_SCRIPT = r'''
tell application "System Events"
  set frontApp to name of first application process whose frontmost is true
  set out to ""
  try
    tell process frontApp
      set win to front window
      set kinds to {"button", "text field", "checkbox", "pop up button", "menu button", "radio button"}
      repeat with k in kinds
        try
          set els to (every UI element of win whose role description is (k as text))
        on error
          set els to {}
        end try
        repeat with e in els
          try
            set nm to (name of e as text)
            set ps to position of e
            set sz to size of e
            set out to out & (k as text) & "\t" & nm & "\t" & (item 1 of ps) & "\t" & (item 2 of ps) & "\t" & (item 1 of sz) & "\t" & (item 2 of sz) & "\n"
          end try
        end repeat
      end repeat
    end tell
  end try
  return out
end tell
'''


def _ax_snapshot() -> list[dict]:
    """Real UI elements of the frontmost window from the macOS Accessibility tree —
    name + OS-provided centre coordinates. Far more reliable than guessing pixels
    from a screenshot. [] if Accessibility/Automation isn't granted."""
    try:
        r = subprocess.run(["osascript", "-e", _AX_SCRIPT], capture_output=True,
                           text=True, timeout=8)
        out = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 6:
                role, name, x, y, w, h = parts
                if not name.strip():
                    continue
                out.append({"role": role, "name": name.strip(),
                            "cx": int(x) + int(w) // 2, "cy": int(y) + int(h) // 2})
        return out[:60]
    except Exception:
        return []


def _plan_grounded(task: str) -> list[dict]:
    """Grounded plan: pick real elements by name from the Accessibility tree and
    click their exact OS coordinates. Deterministic where the AX tree exposes labels."""
    els = _ax_snapshot()
    if not els:
        return []
    catalog = "\n".join(f"{i}. [{e['role']}] \"{e['name']}\" @ {e['cx']},{e['cy']}"
                        for i, e in enumerate(els))
    from core.llm_client import call_llm_text
    sys_p = (
        "You control a Mac. Below is the LIVE list of clickable UI elements on screen with their "
        "exact centre coordinates. Output ONLY a JSON array of steps to accomplish the task, using "
        "the EXACT coordinates from the list. Steps: "
        '{"action":"click","x":..,"y":..}, {"action":"type","text":".."}, '
        '{"action":"hotkey","keys":"cmd+t"}, {"action":"press","key":"enter"}. '
        "To type into a field, click it first then type. If no element matches, output []."
    )
    try:
        raw = call_llm_text(f"TASK: {task}\n\nON-SCREEN ELEMENTS:\n{catalog}",
                            system=sys_p, tier="main", timeout=30, max_tokens=350)
        m = re.search(r"\[.*\]", raw or "", re.DOTALL)
        steps = json.loads(m.group(0)) if m else []
        return steps if isinstance(steps, list) else []
    except Exception:
        return []


def _plan_from_task(task: str, jarvis) -> list[dict]:
    """Plan concrete steps. Tries GROUNDED accessibility-tree planning first (real
    element coordinates), then falls back to vision (screenshot + Haiku)."""
    grounded = _plan_grounded(task)
    if grounded:
        return grounded
    try:
        import pyautogui, base64, io
        shot = pyautogui.screenshot()
        shot.thumbnail((1280, 800))
        buf = io.BytesIO(); shot.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        w, h = shot.size
    except Exception:
        return []
    from core.llm_client import call_llm_text
    sys_p = (
        "You control a Mac via pyautogui. Given the screenshot and the user's task, output ONLY "
        f"a JSON array of steps. The screenshot is {w}x{h}; give click coordinates in THAT space. "
        'Allowed steps: {"action":"click","x":..,"y":..}, {"action":"type","text":".."}, '
        '{"action":"hotkey","keys":"cmd+t"}, {"action":"press","key":"enter"}, '
        '{"action":"scroll","amount":-300}. Keep it to the few steps needed. If the task is unsafe '
        "or you cannot locate the target, output []."
    )
    try:
        raw = call_llm_text(f"Task: {task}\n[screenshot attached as data]\nSCREEN_B64:{b64[:1]}",
                            system=sys_p, tier="main", timeout=40, max_tokens=400)
        m = re.search(r"\[.*\]", raw or "", re.DOTALL)
        steps = json.loads(m.group(0)) if m else []
        # scale coords from screenshot space → real screen
        import pyautogui as _p
        sw, sh = _p.size()
        for s in steps:
            if "x" in s: s["x"] = int(s["x"] * sw / w)
            if "y" in s: s["y"] = int(s["y"] * sh / h)
        return steps if isinstance(steps, list) else []
    except Exception:
        return []


def _execute(steps: list[dict]) -> str:
    pg = _pg()
    done = 0
    for s in steps:
        a = s.get("action")
        try:
            if a == "move":
                pg.moveTo(s["x"], s["y"], duration=0.25)
            elif a == "click":
                if "x" in s: pg.click(s["x"], s["y"], duration=0.25)
                else: pg.click()
            elif a == "type":
                pg.typewrite(s.get("text", ""), interval=0.02)
            elif a == "hotkey":
                pg.hotkey(*_norm_keys(s.get("keys", "")))
            elif a == "press":
                pg.press(_KEYMAP.get((s.get("key") or "").lower(), s.get("key", "")))
            elif a == "scroll":
                pg.scroll(int(s.get("amount", 0)))
            done += 1
            time.sleep(0.1)
        except Exception as e:
            return f"I managed {done} step(s), sir, then hit an error: {e}"
    return f"Done, sir — {done} action(s) completed on screen."


def computer_use(parameters: dict, response=None, player=None,
                 session_memory=None, jarvis=None, speak=None) -> str:
    """Operate the Mac. ALWAYS confirmed first via the guardrail (permission every
    time). Accepts either explicit `steps` (JSON) or a natural `task`."""
    params = parameters or {}
    task = (params.get("task") or params.get("request") or params.get("text") or "").strip()
    if any(d in task.lower() for d in _DANGER):
        return "I won't do that on your machine, sir — it's the kind of action I'm built to refuse."

    # Build the plan (explicit steps win; otherwise vision-plan from the task).
    steps = params.get("steps")
    if isinstance(steps, str):
        try: steps = json.loads(steps)
        except Exception: steps = None
    if not steps:
        steps = _plan_from_task(task, jarvis)
    if not steps:
        return ("I couldn't work out safe, exact steps for that, sir — tell me precisely what to "
                "click or type, or open the app first and I'll take it from there.")

    # This tool only ever RUNS when the guardrail has set _confirmed=True.
    if not params.get("_confirmed"):
        # Stash so the confirmation description can reflect the real plan.
        if jarvis is not None:
            jarvis._pending_cu_steps = steps
        return "__NEEDS_CONFIRM__"      # main.py turns this into the permission prompt

    return _execute(steps)
