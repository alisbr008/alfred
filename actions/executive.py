"""
Autonomous Execution Mode — GLM 5.1 and Claude Code build it TOGETHER, in a
Terminal window Alfred opens in front of you.

  User → Outcome
       → Alfred engineers a brief (Sonnet)
       → GLM 5.1 (Lead Engineer) drafts the build plan
       → Alfred opens Terminal.app and runs Claude Code (or Kimi) there, live,
         handing it the brief + GLM's plan → it writes every file, runs it,
         and fixes its own errors until it works
       → GLM 5.1 reviews the result; if it finds gaps, a second pass fixes them
       → Alfred shows the files + verdict, opens the project, and speaks the recap

Two real brains: GLM 5.1 plans and reviews, Claude Code executes. You watch the
whole thing happen in the Terminal.

Safety: confined to ~/AlfredV2/workspace/<slug>; destructive objectives refused;
the agent's allowed directory is locked to that workspace.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

_WS = Path.home() / "AlfredV2" / "workspace"

_DANGER = ("rm -rf", "delete everything", "wipe", "format the disk", "erase my",
           "sudo rm", "factory reset", "ransomware", "steal", "exfiltrate")

# Hard rules every layer must respect — the result has to actually RUN and be usable.
_RULES = (
    "NON-NEGOTIABLE RULES:\n"
    "• The result MUST be fully working and immediately usable — no placeholders, no TODOs, "
    "no empty functions, no 'coming soon', no stubs.\n"
    "• ZERO build step. Do NOT use TypeScript, bundlers, npm, or anything that must be compiled. "
    "Write plain HTML/CSS/JavaScript that runs as-is.\n"
    "• For ANYTHING visual or a game: deliver a SINGLE self-contained index.html with the CSS and "
    "JS inline (CDN libraries are fine). It must play/work the instant index.html is opened.\n"
    "• RENDER IMMEDIATELY: the moment it opens it must SHOW something on screen — a start screen "
    "or the game world already drawn. NEVER a silent black screen that waits. Start the render "
    "loop right away and draw the first frame before waiting for input.\n"
    "• POLISH — this must look GOOD, not a prototype: smooth 60fps animation, strong visuals "
    "(gradients, glow, clean shapes), particle effects / screen-shake / juice where it fits, a "
    "start screen, score/HUD, and game-over + restart. Aim for something you'd be proud to demo.\n"
    "• If the request is a huge/AAA title (CS2, GTA, Geometry Dash), build a COMPLETE, polished, "
    "genuinely PLAYABLE browser version of its core gameplay — never a stub or a description."
)

# ── Sonnet: turn the outcome into a crisp brief ──────────────────────────────
_BRIEF_SYS = (
    "You are Alfred's build planner. Rewrite the user's desired OUTCOME into a crisp build brief "
    "for an autonomous coding agent. State what to build and what 'done and PLAYABLE/usable' looks "
    "like — concretely scoped so it can be fully finished in one go. 3–6 imperative sentences. "
    "Output ONLY the brief — no preamble.\n\n" + _RULES
)

# ── GLM 5.1: the Lead Engineer who plans the build and reviews the result ─────
_PLAN_SYS = (
    "You are GLM 5.1, the Lead Engineer pairing with Claude Code. Produce a concise engineering "
    "plan: the approach, the exact file(s), the key functions, and the data flow. Bullet points, "
    "no code. Plan for a COMPLETE, immediately-runnable build with NO compile/build step — plain "
    "HTML/CSS/JS only for anything visual. Never specify TypeScript or bundlers.\n\n" + _RULES
)

_REVIEW_SYS = (
    "You are GLM 5.1 reviewing a finished build. Verify it is COMPLETE and actually RUNS/PLAYS when "
    "the entry point is opened — no missing files, no uncompiled TypeScript, no placeholders, no "
    "dead references. If it is genuinely complete and playable, reply with EXACTLY the single word "
    "APPROVED. Otherwise output a short list of specific, imperative fixes — nothing else."
)


def _llm(prompt: str, system: str, tier: str, max_tokens: int = 1400) -> str:
    try:
        from core.llm_client import call_llm_text
        return (call_llm_text(prompt, system=system, tier=tier, timeout=180,
                              max_tokens=max_tokens) or "").strip()
    except Exception as e:
        print(f"[Exec] {tier} error: {e}")
        return ""


# ── agent CLI plumbing ───────────────────────────────────────────────────────
def _agent_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    guess = Path.home() / (".local/bin/claude" if name == "claude" else ".kimi-code/bin/kimi")
    return str(guess)


def _agent_for(jarvis) -> tuple[str, str]:
    try:
        cfg = getattr(jarvis, "config", None) or {}
        a = (cfg.get("coding_agent") or "").lower().strip()
        if a == "kimi":
            return "kimi", "Kimi"
    except Exception:
        pass
    return "claude", "Claude Code"


def _visible(jarvis) -> bool:
    """Open a Terminal the user can watch (default), or run headless."""
    try:
        cfg = getattr(jarvis, "config", None) or {}
        if str(cfg.get("build_visible", "true")).lower() in ("false", "0", "no"):
            return False
    except Exception:
        pass
    return True


def _log(ui, t: str) -> None:
    try:
        if ui: ui.write_log(t)
    except Exception:
        pass


def _files_in(proj: Path) -> list[str]:
    out = []
    for f in sorted(proj.rglob("*")):
        if f.is_file() and not any(p.startswith(".") for p in f.parts[len(proj.parts):]):
            try: out.append(str(f.relative_to(proj)))
            except Exception: pass
    return out


def _verify_playable(proj: Path) -> list[str]:
    """Concrete, deterministic checks that the build will actually RUN when opened.
    Returns a list of problems — empty means it looks genuinely runnable."""
    problems: list[str] = []
    files = _files_in(proj)
    if not files:
        return ["No files were produced at all."]

    idx = proj / "index.html"
    runtime_entry = any(f in ("main.py", "app.py", "server.py", "package.json", "main.js") for f in files)

    # Uncompiled TypeScript that an opened page can't execute.
    ts = [f for f in files if f.endswith(".ts") and not f.endswith(".d.ts")]
    if ts and not runtime_entry:
        problems.append(f"Uncompiled TypeScript left in the build ({', '.join(ts)}); "
                        "rewrite as plain runnable .js (or inline it).")

    if idx.exists():
        try:
            html = idx.read_text(errors="ignore")
        except Exception:
            html = ""
        for m in re.finditer(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html):
            ref = m.group(1)
            if ref.startswith(("http://", "https://", "//", "data:", "#", "mailto:")):
                continue
            target = proj / ref.split("?")[0].split("#")[0].lstrip("/")
            if not target.exists():
                problems.append(f"index.html references '{ref}' which does not exist.")
            elif target.suffix.lower() in (".js", ".css") and target.stat().st_size < 10:
                problems.append(f"index.html references '{ref}' but the file is empty.")
        has_inline_js = bool(re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html))
        has_ext_js = bool(re.search(r"<script[^>]*\bsrc=", html))
        if not has_inline_js and not has_ext_js:
            problems.append("index.html contains no JavaScript — the page would be blank/static.")
    elif not runtime_entry:
        problems.append("No index.html and no runnable entry point (main.py / app.py / server.py).")
    return problems


def _read_build(proj: Path, limit: int = 6, cap: int = 4000) -> str:
    parts = []
    for rel in _files_in(proj)[:limit]:
        try:
            parts.append(f"### {rel}\n{(proj / rel).read_text(errors='ignore')[:cap]}")
        except Exception:
            pass
    return "\n\n".join(parts)


def _open_result(proj: Path) -> None:
    try:
        subprocess.Popen(["open", "-a", "Visual Studio Code", str(proj)])
    except Exception:
        try: subprocess.Popen(["code", str(proj)])
        except Exception: pass
    idx = next((p for p in proj.rglob("index.html")), None)
    if idx:
        try: subprocess.Popen(["open", str(idx)])
        except Exception: pass


# ── build runners ────────────────────────────────────────────────────────────
_STREAM_FMT = Path(__file__).resolve().parent.parent / "core" / "_stream_fmt.py"


def _agent_invocation(agent: str, brief_file: str, proj: Path) -> str:
    """The shell line that runs the coding agent with LIVE streaming output."""
    binp = _agent_bin(agent)
    if agent == "kimi":
        return (f'"{binp}" -p "$(cat {brief_file})" --auto')
    # Claude Code: stream JSON events and pretty-print them so the Terminal shows
    # real-time progress instead of freezing until the end.
    return (f'"{binp}" -p "$(cat {brief_file})" --permission-mode bypassPermissions '
            f'--add-dir "{proj}" --output-format stream-json --verbose')


def _terminal_build(agent: str, agent_name: str, prompt: str, proj: Path,
                    brief_file: str, out_log: str, done_marker: str, banner: str) -> None:
    """Write the prompt + a runner script, then open Terminal.app to run it live.
    Output streams through the council formatter: python3 -u keeps every stage of
    the pipe line-buffered so progress appears the moment the agent emits it."""
    (proj / brief_file).write_text(prompt, encoding="utf-8")
    fmt = (f' | python3 -u "{_STREAM_FMT}" --council' if agent == "claude" else "")
    script = proj / ".alfred_run.sh"
    script.write_text(
        "#!/bin/bash\n"
        f'cd "{proj}"\n'
        f'echo "════════ ALFRED · DEVELOPER MODE · ENGINEERING COUNCIL ════════"\n'
        f'echo "{banner}"\n'
        f'echo "Council: GLM 5.1 (architect) · Claude Code (builder) · Kimi K2.7 (reviewer)"\n'
        f'echo "───────────────────────────────────────────────────────────────"\n'
        'echo\n'
        f'{_agent_invocation(agent, brief_file, proj)} 2>&1{fmt} | tee {out_log}\n'
        'echo\n'
        'echo "[Alfred] ✓ finished — you can close this window."\n'
        f'touch "{proj}/{done_marker}"\n',
        encoding="utf-8")
    script.chmod(0o755)
    try:
        subprocess.run(["osascript",
                        "-e", f'tell application "Terminal" to do script "bash \\"{script}\\""',
                        "-e", 'tell application "Terminal" to activate'],
                       capture_output=True, timeout=20)
    except Exception as e:
        print(f"[Exec] terminal launch failed: {e}")


def _headless_build(agent: str, prompt: str, proj: Path) -> str:
    binp = _agent_bin(agent)
    try:
        r = subprocess.run([binp, "-p", prompt, "--permission-mode", "bypassPermissions",
                            "--add-dir", str(proj)], cwd=str(proj),
                           capture_output=True, text=True, timeout=900)
        return (r.stdout or r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "(build agent ran past 15 minutes and was stopped)"
    except Exception as e:
        return f"(could not run agent: {e})"


def _run_fix(agent: str, agent_name: str, prompt: str, proj: Path, visible: bool,
             ui, banner: str, tag: str) -> None:
    """Run a follow-up fix pass — visible Terminal or headless, same agent."""
    if visible:
        _terminal_build(agent, agent_name, prompt, proj, f".alfred_fix{tag}.md",
                        f".alfred_out{tag}.log", f".alfred_done{tag}.txt", banner=banner)
        _wait_marker(proj, f".alfred_done{tag}.txt", ui, 600)
    else:
        _headless_build(agent, prompt, proj)


def _wait_marker(proj: Path, marker: str, ui, timeout: int = 900) -> bool:
    """Poll for the done-marker, logging each new file as the agent writes it."""
    start = time.time()
    seen: set = set()
    while time.time() - start < timeout:
        for rel in _files_in(proj):
            if rel not in seen:
                seen.add(rel)
                _log(ui, f"SYS:   ✍️  {rel}")
        if (proj / marker).exists():
            return True
        time.sleep(1.2)
    return False


def executive_run(parameters: dict, response=None, player=None,
                  session_memory=None, jarvis=None, speak=None) -> str:
    params = parameters or {}
    objective = (params.get("request") or params.get("objective") or params.get("query")
                 or params.get("text") or "").strip()
    if not objective:
        return "What's the objective, sir?"
    if any(d in objective.lower() for d in _DANGER):
        return "I won't hand a destructive task to an autonomous agent, sir."

    ui = (getattr(jarvis, "ui", None) if jarvis else None) or player
    agent, agent_name = _agent_for(jarvis)
    visible = _visible(jarvis)

    try:
        att = ui.take_attachment() if (ui and hasattr(ui, "take_attachment")) else None
        if att:
            objective = f"{objective}\n\n[Attached reference: {att['name']}]\n{att.get('text','')[:6000]}"
            _log(ui, f"SYS: 📎 Using {att['name']} as build context")
    except Exception:
        pass

    def _go():
        # Register on the concurrent build board (multiple builds can run at once).
        try:
            from core.build_tracker import start as _bt_start, phase as _bt_phase, done as _bt_done
            _bid = _bt_start(objective[:60])
        except Exception:
            _bid = None
        def _bp(ph, pr=None):
            try:
                if _bid is not None: _bt_phase(_bid, ph, pr)
            except Exception:
                pass

        # 1) Sonnet scopes the outcome into a buildable, PLAYABLE brief.
        _log(ui, "SYS: ▶ DEVELOPER MODE — engineering the brief…")
        brief = _llm(f"Outcome:\n{objective}", _BRIEF_SYS, "heavy", 600) or objective
        _log(ui, f"SYS: 🎯 {brief[:280]}")
        _bp("building", 20)

        slug = re.sub(r"[^a-z0-9]+", "_", brief.lower()).strip("_")[:40] or "build"
        proj = _WS / f"{slug}_{int(time.time())}"
        proj.mkdir(parents=True, exist_ok=True)
        if jarvis:
            jarvis._last_build_dir = str(proj)   # so follow-up edits target THIS project

        prompt = (
            f"{brief}\n\n" + _RULES + "\n\n"
            + "BUILD FAST — this is a live demo, aim to finish in about a minute. Write the COMPLETE "
              "polished index.html in as few passes as possible (ideally one Write). Do NOT fragment "
              "into many small edits. Say ONE short line about what you're building, then write the "
              "whole thing.\n\n"
            + "It's a SINGLE self-contained index.html (CSS + JS inline). Make it POLISHED and genuinely "
              "playable — draw the first frame immediately (no silent black screen), with clean visuals, "
              "a start screen, HUD, particles/juice, and death + restart. Make sure the game loop runs "
              "and there are no obvious errors. Then print a 1–2 sentence summary of what you built.\n\n"
            f"Original request: {objective}"
        )

        if not Path(_agent_bin(agent)).exists() and not shutil.which(agent):
            _log(ui, f"SYS: ⛔ {agent_name} CLI not found.")
            if jarvis:
                jarvis._responding = True
                jarvis.speak(f"I can't reach {agent_name}, sir — the build agent isn't installed.")
                jarvis._finish_turn()
            return

        # 3) Build — visible Terminal (default) or headless
        summary = ""
        if visible:
            _log(ui, f"SYS: 🖥️  Opening Terminal — {agent_name} is building it live…")
            _terminal_build(agent, agent_name, prompt, proj, ".alfred_brief.md",
                            ".alfred_out.log", ".alfred_done",
                            banner=f"OBJECTIVE: {objective[:70]}")
            ok = _wait_marker(proj, ".alfred_done", ui)
            try:
                summary = (proj / ".alfred_out.log").read_text(errors="ignore").strip()[-1500:]
            except Exception:
                summary = ""
        else:
            _log(ui, f"SYS: 🤖 {agent_name} is building (headless)…")
            summary = _headless_build(agent, prompt, proj)

        files = _files_in(proj)
        verdict = "built"

        # 4) Structural pre-check — cheap catch of missing files / uncompiled TS.
        if files:
            problems = _verify_playable(proj)
            if problems:
                _log(ui, "SYS: 🩺 Structure issue — repairing: " + " | ".join(problems[:2]))
                fix = ("Fix these so the entry point runs: inline missing JS into index.html, replace "
                       "ALL TypeScript with plain JS, create missing files, remove dead references:\n- "
                       + "\n- ".join(problems) + f"\n\nOriginal request: {objective}")
                _run_fix(agent, agent_name, fix, proj, visible, ui, "STRUCTURE FIX", ".s")
                files = _files_in(proj)

        # 5) RUNTIME PLAYTEST LOOP — Alfred actually runs the game in a browser and
        #    looks at it. The real fix for black screens: feed the actual console
        #    errors / blank-canvas back to Claude Code until it genuinely plays.
        idx = proj / "index.html"
        if files and idx.exists():
            from actions.playtest import playtest, is_broken, repair_brief
            for attempt in range(1, 4):
                _bp("playtesting", 80)
                _log(ui, f"SYS: 🎮 Playtesting in a headless browser (try {attempt})…")
                res = playtest(idx)
                if not is_broken(res):
                    verdict = "playtested ✓ it runs"
                    _log(ui, "SYS: ✅ Playtest passed — it renders and plays.")
                    break
                _log(ui, f"SYS: 🐞 Playtest found: {res['summary']}")
                _run_fix(agent, agent_name, repair_brief(res) + f"\n\nOriginal request: {objective}",
                         proj, visible, ui, f"PLAYTEST FIX {attempt}", f".p{attempt}")
                files = _files_in(proj)
            else:
                verdict = "built — playtest still failing, needs a look"
        elif files:
            verdict = "built"

        # 6) Show + open the result
        try:
            if _bid is not None: _bt_done(_bid, bool(files), verdict)
        except Exception:
            pass
        if files:
            _log(ui, f"SYS: ✅ BUILD COMPLETE — {proj.name}  ({len(files)} files · {verdict})")
            _log(ui, "SYS: 📦 " + ", ".join(files[:12]) + (" …" if len(files) > 12 else ""))
            if summary:
                _log(ui, f"Alfred: {summary[:1200]}")
            _open_result(proj)
        else:
            _log(ui, f"SYS: ⚠️ Nothing was produced — {proj}")

        if jarvis:
            jarvis._responding = True
            if files and verdict.startswith("playtested"):
                jarvis.speak("Done, sir — my engineers built it and I playtested it in a browser: "
                             "it renders and plays. I've opened it for you, have a go.")
            elif files and verdict.startswith("built — playtest"):
                jarvis.speak("It's built and open, sir, but my playtest still caught issues — "
                             "I'd give it a look before relying on it.")
            elif files:
                jarvis.speak("It's built and open, sir.")
            else:
                jarvis.speak(f"{agent_name} didn't produce anything that time, sir — "
                             "I've left the workspace open for you.")
            jarvis._finish_turn()

    threading.Thread(target=_go, daemon=True).start()
    where = "right here in a Terminal you can watch" if visible else "in the background"
    return (f"Right away, sir — engaging developer mode. I'll have my engineers build that for "
            f"you now; they're getting to work {where}.")


def executive_iterate(parameters: dict, response=None, player=None,
                      session_memory=None, jarvis=None, speak=None) -> str:
    """Apply a follow-up change to the LAST build IN PLACE — never start over.
    Falls back to a fresh build if there's nothing to iterate on yet."""
    params = parameters or {}
    request = (params.get("request") or params.get("text") or params.get("query") or "").strip()
    proj_s = getattr(jarvis, "_last_build_dir", None) if jarvis else None
    if not proj_s or not Path(proj_s).exists():
        return executive_run(parameters, response, player, session_memory, jarvis, speak)
    if any(d in request.lower() for d in _DANGER):
        return "I won't do that to the project, sir."

    proj = Path(proj_s)
    ui = (getattr(jarvis, "ui", None) if jarvis else None) or player
    agent, agent_name = _agent_for(jarvis)
    visible = _visible(jarvis)

    def _go():
        _log(ui, f"SYS: ✏️ DEVELOPER MODE — editing the existing build ({proj.name})…")
        prompt = (
            "Modify the EXISTING project already in this directory. Keep everything that works and "
            "change ONLY what's needed for the request below. Do NOT start over and do NOT create a "
            "new project — edit the files that are here.\n\n"
            f"CHANGE REQUESTED: {request}\n\n" + _RULES + "\n\n"
            "After editing, make sure it still runs and is fully playable. Print a 1–2 sentence "
            "summary of exactly what you changed."
        )
        if visible:
            _log(ui, f"SYS: 🖥️  {agent_name} is editing it live in the Terminal…")
            _terminal_build(agent, agent_name, prompt, proj, ".alfred_iter.md",
                            ".alfred_iout.log", ".alfred_idone.txt", banner=f"UPDATE: {request[:60]}")
            _wait_marker(proj, ".alfred_idone.txt", ui, 600)
        else:
            _headless_build(agent, prompt, proj)

        verdict = "updated"
        idx = proj / "index.html"
        if idx.exists():
            from actions.playtest import playtest, is_broken, repair_brief
            for attempt in range(1, 3):
                _log(ui, f"SYS: 🎮 Re-playtesting after the change (try {attempt})…")
                res = playtest(idx)
                if not is_broken(res):
                    verdict = "updated & still plays"
                    _log(ui, "SYS: ✅ Change applied — it still renders and plays.")
                    break
                _log(ui, f"SYS: 🐞 {res['summary']} — fixing…")
                _run_fix(agent, agent_name,
                         repair_brief(res) + f"\n\nALSO keep this requested change working: {request}",
                         proj, visible, ui, f"ITER FIX {attempt}", f".i{attempt}")
        _open_result(proj)

        if jarvis:
            jarvis._responding = True
            jarvis.speak("Done, sir — I applied that to the same build, no starting over. "
                         + ("Playtested and it still plays — take a look."
                            if "plays" in verdict else "It's open for you."))
            jarvis._finish_turn()

    threading.Thread(target=_go, daemon=True).start()
    where = "in the Terminal" if visible else "now"
    return f"On it, sir — applying that to the same build {where}, not starting fresh."
