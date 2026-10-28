"""
SIMULATION ROOM — "war-game this decision."

Opens a tab (the war-game board) and fills it live: the ADVOCATE (Qwen) builds the
strongest case FOR, the SKEPTIC (DeepSeek V4) attacks it, then the JUDGE (Sonnet)
weighs both and rules — go / no-go / go-with-conditions. Each stage streams into the
board as it lands; Alfred speaks the verdict. Costs pennies (two free NIM calls + one
Sonnet). Unless the ruling is a flat no-go, a follow-up mission is logged.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def _llm_json(prompt: str, system: str, tier: str, model: str | None = None,
              max_tokens: int = 600) -> dict:
    try:
        from core.llm_client import call_llm_text
        out = call_llm_text(prompt, system=system, tier=tier, model=model,
                            timeout=90, max_tokens=max_tokens) or ""
        m = re.search(r"\{.*\}", out, re.DOTALL)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def war_game(topic: str, jarvis=None, player=None) -> str:
    topic = (topic or "").strip().rstrip(".?!")
    if len(topic) < 5:
        return "War-game what decision, sir?"
    ui = (getattr(jarvis, "ui", None) if jarvis else None) or player
    sim_id = f"SR-{time.strftime('%Y%m%d')}-{int(time.time()) % 100:02d}A"
    started = time.time()

    def _show(data: dict):
        if ui and hasattr(ui, "show_visualization"):
            try:
                data.setdefault("sim_id", sim_id)
                data["elapsed"] = int(time.time() - started)
                ui.show_visualization({"type": "simroom", "title": "War Game", "data": data})
            except Exception:
                pass

    def _log(t):
        try:
            if ui: ui.write_log(t)
        except Exception:
            pass

    def _go():
        board = {"topic": topic, "status": "RUNNING",
                 "advocate": None, "skeptic": None, "judge": None}
        _show(board)                                      # open the tab immediately
        _log(f"SYS: ⚔️ SIMULATION ROOM — war-gaming: {topic}")

        adv_sys = ("You are the ADVOCATE in a war-game, arguing the strongest case FOR the "
                   "decision. STRICT JSON: {\"position\":\"one-line stance\","
                   "\"arguments\":[\"3-5 punchy points\"],\"evidence\":[\"2-3 supporting facts/observations\"],"
                   "\"confidence\":0-100}. No markdown.")
        skp_sys = ("You are the SKEPTIC in a war-game, attacking the decision — failure modes, "
                   "hidden costs, the most likely way it goes wrong. STRICT JSON: {\"position\":"
                   "\"one-line stance\",\"arguments\":[\"3-5 punchy points\"],\"evidence\":"
                   "[\"2-3 supporting facts/observations\"],\"confidence\":0-100}. No markdown.")
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(_llm_json, f"Decision under review: {topic}", adv_sys, "simple")
            fs = ex.submit(_llm_json, f"Decision under review: {topic}", skp_sys, "simple",
                           "deepseek-ai/deepseek-v4-flash")
            adv, skp = fa.result(), fs.result()
        board["advocate"] = adv or {"position": "(unavailable)"}
        board["skeptic"] = skp or {"position": "(unavailable)"}
        _show(board)                                      # fill both corners
        _log(f"[ADVOCATE — Qwen] {adv.get('position','')}")
        _log(f"[SKEPTIC — DeepSeek V4] {skp.get('position','')}")

        judge = _llm_json(
            f"Decision: {topic}\n\nADVOCATE ({adv.get('confidence','?')}% sure): "
            f"{adv.get('position','')} — {'; '.join(adv.get('arguments',[]))}\n\n"
            f"SKEPTIC ({skp.get('confidence','?')}% sure): {skp.get('position','')} — "
            f"{'; '.join(skp.get('arguments',[]))}\n\nRule on it.",
            "You are the JUDGE of a war-game. Weigh both cases and rule. STRICT JSON: "
            "{\"decision\":\"GO | NO-GO | GO WITH CONDITIONS\",\"weighting\":{\"advocate\":int,"
            "\"skeptic\":int},\"analysis\":\"2-3 sentences\",\"reasoning\":[\"2-4 key points\"],"
            "\"condition\":\"the single most important condition or risk to manage\","
            "\"mission_success\":0-100}. Weighting must sum to 100. No markdown.",
            "heavy", None, 700)
        board["judge"] = judge or {"decision": "UNDECIDED"}
        board["status"] = "COMPLETE"
        # cross-feature: surface any open case this decision touches
        try:
            from actions.casefiles import _load as _cload
            terms = {w for w in topic.lower().split() if len(w) > 3}
            rel = []
            for c in _cload().get("cases", {}).values():
                hay = (c.get("name", "") + " " +
                       " ".join(e.get("note", "") for e in c.get("evidence", []))).lower()
                if terms & set(hay.split()):
                    rel.append({"name": "Case: " + c["name"],
                                "id": f"{''.join(w[0] for w in c['name'].split()[:2]).upper()}-{int(c.get('opened',0))%1000:03d}"})
            board["related"] = rel[:3]
        except Exception:
            pass
        _show(board)                                      # final verdict on the board

        decision = (judge.get("decision") or "").upper()
        spoken = (f"Verdict, sir — {decision.lower() or 'undecided'}. "
                  f"{judge.get('analysis','')} "
                  + (f"The key condition: {judge.get('condition','')}." if judge.get('condition') else ""))
        _log(f"[VERDICT — Sonnet] {decision}")

        tail = ""
        if decision and "NO-GO" not in decision and "NO GO" not in decision:
            try:
                from actions.missions import add_mission
                add_mission(f"Act on war-game: {topic}")
                tail = " I've logged a mission to act on it, sir."
            except Exception:
                pass
        if jarvis is not None:
            jarvis._responding = True
            jarvis.speak(spoken + tail)
            jarvis._finish_turn()

    threading.Thread(target=_go, daemon=True).start()
    return ("The simulation room is open, sir — advocate and skeptic are at the table, "
            "verdict shortly.")
