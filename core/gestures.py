"""
GESTURE MODE — control Alfred's tabs with your bare hands. No mouse, no trackpad.

"Gesture mode" → the webcam tracks one hand via MediaPipe (19 ms/frame on the M3):
  • PINCH (thumb+index together) and MOVE  → drag / pan the open tab (map, viz, board)
  • open your hand                          → release
  • two-finger pinch spread (later)         → zoom
  • "gesture mode off" / closing the tab    → stops, camera released

Heat discipline (the Air rule): runs ONLY while explicitly on, ~12 fps with the
lightweight model, auto-stops after 3 idle minutes. Nothing polls when off.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import queue

_BASE = Path(__file__).resolve().parent.parent
_MODEL = _BASE / "models" / "hand_landmarker.task"

_state = {"on": False, "stop": None, "thread": None, "last_seen": 0.0}
_frames: "queue.Queue" = queue.Queue(maxsize=2)   # latest browser webcam frames (BGR)


def is_on() -> bool:
    return bool(_state["on"])


def push_frame(data_url: str) -> None:
    """Bridge callback: a webcam frame from the BROWSER (which already holds camera
    permission). Decoded to BGR and queued for the tracker — this is how we bypass
    OpenCV's blocked direct-camera access on macOS."""
    try:
        import base64, numpy as np, cv2
        b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
        buf = np.frombuffer(base64.b64decode(b64), np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return
        if _frames.full():
            try: _frames.get_nowait()
            except Exception: pass
        _frames.put_nowait(img)
    except Exception:
        pass


def _dist(a, b) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def _loop(jarvis, stop: threading.Event) -> None:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import vision, BaseOptions

    ui = jarvis.ui

    def _log(msg):
        print(f"[Gestures] {msg}")
        try: ui.write_log(f"SYS: 🖐 {msg}")
        except Exception: pass
        try: ui._call("gestureStatus", str(msg))       # live state on the chip
        except Exception: pass

    try:
        opts = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(_MODEL)),
            num_hands=2, running_mode=vision.RunningMode.VIDEO,
            min_hand_detection_confidence=0.5, min_tracking_confidence=0.4)
        lm = vision.HandLandmarker.create_from_options(opts)
    except Exception as e:
        _log(f"init failed: {e}")
        _state["on"] = False
        return

    # show the chip + ask the BROWSER to start streaming its (permitted) webcam
    try:
        ui._call("setGesture", True)
        ui._call("gestureStartCamera")
    except Exception:
        pass

    # Prefer BROWSER frames (getUserMedia already has permission). Wait a few
    # seconds; if none arrive, fall back to OpenCV direct capture.
    while not _frames.empty():                             # clear any stale frames
        try: _frames.get_nowait()
        except Exception: break
    cap = None
    source = None
    _log("engaging camera…")
    t_wait = time.time()
    while time.time() - t_wait < 5 and not stop.is_set():
        if not _frames.empty():
            source = "browser"
            break
        time.sleep(0.1)
    if source != "browser":
        try:                                              # fallback: OpenCV direct
            import json
            cam_idx = int(json.loads((_BASE / "config" / "api_keys.json")
                                     .read_text()).get("camera_index", 0))
        except Exception:
            cam_idx = 0
        cap = cv2.VideoCapture(cam_idx)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 480); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 270)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if cap.isOpened() and cap.read()[0]:
            source = "opencv"
        else:
            try: cap.release()
            except Exception: pass
            _log("camera unavailable — grant camera access, then reopen the tab")
            try: ui._call("gestureStopCamera")
            except Exception: pass
            _state["on"] = False
            return
    _log(f"camera live ({source}) — show your hand, pinch to grab")

    GAIN = 620                         # normalized hand motion → px of drag (calm, ~1:1 feel)
    DEAD = 0.006                       # deadzone: ignore sub-jitter motion (kills drift)
    start_t = time.time()
    idle_since = start_t
    seen_hand = False
    pinch_state: dict = {}             # per-hand hysteresis: enter <0.62, exit >0.80
    pinch_run = 0                      # consecutive pinch frames (debounce false grabs)
    px = py = 0.0                      # drag anchor (primary hand)
    zoom_prev = 0.0                    # two-hand zoom: last frame's wrist span
    dz = None                          # smoothed wrist span
    palm_prev = 0.0                    # one-hand zoom: last frame's palm size
    palm_s = None                      # smoothed palm size
    dragging = False
    sx = sy = None                     # adaptively-smoothed cursor
    prev_rx = prev_ry = None
    hand_gone_t = 0.0                  # when the hand left frame (for auto-reset)
    reset_done = False                 # snapped the view back to normal already?

    def _pinched(h, key) -> bool:
        scale = max(_dist(h[0], h[9]), 1e-4)                  # wrist→mid-MCP = hand size
        ratio = _dist(h[4], h[8]) / scale
        prev = pinch_state.get(key, False)
        now = ratio < (0.80 if prev else 0.62)                # tighter: relaxed hand ≠ grab
        pinch_state[key] = now
        return now

    def _openness(h) -> float:
        """How spread the hand is: mean fingertip distance from the wrist, scaled by
        hand size (distance-invariant). Open/spread palm ≈ high, fist ≈ low."""
        scale = max(_dist(h[0], h[9]), 1e-4)
        return sum(_dist(h[t], h[0]) for t in (8, 12, 16, 20)) / (4 * scale)

    while not stop.is_set():
        if source == "browser":
            try:
                frame = _frames.get(timeout=0.5)          # freshest browser webcam frame
            except Exception:
                continue
        else:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.03)
                continue
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame)
        ts = int((time.time() - start_t) * 1000)              # real monotonic ms
        try:
            res = lm.detect_for_video(img, ts)
        except Exception:
            res = None

        hands = getattr(res, "hand_landmarks", None) or []
        if hands:
            if not seen_hand:
                seen_hand = True
                _log("hands live — move your palms to control; drop them to reset")
            idle_since = time.time()
            hand_gone_t = 0.0; reset_done = False          # hand's back
            h0 = hands[0]

            # ── ADAPTIVE smoothing (1€-style): still hand → heavy smoothing (no
            # jitter, no drift); fast hand → light smoothing (no lag). Fixes both.
            rx, ry = 1.0 - h0[8].x, h0[8].y
            if prev_rx is None:
                prev_rx, prev_ry = rx, ry
            speed = ((rx - prev_rx) ** 2 + (ry - prev_ry) ** 2) ** 0.5
            prev_rx, prev_ry = rx, ry
            A = min(0.85, 0.28 + speed * 14.0)                # 0.28 still → 0.85 fast
            sx = rx if sx is None else A * rx + (1 - A) * sx
            sy = ry if sy is None else A * ry + (1 - A) * sy

            p0 = _pinched(h0, 0)
            pinch_run = pinch_run + 1 if p0 else 0
            grab = pinch_run >= 2                              # need 2 frames to engage
            moving = speed > 0.0045                            # register slow deliberate moves too

            mode, a, b = 0, 0, 0                               # 0 idle · 1 drag · 2 zoom
            if len(hands) > 1:
                # TWO PALMS → TRACKPAD PINCH-ZOOM. The distance between your two hands
                # is the zoom: spread them APART → zoom IN, bring them TOGETHER → zoom
                # OUT — exactly like a two-finger pinch on the Mac trackpad. Only fires
                # while your hands are actually moving, so still palms never drift.
                d = _dist(h0[8], hands[1][8])                  # index-tip to index-tip
                dz = d if dz is None else 0.6 * d + 0.4 * dz   # light smoothing
                if zoom_prev == 0:
                    zoom_prev = dz                             # anchor on first frame
                elif moving:
                    ratio = dz / zoom_prev                     # spread (bigger) → >1 = zoom IN
                    if abs(ratio - 1.0) > 0.012:               # deadzone
                        mode, a = 2, round(1.0 + (ratio - 1.0) * 1.9, 4)
                    zoom_prev = dz
                else:
                    zoom_prev = dz                             # keep anchor fresh while still
                dragging = False
                p0 = True                                      # both-hands cursor = active
            elif grab:                                         # ONE-HAND PINCH → DRAG
                zoom_prev = 0.0; dz = None
                if not dragging:
                    dragging, px, py = True, sx, sy            # anchor on grab
                else:
                    rdx, rdy = sx - px, sy - py                # deadzone on RAW delta
                    mdx = 0.0 if abs(rdx) < DEAD else rdx - (DEAD if rdx > 0 else -DEAD)
                    mdy = 0.0 if abs(rdy) < DEAD else rdy - (DEAD if rdy > 0 else -DEAD)
                    if mdx or mdy:
                        mode, a, b = 1, int(mdx * GAIN), int(mdy * GAIN)
                        px, py = sx, sy
            else:
                # ONE hand, not pinched → just the cursor follows (no zoom).
                dragging = False; zoom_prev = 0.0; dz = None
                p0 = False

            try:
                ui._call("gestureFrame", round(sx, 4), round(sy, 4), bool(p0), mode, a, b)
            except Exception:
                pass
        else:
            pinch_run = 0
            # AUTO-RESET: drop your hands and the view snaps back to normal after ~1s,
            # so a scaled/shifted overlay can always be recovered without a mouse.
            if hand_gone_t == 0.0:
                hand_gone_t = time.time()
            elif not reset_done and time.time() - hand_gone_t > 1.0:
                try:
                    ui._call("gestureReset")
                    ui._call("gestureStatus", "view reset — move your palms to control")
                except Exception:
                    pass
                reset_done = True
            if time.time() - idle_since > 180:                 # 3 min no hand → stand down
                break
        time.sleep(0.005)                                      # let camera/MediaPipe pace it

    if cap is not None:
        try: cap.release()
        except Exception: pass
    try:
        ui._call("setGesture", False)
        ui._call("gestureStopCamera")                       # release the browser webcam
    except Exception:
        pass
    _state["on"] = False
    print("[Gestures] stopped")


def start(jarvis, quiet: bool = False) -> str:
    if _state["on"]:
        return "" if quiet else "Gesture control is already on, sir."
    if not _MODEL.exists():
        return "" if quiet else "The hand-tracking model is missing, sir."
    # FAST PATH: run MediaPipe inside the page. Nothing crosses the Qt bridge per
    # frame, so there's no marshalling cost and no GUI-thread contention — this is
    # what makes it smooth. Python only flips it on; the browser does the rest.
    _state["on"] = True
    _state["mode"] = "browser"
    try:
        jarvis.ui._call("setGesture", True)
        jarvis.ui._call("gestureEngineStart")
    except Exception:
        pass
    if quiet:
        return ""
    return ("Gesture control engaged, sir — pinch to grab and drag, two palms to zoom, "
            "drop your hands to reset.")


def start_python_fallback(jarvis, quiet: bool = True) -> str:
    """Only used if the in-page engine can't load (no CDN / WASM blocked)."""
    if _state.get("mode") == "python" and _state["on"]:
        return ""
    stop_ev = threading.Event()
    _state.update({"on": True, "stop": stop_ev, "mode": "python"})
    t = threading.Thread(target=_loop, args=(jarvis, stop_ev), daemon=True)
    _state["thread"] = t
    t.start()
    if quiet:
        return ""
    return ("Gesture control engaged, sir — pinch your thumb and finger to grab the view, "
            "move your hand to drag it, open your hand to release. Say gesture off when done.")


def stop(quiet: bool = False, jarvis=None) -> str:
    if not _state["on"]:
        return "" if quiet else "Gesture control isn't running, sir."
    try:
        if _state.get("stop"):
            _state["stop"].set()                       # python fallback loop
    except Exception:
        pass
    if jarvis is not None:
        try:
            jarvis.ui._call("gestureEngineStop")       # in-page engine
            jarvis.ui._call("setGesture", False)
        except Exception:
            pass
    _state["on"] = False
    return "" if quiet else "Hands off, sir — gesture control disengaged."
