"""
Semantic memory — Alfred's long-term recall that actually scales.

The v1 recall was keyword overlap: it misses "the neon jump game" ↔ "geometry dash"
because they share no words. This layer embeds every memory (episodes, night-log
events, Obsidian vault notes) with NVIDIA NIM embeddings and recalls by MEANING via
cosine similarity — so he finds the right memory even when you phrase it differently.

Design goals:
  • NIM only — cloud embeddings (nvidia/nv-embedqa-e5-v5, 1024-dim). Zero local CPU.
  • Cheap — one tiny embed call when a memory is stored; search is in-memory numpy.
  • Safe — every failure (offline, API down) degrades silently to the keyword layer.
  • Async-friendly — indexing never blocks a turn.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_STORE = _base() / "memory" / "semantic.jsonl"
_MODEL = "nvidia/nv-embedqa-e5-v5"          # 1024-dim, asymmetric (query vs passage)
_URL   = "https://integrate.api.nvidia.com/v1/embeddings"

_LOCK  = threading.Lock()
_CACHE: dict = {"mtime": 0.0, "mat": None, "meta": []}   # in-memory vector matrix


def _mid(text: str, kind: str) -> str:
    """Stable id — hashlib, NOT builtin hash() which is per-process randomised
    (that double-indexed the store across runs)."""
    import hashlib
    return f"{kind}:{hashlib.md5(text.strip().lower().encode('utf-8')).hexdigest()[:16]}"


def _key() -> str:
    try:
        return json.loads((_base() / "config" / "api_keys.json").read_text(encoding="utf-8")).get("llm_api_key", "")
    except Exception:
        return ""


def _embed(texts: list[str], input_type: str = "passage") -> list[list[float]] | None:
    """Embed a batch via NIM. input_type: 'passage' to store, 'query' to search."""
    key = _key()
    if not key or not texts:
        return None
    try:
        import requests
        r = requests.post(_URL, timeout=20,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"input": texts, "model": _MODEL, "input_type": input_type,
                                "truncate": "END"})
        if r.status_code != 200:
            return None
        return [d["embedding"] for d in r.json().get("data", [])]
    except Exception:
        return None


def _seen_ids() -> set:
    ids = set()
    try:
        for line in _STORE.read_text(encoding="utf-8").splitlines():
            try:
                ids.add(json.loads(line)["id"])
            except Exception:
                pass
    except Exception:
        pass
    return ids


def index(text: str, kind: str = "episode", mid: str | None = None, meta: dict | None = None) -> bool:
    """Embed one memory and append it to the store. Returns True on success."""
    text = (text or "").strip()
    if len(text) < 4:
        return False
    mid = mid or _mid(text, kind)
    if mid in _seen_ids():
        return True                              # already indexed
    vec = _embed([text], "passage")
    if not vec:
        return False
    rec = {"id": mid, "kind": kind, "text": text[:500], "ts": time.time(),
           "vec": [round(x, 5) for x in vec[0]], **({"meta": meta} if meta else {})}
    with _LOCK:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        with _STORE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def index_async(text: str, kind: str = "episode", meta: dict | None = None) -> None:
    threading.Thread(target=index, args=(text, kind), kwargs={"meta": meta}, daemon=True).start()


def index_many(records: list[dict], batch: int = 50) -> int:
    """Batch-embed + store many memories at once. records: [{text, kind, meta?}].
    NIM embeds a whole batch per call, so 460 memories = ~10 calls, not 460."""
    seen = _seen_ids()
    pending, rows = [], []
    for r in records:
        text = (r.get("text") or "").strip()
        if len(text) < 4:
            continue
        mid = _mid(text, r.get("kind","episode"))
        if mid in seen:
            continue
        seen.add(mid)
        pending.append(text)
        rows.append({"id": mid, "kind": r.get("kind", "episode"), "text": text[:500],
                     "meta": r.get("meta")})
    if not pending:
        return 0
    written = 0
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(pending), batch):
        chunk_txt = pending[i:i + batch]
        chunk_row = rows[i:i + batch]
        vecs = _embed(chunk_txt, "passage")
        if not vecs or len(vecs) != len(chunk_txt):
            continue
        with _LOCK:
            with _STORE.open("a", encoding="utf-8") as f:
                for row, v in zip(chunk_row, vecs):
                    rec = {"id": row["id"], "kind": row["kind"], "text": row["text"],
                           "ts": time.time(), "vec": [round(x, 5) for x in v]}
                    if row.get("meta"):
                        rec["meta"] = row["meta"]
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
    return written


def _load_matrix():
    """Load (and cache) the vector matrix + metadata. Rebuilt only when the file changes."""
    try:
        mtime = _STORE.stat().st_mtime
    except Exception:
        return None, []
    with _LOCK:
        if _CACHE["mat"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["mat"], _CACHE["meta"]
        vecs, meta = [], []
        try:
            for line in _STORE.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    vecs.append(r["vec"])
                    meta.append({"kind": r.get("kind"), "text": r.get("text"),
                                 "ts": r.get("ts"), "meta": r.get("meta")})
                except Exception:
                    pass
        except Exception:
            return None, []
        if not vecs:
            return None, []
        mat = np.asarray(vecs, dtype=np.float32)
        mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)   # pre-normalise
        _CACHE.update({"mtime": mtime, "mat": mat, "meta": meta})
        return mat, meta


def search(query: str, k: int = 4, kinds: list[str] | None = None, min_sim: float = 0.30) -> list[dict]:
    """Semantic recall by meaning. Returns [{kind, text, ts, score}], best first."""
    mat, meta = _load_matrix()
    if mat is None:
        return []
    qv = _embed([query], "query")
    if not qv:
        return []
    q = np.asarray(qv[0], dtype=np.float32)
    q /= (np.linalg.norm(q) + 1e-8)
    sims = mat @ q                                # cosine (both normalised)
    order = np.argsort(-sims)
    out = []
    for i in order:
        if kinds and meta[i]["kind"] not in kinds:
            continue
        s = float(sims[i])
        if s < min_sim:
            break
        out.append({**meta[i], "score": round(s, 3)})
        if len(out) >= k:
            break
    return out


def backfill(limit: int = 400) -> dict:
    """Batch-index anything not yet embedded: episodes, night-log events, vault notes."""
    records: list[dict] = []

    # episodes
    try:
        eps = json.loads((_base() / "memory" / "episodes.json").read_text(encoding="utf-8"))
        for e in eps[-limit:]:
            u = (e.get("user") or "").strip()
            if u:
                records.append({"text": u, "kind": "episode"})
    except Exception:
        pass

    # night-log events (recent days)
    try:
        nd = _base() / "workspace" / "nightlog"
        for fp in (sorted(nd.glob("*.jsonl"))[-5:] if nd.exists() else []):
            for line in fp.read_text(encoding="utf-8").splitlines()[-80:]:
                try:
                    r = json.loads(line)
                    if r.get("kind") in ("mail", "security", "x", "world") and r.get("text"):
                        records.append({"text": r["text"], "kind": "nightlog"})
                except Exception:
                    pass
    except Exception:
        pass

    # Obsidian vault note titles/snippets
    try:
        cfg = json.loads((_base() / "config" / "api_keys.json").read_text(encoding="utf-8"))
        vault = cfg.get("obsidian_vault", "")
        if vault and Path(vault).expanduser().exists():
            for md in list(Path(vault).expanduser().rglob("*.md"))[:limit]:
                try:
                    snippet = (md.stem + " — " + md.read_text(encoding="utf-8")[:300]).strip()
                    records.append({"text": snippet, "kind": "vault", "meta": {"file": md.name}})
                except Exception:
                    pass
    except Exception:
        pass

    n = index_many(records)
    return {"indexed_now": n, "total": stats()["indexed"]}


def backfill_async() -> None:
    threading.Thread(target=backfill, daemon=True).start()


def stats() -> dict:
    try:
        n = sum(1 for _ in _STORE.read_text(encoding="utf-8").splitlines())
    except Exception:
        n = 0
    return {"indexed": n, "model": _MODEL}
