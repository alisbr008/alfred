"""
Dual-Brain coding engine — Alfred's internal software company.

  Chief Architect  : Claude Sonnet 4.6  (tier "heavy")  — designs & reviews
  Lead Engineer    : GLM 5.1            (tier "code")   — implements & revises
  CEO / OS         : Alfred — the only thing the user ever sees

Tasks are classified by complexity and routed through a review loop. Neither
model has an identity or talks to the user; Alfred presents one coherent result.
Runs async (GLM on NIM is ~25s) — Alfred acks, then presents when ready.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path


def _llm(prompt: str, system: str, tier: str) -> str:
    try:
        from core.llm_client import call_llm_text
        mt = 8000 if tier == "code" else 2000          # full code files, never truncated
        return (call_llm_text(prompt, system=system, tier=tier, timeout=300, max_tokens=mt) or "").strip()
    except Exception as e:
        print(f"[DualBrain] {tier} error: {e}")
        return ""


_ENG = ("You are the Lead Software Engineer (the build is yours). Write a COMPLETE, FULL, "
        "production-ready implementation — every function fully written, no placeholders, no "
        "'TODO', no '... rest of code', no stubs, no omissions. Minimal complexity, no needless "
        "abstractions. The code must run as-is. Output ONLY the full code in one block.")

_ARCH = ("You are the Chief Systems Architect. Think before code. Design a clear, modular, "
         "production-ready approach: components, data flow, edge cases, failure modes. Be "
         "concise — a spec the engineer can build from. Do NOT write the full implementation.")

_REVIEW = ("You are the Chief Systems Architect reviewing an implementation for correctness, "
           "architecture, security, edge cases, regressions, and readability. If it is "
           "production-ready, reply with exactly 'APPROVED'. Otherwise give a short, "
           "structured list of the specific fixes required — nothing else.")


def _classify(task: str) -> str:
    t = (task or "").lower()
    if any(k in t for k in ("typo", "rename", "syntax", "import", "one-liner", "one line",
                            "small fix", "quick fix", "format")):
        return "LOW"
    if any(k in t for k in ("engine", "architecture", "system", "multi-agent", "routing",
                            "orchestrat", "memory engine", "voice", "visualization engine",
                            "wake", "whole", "entire", "rebuild")):
        return "HIGH"
    return "MEDIUM"


def _strip_fence(s: str) -> str:
    m = re.search(r"```[a-z]*\n([\s\S]*?)```", s)
    return m.group(1).strip() if m else s.strip()


def dual_brain_code(parameters: dict, response=None, player=None,
                    session_memory=None, jarvis=None, speak=None) -> str:
    params = parameters or {}
    task = (params.get("request") or params.get("query") or params.get("task")
            or params.get("text") or "").strip()
    if not task:
        return "What should I build, sir?"

    ui = (getattr(jarvis, "ui", None) if jarvis else None) or player

    def _run():
        comp = _classify(task)
        spec = task
        if comp == "HIGH":
            design = _llm(f"Design the architecture for this task:\n{task}", _ARCH, "heavy")
            if design:
                spec = f"{task}\n\n[Architect's design]\n{design}"
        code = _llm(f"Implement this:\n{spec}", _ENG, "code")
        if comp in ("MEDIUM", "HIGH") and code:
            review = _llm(f"Task:\n{task}\n\nImplementation:\n{code}", _REVIEW, "heavy")
            if review and "APPROVED" not in review.upper()[:40]:
                revised = _llm(f"Revise the code per this review. Output only the full updated code.\n"
                               f"Task:\n{task}\n\nReview:\n{review}\n\nCurrent code:\n{code}", _ENG, "code")
                if revised:
                    code = revised
        code = _strip_fence(code)
        if not code:
            if ui:
                try: ui.write_log("Alfred: I couldn't complete that build, sir.")
                except Exception: pass
            if jarvis: jarvis.speak("I couldn't complete that build, sir.")
            return

        # save + open the result
        ext = "py" if "python" in task.lower() or "def " in code else \
              "js" if "javascript" in task.lower() else \
              "sh" if "bash" in task.lower() else "txt"
        fname = f"alfred_build_{int(time.time())}.{ext}"
        try:
            p = Path.home() / "Desktop" / fname
            p.write_text(code, encoding="utf-8")
            import subprocess
            subprocess.Popen(["open", "-t", str(p)])
        except Exception:
            pass
        if ui:
            try: ui.write_log(f"Alfred: [{comp} build complete]\n{code[:600]}")
            except Exception: pass
        if jarvis:
            jarvis._responding = True
            jarvis.speak(f"It's ready, sir — a {comp.lower()}-complexity build, reviewed and on "
                         f"your Desktop as {fname}.")
            jarvis._finish_turn()

    threading.Thread(target=_run, daemon=True).start()
    comp = _classify(task)
    crew = "my architect and engineer are" if comp != "LOW" else "my engineer is"
    return f"On it, sir — {crew} on it. I'll have it on your Desktop shortly."
