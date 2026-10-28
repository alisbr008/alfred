"""
Speech-to-Text engines for Alfred.

Whisper  – offline transcription via faster-whisper (VAD-buffered)
Vosk     – offline streaming transcription (lighter)
"""
import json
import numpy as np


class WhisperSTT:
    """Offline transcription using faster-whisper."""

    def __init__(self, model_name: str = "base.en", language: str | None = None):
        import os
        from faster_whisper import WhisperModel
        print(f"[STT] Loading Whisper '{model_name}'…")
        try:
            import torch
            device  = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
        except Exception:
            device, compute = "cpu", "int8"

        try:
            self._model = WhisperModel(model_name, device=device, compute_type=compute)
        except Exception as _first_err:
            # Offline flag set but model not cached yet → clear flags and download once.
            # Keywords cover multiple huggingface_hub error message variants across versions.
            _e = str(_first_err).lower()
            _offline_keywords = (
                "offline", "not found", "cache", "localentry",
                "does not exist", "outgoing", "local_files_only",
            )
            if any(k in _e for k in _offline_keywords):
                print(f"[STT] Whisper '{model_name}' not in local cache — downloading (one-time, internet required)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                # CRITICAL: huggingface_hub reads HF_HUB_OFFLINE ONCE at import time —
                # popping the env after import does nothing. Flip the live constant too.
                try:
                    import huggingface_hub.constants as _hfc
                    _hfc.HF_HUB_OFFLINE = False
                except Exception:
                    pass
                try:
                    self._model = WhisperModel(model_name, device=device, compute_type=compute)
                except Exception as _dl_err:
                    # NEVER boot deaf: fall back to the cached base model and say so.
                    if model_name != "base.en":
                        print(f"[STT] ⚠️ '{model_name}' download failed ({str(_dl_err)[:80]}) — "
                              "falling back to cached 'base.en'. Ears online, slightly less sharp.")
                        self._model = WhisperModel("base.en", device=device, compute_type=compute)
                        model_name = "base.en"
                    else:
                        raise RuntimeError(
                            f"Whisper '{model_name}' model download failed.\n"
                            f"Details: {_dl_err}"
                        ) from _dl_err
            else:
                raise

        self._language = None if (not language or language.strip().lower() == "auto") else language.strip().lower()
        print(f"[STT] Whisper '{model_name}' ready ({device})")

    # Bias Whisper toward the proper nouns it would otherwise mangle (model names,
    # brands, the agent names). This dramatically improves recognition of words like
    # "Anthropic", "Fable", "Claude", "Velthar" that are rare in generic speech.
    _VOCAB = ("Alfred, Anthropic, Claude, Claude Code, Fable, Sonnet, Haiku, Opus, Qwen, "
              "GLM, Kimi, NVIDIA NIM, GPT, Velthar Lab, Sentinel, Oracle, Echo, Obsidian, "
              "Tavily, Higgsfield, batcomputer, night watch, mission board, Alpha Centauri, "
              "Boeing, workflow, research, briefing, developer mode, showcase.")

    # Deterministic mishear repair — Whisper's classic slips for OUR proper nouns.
    # Applied after transcription; case-insensitive, whole-phrase, zero latency.
    _FIXES = (
        (r"\bcloud[\s-]*code\b", "Claude Code"), (r"\bclod[\s-]*code\b", "Claude Code"),
        (r"\bclaw(?:ed)?[\s-]*code\b", "Claude Code"), (r"\bcloud'?s code\b", "Claude Code"),
        (r"\bcloud\b(?=.*\b(?:build|coding|agent|model)\b)", "Claude"),
        (r"\bquen\b|\bkwen\b", "Qwen"), (r"\bkimmy\b|\bkimmie\b", "Kimi"),
        (r"\bg\.?l\.?m\.?\b", "GLM"), (r"\bhiggs?[\s-]*field\b", "Higgsfield"),
        (r"\bvel(?:th|t)ar\b|\bvelter\b|\bfelthar\b", "Velthar"),
        (r"\bbat[\s-]*computer\b", "batcomputer"), (r"\bsonet\b", "Sonnet"),
        (r"\banthropic'?s\b", "Anthropic's"), (r"\ben[\s-]*thropic\b", "Anthropic"),
        (r"\bnim\b", "NIM"), (r"\bhi[\s-]*ku\b", "Haiku"),
    )

    @classmethod
    def _repair(cls, text: str) -> str:
        import re as _re
        for pat, rep in cls._FIXES:
            text = _re.sub(pat, rep, text, flags=_re.IGNORECASE)
        return text

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz numpy array. Returns transcript string."""
        try:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1,                       # greedy — 2-3x faster
                best_of=1,
                condition_on_previous_text=False,  # no hallucinations, faster
                initial_prompt=self._VOCAB,        # prime domain vocabulary
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            # Anti-phantom-wake: the vocab prompt biases Whisper toward "Alfred",
            # so on noise/typing-clacks it can hallucinate the wake word. Drop
            # segments the model itself doubts are real, confident speech.
            parts = []
            for s in segments:
                if getattr(s, "no_speech_prob", 0.0) > 0.6:
                    continue                        # model thinks this wasn't speech
                if getattr(s, "avg_logprob", 0.0) < -1.2:
                    continue                        # gibberish / low-confidence decode
                parts.append(s.text)
            return self._repair(" ".join(parts).strip())
        except Exception as e:
            print(f"[STT] Transcription error: {e}")
            raise


class VoskSTT:
    """Streaming transcription using Vosk."""

    def __init__(self, model_path: str | None = None, language: str = "en-us"):
        from vosk import Model, KaldiRecognizer
        print("[STT] Loading Vosk model…")
        if model_path:
            model = Model(model_path)
        else:
            lang  = language.strip().lower() if language and language.strip().lower() != "auto" else "en-us"
            model = Model(lang=lang)
        self._rec = KaldiRecognizer(model, 16000)
        print("[STT] Vosk ready.")

    def process_chunk(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Feed raw int16 LE PCM bytes. Returns (text, is_final)."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False
