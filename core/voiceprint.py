"""
VOICE-PRINT LOCK — the batcomputer's "authorized user: RECOGNIZED / DENIED" gate.

Not what you say — WHO says it. Alfred learns the owner's vocal fingerprint and, on
any sensitive action (post / spend / real-hands / delete), checks the confirming
utterance against it. A misheard command, the TV, or a stranger's voice → DENIED.

Deliberately dependency-light: an MFCC-statistics embedding in pure numpy (no torch,
no Ollama — nothing that overheats the M3 Air). Good enough to reject a clearly
different speaker as a SECOND factor on top of the spoken confirmation. It is not
bank-grade biometrics; a neural encoder could be dropped in later for that.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_STORE = _base() / "memory" / "voiceprint.json"
_SR = 16000               # Alfred's mic pipeline runs at 16 kHz mono


def _threshold() -> float:
    """Similarity (0..1) required to accept. Same voice ≈ 1, a stranger ≈ 0.
    Tunable via config 'voiceprint_threshold' (raise = stricter). Default conservative."""
    try:
        return float(json.loads((_base() / "config" / "api_keys.json")
                                .read_text()).get("voiceprint_threshold", 0.78))
    except Exception:
        return 0.78


# ── MFCC (numpy only) ────────────────────────────────────────────────────────
def _mel_filterbank(n_filters: int, n_fft: int, sr: int) -> np.ndarray:
    def hz2mel(f): return 2595.0 * np.log10(1 + f / 700.0)
    def mel2hz(m): return 700.0 * (10 ** (m / 2595.0) - 1)
    lo, hi = hz2mel(0), hz2mel(sr / 2)
    pts = mel2hz(np.linspace(lo, hi, n_filters + 2))
    bins = np.floor((n_fft + 1) * pts / sr).astype(int)
    fb = np.zeros((n_filters, n_fft // 2 + 1))
    for m in range(1, n_filters + 1):
        l, c, r = bins[m - 1], bins[m], bins[m + 1]
        for k in range(l, c):
            if c > l: fb[m - 1, k] = (k - l) / (c - l)
        for k in range(c, r):
            if r > c: fb[m - 1, k] = (r - k) / (r - c)
    return fb


def _mfcc(sig: np.ndarray, sr: int = _SR, n_mfcc: int = 13) -> np.ndarray:
    sig = np.asarray(sig, dtype=np.float32).flatten()
    if sig.size < sr // 2:                                # under 0.5 s → nothing usable
        return np.zeros((0, n_mfcc), np.float32)
    sig = sig / (np.max(np.abs(sig)) + 1e-9)
    sig = np.append(sig[0], sig[1:] - 0.97 * sig[:-1])    # pre-emphasis
    win, hop, n_fft = int(0.025 * sr), int(0.010 * sr), 512
    n_frames = 1 + (len(sig) - win) // hop
    if n_frames < 3:
        return np.zeros((0, n_mfcc), np.float32)
    frames = np.stack([sig[i * hop:i * hop + win] for i in range(n_frames)])
    frames *= np.hamming(win)
    mag = np.abs(np.fft.rfft(frames, n_fft))
    pow_spec = (mag ** 2) / n_fft
    fb = _mel_filterbank(26, n_fft, sr)
    energy = np.dot(pow_spec, fb.T)
    energy = np.where(energy == 0, 1e-10, energy)
    log_e = np.log(energy)
    # DCT-II → keep first n_mfcc
    K = log_e.shape[1]
    dct = np.cos(np.pi / K * (np.arange(K) + 0.5)[None, :] * np.arange(n_mfcc)[:, None])
    mfcc = log_e @ dct.T
    # voiced-frame gate: drop the quietest 20 % (silence/breath)
    frame_e = np.log(pow_spec.sum(1) + 1e-9)
    keep = frame_e > np.percentile(frame_e, 20)
    return mfcc[keep] if keep.sum() >= 3 else mfcc


def _embed(sig: np.ndarray) -> np.ndarray | None:
    """Speaker embedding: mean+std of MFCCs 1..12 and their deltas. Coefficient 0
    (overall energy = volume/mic distance, NOT speaker identity) is dropped so the
    print doesn't change when you speak louder or nearer the mic."""
    m = _mfcc(sig)
    if m.shape[0] < 3:
        return None
    m = m[:, 1:]                                         # drop c0 (energy)
    d = np.diff(m, axis=0)
    emb = np.concatenate([m.mean(0), m.std(0), d.mean(0), d.std(0)])
    return emb.astype(np.float32)


def _load() -> dict:
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8"))
        d["ref"] = np.array(d["ref"], np.float32)
        return d
    except Exception:
        return {}


def is_enrolled() -> bool:
    return _STORE.exists()


# ── enrollment / verification ────────────────────────────────────────────────
def enroll(clips: list[np.ndarray]) -> str:
    """Learn the owner's voiceprint from one or more short clips."""
    embs = [e for e in (_embed(c) for c in clips) if e is not None]
    if len(embs) < 1:
        return "I didn't catch enough of your voice, sir — try again and speak for a few seconds."
    E = np.stack(embs)
    ref = E.mean(0)                                      # enrolled centroid
    # auto-calibrate the acceptance radius from the enrollment's own spread, with a
    # floor so a few near-identical clips don't make the gate impossibly strict.
    spread = float(np.mean([np.linalg.norm(e - ref) for e in embs])) if len(embs) > 1 else 0.0
    tau = max(spread, 3.0)                                # distance scale (MFCC units)
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps({"ref": ref.tolist(), "tau": tau, "n": len(embs),
                                  "ts": time.time()}), encoding="utf-8")
    return "Voiceprint enrolled, sir — I'll know your voice from now on."


def verify(sig: np.ndarray) -> tuple[bool, float]:
    """Return (accepted, similarity 0..1) for an utterance vs the enrolled owner.
    Similarity falls off with distance from the owner centroid in units of the
    owner's own natural spread — same voice ≈ near 1, a different voice ≈ near 0."""
    ref = _load()
    if not ref:
        return True, 1.0                                 # not enrolled → gate is open
    e = _embed(sig)
    if e is None:
        return False, 0.0
    d = float(np.linalg.norm(e - ref["ref"]))
    tau = float(ref.get("tau", 3.0))
    sim = float(np.exp(-(d / (3.2 * tau)) ** 2))         # 1 at centroid → 0 far away
    return sim >= _threshold(), sim


def unenroll() -> str:
    try:
        _STORE.unlink()
        return "Voiceprint wiped, sir — the lock is off."
    except Exception:
        return "There was no voiceprint on file, sir."
