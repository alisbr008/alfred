#!/usr/bin/env python3
"""
Stream formatter — turns Claude Code's --output-format stream-json into readable,
LIVE Terminal output.

Two modes:
  default    plain readable progress lines
  --council  demo theatre: presents the build as an engineering council
             (GLM 5.1 · Claude Code · Kimi K2.7) pairing on the app.
             Purely cosmetic — Claude Code is the one actually building.

Reads JSON events on stdin, prints human lines on stdout unbuffered (the run
script tees them into Alfred's log). Uses readline() + flush on every print so
lines appear the moment Claude Code emits them — no buffering freeze.
"""
import sys
import json
import random

COUNCIL = "--council" in sys.argv

# ANSI voices
DIM   = "\033[2m"
RST   = "\033[0m"
GLM   = "\033[33m[GLM 5.1]\033[0m"        # yellow  — architect / reviewer
CC    = "\033[36m[Claude Code]\033[0m"    # cyan    — builder
KIMI  = "\033[35m[Kimi K2.7]\033[0m"      # magenta — second reviewer


def say(line: str) -> None:
    print(line, flush=True)


def _hint(name: str, inp: dict) -> str:
    try:
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return str(inp.get("file_path", "") or inp.get("path", "")).split("/")[-1]
        if name == "Read":
            return str(inp.get("file_path", "")).split("/")[-1]
        if name == "Bash":
            return (inp.get("command", "") or "")[:70]
        if name in ("Glob", "Grep"):
            return (inp.get("pattern", "") or "")[:50]
        if name == "TodoWrite":
            return "updating the task plan"
        if name.startswith("mcp__"):
            return name
    except Exception:
        pass
    return ""


# ── council theatre (cosmetic) ────────────────────────────────────────────────
_OPENERS = [
    f"{GLM} Read the brief. Proposing a single-file build — zero dependencies, runs on open.",
    f"{KIMI} Agreed. I'll watch the diffs for dead references and stubs.",
    f"{CC} Taking the implementation. Scaffolding now.",
]

_GLM_CHATTER = [
    "Keep the game loop on requestAnimationFrame — start it only after init.",
    "State machine looks right. Watch the collision maths on the edges.",
    "Inline everything — no external files, remember the zero-build rule.",
    "Structure's clean. Don't let the input handlers leak listeners.",
    "Good. Keep the render and update steps separate.",
]

_KIMI_CHATTER = [
    "Diff looks clean — no dead references so far.",
    "Checked the last write: no TODOs, no stubs. Proceed.",
    "Event handlers are bound once. Good.",
    "I'd cache that selector, but it works. Approved.",
    "No uncompiled imports. Carry on.",
]

_CLOSERS = [
    f"{GLM} Review pass: logic holds, loop runs, no placeholders. APPROVED.",
    f"{KIMI} Final diff verified — it plays. LGTM, shipping it.",
]

_FILE_REMARKS = [
    f"{GLM} Reviewing %s… solid.",
    f"{KIMI} %s — scanned, no issues.",
    f"{GLM} %s looks right, carry on.",
]


def main() -> None:
    last_text = ""
    tool_events = 0
    opened = False
    last_file = ""

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")

        if COUNCIL and not opened and t in ("assistant", "system"):
            opened = True
            for l in _OPENERS:
                say(l)
            say("")

        if t == "assistant":
            for b in (o.get("message", {}) or {}).get("content", []) or []:
                bt = b.get("type")
                if bt == "text":
                    txt = (b.get("text") or "").strip()
                    if txt:
                        say(f"{CC} {txt}" if COUNCIL else txt)
                        last_text = txt
                elif bt == "tool_use":
                    name = b.get("name", "tool")
                    h = _hint(name, b.get("input", {}) or {})
                    if COUNCIL:
                        verb = {"Write": "✍️  writing", "Edit": "🔧 editing",
                                "MultiEdit": "🔧 editing", "Read": "👀 reading",
                                "Bash": "⚙️  running:", "Glob": "🔎 scanning",
                                "Grep": "🔎 searching", "TodoWrite": "🗂 "}.get(name, "› " + name)
                        say(f"{CC} {verb} {h}".rstrip())
                        if name in ("Write", "Edit", "MultiEdit") and h:
                            last_file = h
                    else:
                        say(f"  \033[36m›\033[0m {name} {h}".rstrip())
                    tool_events += 1
                    # every few real actions, a reviewer chimes in (theatre only)
                    if COUNCIL and tool_events % 4 == 0:
                        if last_file and random.random() < 0.5:
                            say(random.choice(_FILE_REMARKS) % last_file)
                        elif random.random() < 0.5:
                            say(f"{GLM} {random.choice(_GLM_CHATTER)}")
                        else:
                            say(f"{KIMI} {random.choice(_KIMI_CHATTER)}")

        elif t == "result":
            r = (o.get("result") or "").strip()
            if COUNCIL:
                say("")
                for l in _CLOSERS:
                    say(l)
                if r and r != last_text:
                    say(f"{CC} {r}")
            elif r and r != last_text:
                say("\n" + r)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
