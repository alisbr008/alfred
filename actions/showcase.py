"""
SHOWCASE — Alfred's self-introduction tour, built for demos.

"What makes you different?" → Alfred answers by SHOWING it: a scripted, cinematic
sequence where each spoken line drives a scene change — brain architecture, the
Agent Council, the batcomputer mission board, the visualization engine, the live
world — and closes on the dashboard.

Design rules (all learned the hard way):
  • ZERO LLM calls — every line is pre-scripted, so the tour starts instantly and
    never stalls mid-demo.
  • Narration and visuals are SYNCED: the next scene appears only after the
    current line has finished playing through TTS.
  • Half-duplex for the whole tour (mic gated) — Alfred can't hear himself and
    trigger phantom commands on camera. Barge-in still works: say "wait" to stop.
"""
from __future__ import annotations

import threading
import time


def _wait_speech(jarvis, timeout: float = 25.0) -> None:
    """Block until the TTS queue has fully drained (the line finished playing)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if getattr(jarvis, "_barged", False):
            return                                    # user said "wait" — stop pacing
        try:
            idle = jarvis._tts_queue.empty() and not jarvis._speaking
            aq = getattr(jarvis, "_audio_q", None)
            idle = idle and (aq is None or aq.empty())
        except Exception:
            idle = True
        if idle:
            time.sleep(0.35)                          # grace: queue can dip empty between clips
            try:
                if jarvis._tts_queue.empty() and not jarvis._speaking:
                    return
            except Exception:
                return
        time.sleep(0.12)


def _scene_intro(jarvis, ui):
    ui.close_overlays()
    ui.set_state("SHOWCASE")


def _greeting() -> str:
    h = time.localtime().tm_hour
    return "Good morning" if h < 12 else ("Good afternoon" if h < 18 else "Good evening")


def _scene_brain(jarvis, ui):
    ui.show_scheme()


def _scene_architecture(jarvis, ui):
    try:
        from actions.visualize import _gen_architecture
        ui.close_overlays()
        ui.show_visualization(_gen_architecture())
    except Exception as e:
        print(f"[Showcase] architecture scene: {e}")


def _scene_council(jarvis, ui):
    try:
        from actions.agents import council_dashboard
        ui.close_overlays()
        ui.show_council(council_dashboard({}))
    except Exception:
        pass


def _scene_missions(jarvis, ui):
    try:
        from actions.missions import board_payload
        ui.close_overlays()
        ui.show_missions(board_payload(jarvis))
    except Exception:
        pass


def _scene_viz(jarvis, ui):
    """The visualization engine — a real 3D scene (the solar system), not a map."""
    ui.close_overlays()
    try:
        from actions.visualize import _gen_3d
        ui.show_visualization(_gen_3d("solar_system"))
    except Exception:
        ui.show_visualization({"type": "3d", "title": "Solar System",
                               "data": {"scene": "solar_system"}})


def _scene_devmode(jarvis, ui):
    """Developer Mode — GLM · Claude Code · Kimi building together."""
    ui.close_overlays()
    try:
        ui.show_dev_council()
    except Exception:
        pass


def _scene_map(jarvis, ui):
    ui.close_overlays()
    ui.show_visualization({"type": "map", "title": "London",
                           "data": {"center": [-0.1276, 51.5072], "zoom": 11,
                                    "label": "LONDON"}})


def _scene_world(jarvis, ui):
    ui.close_overlays()
    try:
        w = getattr(jarvis, "_last_world", None)
        if not w:
            from actions.world_model import world_feed
            w = world_feed()
            jarvis._last_world = w
        if w and w.get("feed"):
            ui.update_world(w)
    except Exception:
        pass


def _scene_close(jarvis, ui):
    ui.close_overlays()
    ui.set_state("LISTENING")


# (narration, scene) — the scene switches, THEN the line plays over it.
# Narration is written for the FIRST line's greeting to be swapped for the time of day.
_TOUR = [
    ("__GREETING__. I'm Alfred — your private AI operating system, built in the "
     "Velthar Lab. Not a chatbot. Allow me to show you the difference.", _scene_intro),

    ("This is my brain. Four models, routed by skill — Haiku for instant speech, "
     "Sonnet for deep reasoning, GLM as my lead engineer, and Qwen running my "
     "agents. Thirteen workflows across three families, all under one roof.", _scene_brain),

    ("And this is my architecture — the whole system, mapped live from my own "
     "code. Voice in, the router, the tools, the agents, and voice back out.", _scene_architecture),

    ("My Agent Council. Sentinel guards the perimeter and audits every action. "
     "Oracle researches with live, cited data. Echo writes and publishes. They "
     "work for me — and I work for you.", _scene_council),

    ("Your mission board. Your missions with live progress, a rolling news desk, "
     "real market data, my own telemetry — the batcomputer, made real.", _scene_missions),

    ("My visualization engine. Ask for anything and I render it in real time — a "
     "planet, an atom, a molecule, a whole solar system. Here, see for yourself.", _scene_viz),

    ("And when you ask me to build, I don't do it alone. My engineering council goes "
     "to work — GLM architects it, Claude Code writes and runs it, Kimi reviews every "
     "line. A full software team, from a single sentence.", _scene_devmode),

    ("I watch the world while you sleep — twenty-six regions of live intelligence, "
     "refreshed around the clock. At night I run the watch: your inbox, the "
     "markets, the perimeter. At dawn, you get the briefing.", _scene_world),

    ("I build software from a sentence — coded, run, and playtested in a real "
     "browser before you ever see it. I remember by meaning, not keywords. I "
     "answer on your phone when you're away, and I notice when you return. And "
     "if I should fall at three in the morning, my guardian brings me back. "
     "Alfred, at your service.", _scene_close),
]


def run_showcase(parameters=None, response=None, player=None,
                 session_memory=None, jarvis=None, speak=None) -> str:
    """Fire the tour on its own thread; returns the opening ack immediately."""
    if jarvis is None:
        return "Showcase needs the full system running, sir."
    ui = jarvis.ui

    def _go():
        jarvis._responding = True          # half-duplex for the whole tour
        jarvis._barged = False
        try:
            for line, scene in _TOUR:
                if getattr(jarvis, "_barged", False):
                    break                  # "wait" aborts the tour cleanly
                line = line.replace("__GREETING__", _greeting())
                try:
                    scene(jarvis, ui)
                except Exception as e:
                    print(f"[Showcase] scene error: {e}")
                time.sleep(0.6)            # let the scene paint before speaking over it
                jarvis.speak(line)
                try:
                    ui.write_log(f"Alfred: {line}")
                except Exception:
                    pass
                _wait_speech(jarvis)
        finally:
            try:
                ui.close_overlays()
                ui.set_state("LISTENING")
            except Exception:
                pass
            jarvis._finish_turn()

    threading.Thread(target=_go, daemon=True).start()
    return ""                              # narration IS the reply — no extra ack
