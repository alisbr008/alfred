#!/bin/bash
# Triggers the macOS Camera permission prompt for Terminal (and thus Alfred).
# macOS won't let you add Terminal to the Camera list manually — an app must
# REQUEST the camera. This opens it via the exact code path Alfred uses, so the
# grant applies to him. First run → macOS asks → click ALLOW.
cd "$HOME/AlfredV2" || exit 1
echo "🎥 Requesting camera access — if macOS shows a prompt, click ALLOW…"
echo ""
.venv/bin/python - <<'PY'
import cv2, time
cap = cv2.VideoCapture(0)
time.sleep(1.5)
ok = cap.isOpened()
frame_ok = False
if ok:
    for _ in range(5):
        cap.read()
    frame_ok, _ = cap.read()
cap.release()
if frame_ok:
    print("✅ CAMERA WORKS — Terminal is now authorised. Alfred can see you.")
else:
    print("⚠️  No frame captured yet.")
    print("   • If a macOS prompt appeared, click ALLOW, then run this again.")
    print("   • If Terminal is now in System Settings → Privacy → Camera, toggle it ON.")
PY
echo ""
echo "When it says CAMERA WORKS, relaunch Alfred (Cmd+Shift+A) and say 'see me'."
echo "Press any key to close…"; read -n 1
