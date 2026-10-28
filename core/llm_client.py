"""
LLM router client for Alfred.

Supports two backends — selected via  "llm_provider"  in config/api_keys.json:

  "llm_provider": "ollama"   (default)
        Uses Ollama's native /api/chat endpoint.
        Download: https://ollama.com
        Default port: 11434

  "llm_provider": "openai"
        Uses any OpenAI-compatible server: LM Studio, Jan, LocalAI,
        llama.cpp server, vLLM, etc.
        LM Studio download: https://lmstudio.ai   (default port: 1234)
        Set  "llm_url": "http://localhost:1234"  in config.
        Note: tool-calling support depends on the model; use a model that
        supports function/tool calls (e.g. Qwen2.5, Llama-3.1, Mistral).
"""
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Generator

import requests

# Matches a sentence boundary: [.!?] followed by whitespace, or a blank line.
# Avoids splitting on decimals (3.5) because those have no space after the dot.
_SENT_END = re.compile(r'(?<=[.!?])\s+|(?<=\n)\s*\n')

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

_DEFAULTS = {
    "llm_url":      "http://localhost:11434",
    "llm_model":    "llama3.2",
    "llm_provider": "ollama",   # "ollama" | "openai"
}


def get_llm_provider() -> str:
    """Returns 'ollama' or 'openai' (covers LM Studio, LocalAI, Jan, etc.)."""
    raw = _load_config().get("llm_provider", "ollama").strip().lower()
    return "openai" if raw in ("openai", "lmstudio", "localai", "jan", "llamacpp") else "ollama"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _openai_headers() -> dict:
    """
    Auth headers for OpenAI-compatible cloud providers (NVIDIA NIM, Gemini,
    OpenRouter, etc.) that require a Bearer key. Reads "llm_api_key" from
    config/api_keys.json. Returns {} for keyless local servers (LM Studio).
    """
    key = _load_config().get("llm_api_key", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _tier(tier: str = "main") -> tuple[str, str, str]:
    """
    Router: resolve (base_url, model, api_key) for a tier.
      simple → Qwen3-Next on NVIDIA (free, fast)        — trivial turns / fallback
      main   → Claude Haiku 4.5 (fast, cheap, reliable) — the conversational brain
      heavy  → Claude Sonnet 4.6 (top reasoning)        — research/content/coding
    """
    cfg   = _load_config()
    tiers = cfg.get("llm_tiers", {})
    t     = tiers.get(tier) or tiers.get("main") or {}
    url   = (t.get("url") or cfg.get("llm_url", _DEFAULTS["llm_url"])).rstrip("/")
    model = t.get("model") or cfg.get("llm_model", _DEFAULTS["llm_model"])
    if "anthropic.com" in url:
        key = cfg.get("anthropic_key", "")
    elif "z.ai" in url or "bigmodel" in url:          # Zhipu / GLM native endpoint
        key = cfg.get("zai_key", "")
    else:
        key = cfg.get("llm_api_key", "")              # NVIDIA NIM
    return url, model, key.strip()


def _chat_endpoint(url: str) -> str:
    """Completions URL per provider. Zhipu's base already carries its /api/paas/v4
    version segment, so it must NOT get an extra /v1."""
    if "z.ai" in url or "bigmodel" in url:
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


# ── self-telemetry: API burn / calls per model (near-zero cost: two dict writes
# per API call + one tiny JSON write; nothing polls, nothing loops) ───────────
_TM_LOCK  = threading.Lock()
_TM_FILE  = None   # resolved lazily under workspace/

# $ per 1M tokens (input, output). NIM tiers are free.
_PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}


def _tm_price(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for k, p in _PRICES.items():
        if k in m:
            return p
    return (0.0, 0.0)


def _tm_record(model: str, usage: dict | None, est_in: int = 0, est_out: int = 0) -> None:
    """Count a call. Uses the API's usage block when present, else estimates."""
    global _TM_FILE
    try:
        tin  = int((usage or {}).get("prompt_tokens") or est_in)
        tout = int((usage or {}).get("completion_tokens") or est_out)
        pi, po = _tm_price(model)
        cost = tin / 1e6 * pi + tout / 1e6 * po
        import datetime as _dt
        day = f"{_dt.date.today()}"
        with _TM_LOCK:
            if _TM_FILE is None:
                _TM_FILE = get_base_dir() / "workspace" / "telemetry.json"
                _TM_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(_TM_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            d = data.setdefault(day, {})
            m = d.setdefault(model, {"calls": 0, "tin": 0, "tout": 0, "cost": 0.0})
            m["calls"] += 1; m["tin"] += tin; m["tout"] += tout
            m["cost"] = round(m["cost"] + cost, 6)
            for k in [k for k in data if k not in (day,)][:-7]:   # keep ~a week
                data.pop(k, None)
            _TM_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


def telemetry_snapshot() -> dict:
    """Today's burn: total $ + per-model calls. Read-only, cheap."""
    try:
        import datetime as _dt
        f = get_base_dir() / "workspace" / "telemetry.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        today = data.get(f"{_dt.date.today()}", {})
        cost = sum(m.get("cost", 0.0) for m in today.values())
        calls = {k.split("/")[-1]: v.get("calls", 0) for k, v in today.items()}
        return {"cost": round(cost, 4), "calls": calls, "models": len(today)}
    except Exception:
        return {"cost": 0.0, "calls": {}, "models": 0}


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _openai_extra(model: str) -> dict:
    """
    Per-model extra request params for cloud providers.

    DeepSeek V4 defaults to a slow internal 'thinking' phase that adds seconds
    of latency before the first spoken word — disable it for a voice assistant.
    """
    if "deepseek" in (model or "").lower():
        return {"chat_template_kwargs": {"thinking": False}}
    return {}


def _post_retry(endpoint: str, payload: dict, headers: dict, timeout: int,
                stream: bool = False, retries: int = 2):
    """
    POST with a SHORT backoff on 429/503. Kept small on purpose: for a voice
    assistant, falling through to the free-tier fallback fast beats stalling the
    conversation for 10+ seconds while a rate-limited tier is retried.
    """
    delay = 0.6
    resp = None
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.post(endpoint, json=payload, headers=headers,
                                 timeout=timeout, stream=stream)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                print(f"[LLM] {type(e).__name__} — retrying in {delay:.0f}s…")
                time.sleep(delay); delay *= 2
                continue
            raise
        if resp.status_code in (429, 503) and attempt < retries - 1:
            if stream:
                resp.close()
            print(f"[LLM] {resp.status_code} rate-limited — retrying in {delay:.0f}s…")
            time.sleep(delay)
            delay *= 2
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp


def _to_openai_messages(messages: list) -> list:
    """
    Normalise internal (Ollama-style) messages to strict OpenAI format so cloud
    endpoints (NVIDIA NIM, etc.) accept replayed tool-call history.

      • assistant tool_calls → ensure  id, "type": "function",
        and  function.arguments  as a JSON *string* (not a dict).
      • tool messages → ensure a matching "tool_call_id" is present.

    Local servers (Ollama/LM Studio) are lenient, so this conversion is only
    applied on the OpenAI-compatible send path.
    """
    out: list = []
    pending_ids: list[str] = []
    counter = 0
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            new_tcs = []
            pending_ids = []
            for tc in msg["tool_calls"]:
                fn   = tc.get("function", {}) or {}
                args = fn.get("arguments", {})
                if not isinstance(args, str):
                    try:
                        args = json.dumps(args)
                    except Exception:
                        args = "{}"
                tc_id = tc.get("id") or f"call_{counter}"
                counter += 1
                pending_ids.append(tc_id)
                new_tcs.append({
                    "id":       tc_id,
                    "type":     "function",
                    "function": {"name": fn.get("name", ""), "arguments": args},
                })
            out.append({
                "role":       "assistant",
                "content":    msg.get("content") or "",
                "tool_calls": new_tcs,
            })
        elif role == "tool":
            m = dict(msg)
            if not m.get("tool_call_id") and pending_ids:
                m["tool_call_id"] = pending_ids.pop(0)
            out.append(m)
        else:
            out.append(msg)
    return out


def ensure_ollama_running(timeout: int = 15) -> bool:
    """
    For Ollama: ping /api/tags; auto-launch 'ollama serve' if not running.
    For OpenAI-compatible providers: just ping /v1/models (server must be started manually).
    Returns True if the LLM server is reachable.
    """
    url, _   = get_llm_settings()
    provider = get_llm_provider()

    if provider == "openai":
        # OpenAI-compatible servers (LM Studio, LocalAI, etc.) must be started
        # by the user — we just check if they're reachable.
        health = f"{url}/v1/models"
        try:
            ok = requests.get(health, timeout=5, headers=_openai_headers()).status_code == 200
            if ok:
                print(f"[LLM] OpenAI-compatible server reachable at {url}")
            else:
                print(f"[LLM] Server at {url} returned non-200.  Is it running?")
            return ok
        except Exception as e:
            print(
                f"[LLM] Cannot reach OpenAI-compatible server at {url}.\n"
                "      Make sure LM Studio / LocalAI / Jan is running and the server is started."
            )
            return False

    # ── Ollama ──────────────────────────────────────────────────────────────
    health = f"{url}/api/tags"

    def _is_up() -> bool:
        try:
            return requests.get(health, timeout=3).status_code == 200
        except Exception:
            return False

    if _is_up():
        return True

    print("[LLM] Ollama not running — launching 'ollama serve'…")
    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except FileNotFoundError:
        print("[LLM] 'ollama' command not found. Install Ollama from https://ollama.com")
        return False
    except Exception as e:
        print(f"[LLM] Could not launch Ollama: {e}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _is_up():
            print("[LLM] Ollama started successfully.")
            return True

    print("[LLM] Ollama did not respond within the timeout.")
    return False


def warmup_model(system_prompt: str | None = None) -> bool:
    """
    Pre-load the model AND prime Ollama's KV prefix cache.

    Why the system_prompt matters
    ─────────────────────────────
    Ollama caches the KV attention state of the prompt prefix across requests.
    If warmup includes the same system prompt that real requests will use, Ollama
    evaluates those tokens ONCE at startup.  Every subsequent request only needs
    to evaluate the small delta (user message ± time context) instead of the full
    300-500 token system prompt → drops first-token latency from ~17 s to <1 s.

    Pass the *static* part of the system prompt (the JARVIS protocol text, without
    timestamps or per-minute context) so the prefix stays valid across calls.
    """
    url, model = get_llm_settings()
    provider   = get_llm_provider()
    print(f"[LLM] Warming up '{model}' ({provider})…")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": "hi"})

    if provider == "openai":
        # Cloud models have no local cold-start to prime, and firing a warmup
        # request just burns quota against rate-limited free tiers (429s).
        print(f"[LLM] '{model}' (cloud) — skipping warmup.")
        return True

    # ── Ollama ──────────────────────────────────────────────────────────────
    payload = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        # num_gpu:99 → push ALL transformer layers to GPU (Ollama caps at available)
        # This is safe even without a GPU — Ollama silently ignores if n_gpu_layers=0
        "options":    {"num_predict": 1, "num_gpu": 99},
    }
    try:
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=180)
        resp.raise_for_status()
        print(f"[LLM] '{model}' loaded and KV cache primed.")
        return True
    except Exception as e:
        print(f"[LLM] Warmup failed (non-fatal): {e}")
        return False


def check_model_available(log: Callable | None = None) -> bool:
    """
    Returns True if the configured model is already pulled in Ollama.
    Logs an actionable warning (to console + optional UI callback) if not.
    Always returns True for non-Ollama providers (cannot inspect their model list).
    """
    if get_llm_provider() != "ollama":
        return True

    url, model = get_llm_settings()
    try:
        resp = requests.get(f"{url}/api/tags", timeout=5)
        resp.raise_for_status()
        pulled = [m.get("name", "") for m in resp.json().get("models", [])]
        model_base = model.split(":")[0]
        found = any(
            m == model or m == model_base or m.startswith(model_base + ":")
            for m in pulled
        )
        if not found:
            available = ", ".join(pulled) if pulled else "none"
            warn = (
                f"WRN: Model '{model}' is not pulled in Ollama.\n"
                f"     Available: {available}\n"
                f"     Fix: ollama pull {model}"
            )
            print(warn)
            if log:
                log(f"WRN: '{model}' not found — run: ollama pull {model}")
        return found
    except Exception:
        return True   # Ollama might still be starting up; non-blocking


def get_llm_settings() -> tuple[str, str]:
    """Returns (base_url, model_name)."""
    cfg   = _load_config()
    url   = cfg.get("llm_url",   _DEFAULTS["llm_url"]).rstrip("/")
    model = cfg.get("llm_model", _DEFAULTS["llm_model"])
    return url, model


def call_llm(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
    tier:     str = "main",
) -> dict:
    """
    Non-streaming chat request.  Routes to Ollama or OpenAI-compatible backend.

    Returns:
        {"content": str, "tool_calls": list}
    """
    url, model, key = _tier(tier)
    provider   = get_llm_provider()

    if provider == "openai":
        endpoint = _chat_endpoint(url)
        payload: dict = {
            "model":      model,
            "messages":   _to_openai_messages(messages),
            "stream":     False,
            "max_tokens": 256,
        }
        payload.update(_openai_extra(model))
        if tools:
            payload["tools"]       = tools
            payload["tool_choice"] = "auto"
        try:
            resp = _post_retry(endpoint, payload, _auth(key), timeout)
            resp.raise_for_status()
            _data = resp.json()
            _tm_record(model, _data.get("usage"))
            choice = _data.get("choices", [{}])[0]
            msg    = choice.get("message", {})
            # OpenAI tool_calls format → normalise to Ollama-style
            raw_tc  = msg.get("tool_calls") or []
            tc_list = [
                {
                    "id":       t.get("id", ""),
                    "function": {
                        "name":      t["function"]["name"],
                        "arguments": (
                            json.loads(t["function"]["arguments"])
                            if isinstance(t["function"].get("arguments"), str)
                            else t["function"].get("arguments", {})
                        ),
                    },
                }
                for t in raw_tc
            ]
            return {
                "content":    (msg.get("content") or "").strip(),
                "tool_calls": tc_list,
            }
        except Exception as e:
            if tier != "simple":   # fall back to the free tier (e.g. Anthropic credit out)
                print(f"[LLM] tier '{tier}' failed ({e}); falling back to simple.")
                return call_llm(messages, tools, timeout, tier="simple")
            raise RuntimeError(f"OpenAI-compatible LLM call failed: {e}")

    # ── Ollama ──────────────────────────────────────────────────────────────
    endpoint = f"{url}/api/chat"
    payload = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        msg  = data.get("message", {})
        return {
            "content":    (msg.get("content") or "").strip(),
            "tool_calls": msg.get("tool_calls") or [],
        }
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                msg  = data.get("message", {})
                return {
                    "content":    (msg.get("content") or "").strip(),
                    "tool_calls": msg.get("tool_calls") or [],
                }
            except Exception:
                pass
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama request timed out after 120 s.")
    except requests.exceptions.HTTPError as e:
        print(f"[LLM] HTTPError: {e.response.status_code} — {e.response.text[:200]}")
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        print(f"[LLM] Unexpected error: {type(e).__name__}: {e}")
        raise RuntimeError(f"LLM call failed: {e}")


def call_llm_text(
    prompt:  str,
    system:  str | None = None,
    model:   str | None = None,
    timeout: int = 120,
    tier:    str = "main",
    max_tokens: int = 900,
) -> str:
    """
    Simple text-only generation (no tools).
    Used by planner, executor, error_handler, code_helper, dev_agent, workflows.
    Pass a large max_tokens for code/full builds so the output isn't truncated.
    """
    url, default_model, key = _tier(tier)
    m        = model or default_model

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # OpenAI-compatible cloud providers (Anthropic, NVIDIA NIM, OpenRouter…)
    if get_llm_provider() == "openai":
        payload = {"model": m, "messages": _to_openai_messages(messages), "stream": False, "max_tokens": max_tokens}
        payload.update(_openai_extra(m))
        try:
            resp = _post_retry(_chat_endpoint(url), payload, _auth(key), timeout)
            resp.raise_for_status()
            _data = resp.json()
            _tm_record(m, _data.get("usage"))
            return (_data.get("choices", [{}])[0]
                    .get("message", {}).get("content") or "").strip()
        except Exception as e:
            if tier != "simple":
                print(f"[LLM] text tier '{tier}' failed ({e}); falling back to simple.")
                return call_llm_text(prompt, system, None, timeout, tier="simple", max_tokens=max_tokens)
            raise RuntimeError(f"OpenAI-compatible text call failed: {e}")

    endpoint = f"{url}/api/chat"
    payload = {"model": m, "messages": messages, "stream": False, "keep_alive": -1, "options": {"num_predict": max_tokens}}

    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        return (resp.json().get("message", {}).get("content") or "").strip()
    except requests.exceptions.ConnectionError:
        if ensure_ollama_running():
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                return (resp.json().get("message", {}).get("content") or "").strip()
            except Exception:
                pass
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except Exception as e:
        raise RuntimeError(f"LLM text call failed: {e}")


def _stream_openai(
    messages: list,
    tools:    list | None,
    timeout:  int,
    tier:     str = "main",
) -> Generator[dict, None, None]:
    """
    Streaming backend for OpenAI-compatible servers (Anthropic, NVIDIA NIM…).

    Parses Server-Sent Events (SSE) and accumulates streaming tool-call fragments
    so the output format is identical to the Ollama backend.
    """
    url, model, key = _tier(tier)
    endpoint   = _chat_endpoint(url)

    payload: dict = {
        "model":      model,
        "messages":   _to_openai_messages(messages),
        "stream":     True,
        # 256 was truncating replies mid-sentence. Give Alfred room to finish a
        # thought without letting him ramble indefinitely.
        "max_tokens": 1200,
    }
    payload.update(_openai_extra(model))
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"

    try:
        with _post_retry(endpoint, payload, _auth(key), timeout, stream=True) as resp:
            resp.raise_for_status()
            full_content = ""
            buf          = ""
            # tool_call fragments: index → {"id", "function": {"name", "arguments"}}
            tc_fragments: dict[int, dict] = {}

            for raw in resp.iter_lines():
                if not raw:
                    continue
                # SSE lines look like: b"data: {...}" or b"data: [DONE]"
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choice = chunk.get("choices", [{}])[0]
                delta  = choice.get("delta", {})
                text   = delta.get("content") or ""

                full_content += text
                buf          += text

                # Accumulate sentence boundaries for streaming TTS
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end():]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                # Accumulate streaming tool-call fragments
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    if idx not in tc_fragments:
                        tc_fragments[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    frag = tc_fragments[idx]
                    frag["id"] = frag["id"] or tc.get("id", "")
                    fn = tc.get("function", {})
                    frag["function"]["name"]      += fn.get("name") or ""
                    frag["function"]["arguments"] += fn.get("arguments") or ""

                finish = choice.get("finish_reason")
                if finish in ("stop", "tool_calls", "length"):
                    break

            # Flush any trailing content
            if buf.strip():
                yield {"type": "sentence", "text": buf.strip()}

            # Parse accumulated tool-call argument strings → dicts
            tool_calls: list = []
            for idx in sorted(tc_fragments):
                frag = tc_fragments[idx]
                args = frag["function"]["arguments"]
                try:
                    args = json.loads(args)
                except Exception:
                    pass   # leave as raw string; _execute_tool handles it
                tool_calls.append({
                    "id":       frag["id"],
                    "function": {"name": frag["function"]["name"], "arguments": args},
                })

            # streaming responses carry no usage block — estimate ~4 chars/token
            _tm_record(model, None,
                       est_in=sum(len(str(m.get("content", ""))) for m in messages) // 4,
                       est_out=len(full_content) // 4)

            yield {
                "type":       "done",
                "content":    full_content.strip(),
                "tool_calls": tool_calls,
            }

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Cannot reach OpenAI-compatible server at {url}.\n"
            "Make sure LM Studio / LocalAI / Jan is running and the server is started."
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("OpenAI-compatible stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"OpenAI-compatible HTTP error: {e.response.status_code}")
    except Exception as e:
        raise RuntimeError(f"OpenAI-compatible stream failed: {e}")


def call_llm_stream(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
    tier:     str = "main",
) -> Generator[dict, None, None]:
    """
    Streaming chat request.  Routes to Ollama or OpenAI-compatible backend.

    Yields:
        {"type": "sentence", "text": str}   — each complete sentence as it arrives
        {"type": "done", "content": str, "tool_calls": list}  — when stream ends

    Sentences are split on [.!?] + whitespace so TTS can start immediately.
    Tool calls always appear in the final "done" event.
    """
    provider = get_llm_provider()
    if provider == "openai":
        emitted = False
        try:
            for ev in _stream_openai(messages, tools, timeout, tier):
                emitted = True
                yield ev
        except Exception as e:
            # Fall back to the free tier only if we haven't spoken anything yet.
            if not emitted and tier != "simple":
                print(f"[LLM] stream tier '{tier}' failed ({e}); falling back to simple.")
                yield from _stream_openai(messages, tools, timeout, "simple")
            else:
                raise
        return

    url, model = get_llm_settings()
    endpoint   = f"{url}/api/chat"

    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "keep_alive": -1,
        # 150 tokens ≈ 100 words ≈ 3-4 sentences — enough for any voice reply.
        # num_gpu:99 pushes all layers to GPU; num_thread removed (Ollama auto-tunes).
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    def _do_stream() -> Generator[dict, None, None]:
        with requests.post(endpoint, json=payload, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            full_content = ""
            tool_calls:  list = []
            buf          = ""

            for raw in resp.iter_lines():
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg   = chunk.get("message", {})
                delta = msg.get("content") or ""

                full_content += delta
                buf          += delta

                # Yield complete sentences as they accumulate
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end() :]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                tc = msg.get("tool_calls")
                if tc:
                    tool_calls.extend(tc)

                if chunk.get("done"):
                    if buf.strip():
                        yield {"type": "sentence", "text": buf.strip()}

                    yield {
                        "type":       "done",
                        "content":    full_content.strip(),
                        "tool_calls": tool_calls,
                    }
                    return

    try:
        yield from _do_stream()
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] Stream ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            yield from _do_stream()
            return
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        print(f"[LLM] Stream error: {type(e).__name__}: {e}")
        raise RuntimeError(f"LLM stream failed: {e}")
