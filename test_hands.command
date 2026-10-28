#!/bin/bash
# HAND-TRACKING GROUND TRUTH — run this to SEE the tracking working (or not).
# A window opens with your camera; your hand gets a dot, pinch turns it green.
# If this works but Alfred's in-app hands don't, the bug is app-side — tell Claude.
cd "$HOME/AlfredV2" || exit 1
.venv/bin/python - <<'PY'
import cv2, time
import mediapipe as mp
from mediapipe.tasks.python import vision, BaseOptions

opts = vision.HandLandmarkerOptions(
    base_options=BaseOptions(model_asset_path="models/hand_landmarker.task"),
    num_hands=2, running_mode=vision.RunningMode.VIDEO,
    min_hand_detection_confidence=0.5, min_tracking_confidence=0.4)
lm = vision.HandLandmarker.create_from_options(opts)
cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("❌ CAMERA FAILED TO OPEN — that's the bug. Check Camera permission for Terminal."); exit(1)
print("✅ camera open — window opening; pinch thumb+index, press Q to quit")
ts = 0
def dist(a,b): return ((a.x-b.x)**2+(a.y-b.y)**2)**0.5
while True:
    ok, frame = cap.read()
    if not ok: print("❌ no frames from camera"); break
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    ts += 33
    res = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
    hands = getattr(res, "hand_landmarks", None) or []
    h_, w_ = frame.shape[:2]
    status = "NO HAND — show your hand to the camera"
    for h in hands:
        pinch = dist(h[4], h[8]) / max(dist(h[0], h[9]), 1e-4) < 0.70
        col = (0,255,0) if pinch else (255,200,0)
        cv2.circle(frame, (int(h[8].x*w_), int(h[8].y*h_)), 18, col, 3)
        status = "PINCHED ✊  (drag would move the tab)" if pinch else "HAND TRACKED ✋  (pinch to grab)"
    cv2.putText(frame, status, (18, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (92,224,200), 2)
    cv2.imshow("Alfred hand test — Q to quit", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'): break
cap.release(); cv2.destroyAllWindows()
print("test finished")
PY
