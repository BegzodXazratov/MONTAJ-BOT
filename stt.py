"""Nutqni matnga o'girish: ikki provayder — Whisper (bepul, serverda) va ElevenLabs Scribe (pullik API).

Ikkalasi ham bir xil formatda so'zlar ro'yxatini qaytaradi: [Word(text, start, end), ...]
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Transcript:
    provider: str
    words: list[Word]
    seconds: float  # o'girishga ketgan vaqt

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


# ---------------- Whisper (faster-whisper, CPU) ----------------
_whisper_model = None


def _whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        name = os.getenv("WHISPER_MODEL", "large-v3")
        threads = int(os.getenv("WHISPER_THREADS", "0")) or os.cpu_count() or 4
        _whisper_model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)
    return _whisper_model


def whisper_transcribe(wav: str, language: str = "uz") -> Transcript:
    t0 = time.time()
    # wav'ni o'zimiz o'qiymiz — PyAV (av) versiyalaridagi nomuvofiqlikdan qochish uchun
    import wave

    import numpy as np

    with wave.open(wav, "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _whisper().transcribe(
        audio,
        language=language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            txt = w.word.strip()
            if txt:
                words.append(Word(txt, float(w.start), float(w.end)))
    return Transcript("whisper", words, time.time() - t0)


# ---------------- ElevenLabs Scribe ----------------
ELEVEN_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def eleven_transcribe(wav: str, language: str = "uzb") -> Transcript:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY .env faylida yo'q")
    t0 = time.time()
    with open(wav, "rb") as f:
        r = httpx.post(
            ELEVEN_URL,
            headers={"xi-api-key": key},
            data={
                "model_id": os.getenv("ELEVEN_MODEL", "scribe_v1"),
                "language_code": language,
                "timestamps_granularity": "word",
                "tag_audio_events": "false",
            },
            files={"file": ("audio.wav", f, "audio/wav")},
            timeout=300,
        )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs xatosi {r.status_code}: {r.text[:300]}")
    words = [
        Word(w["text"].strip(), float(w["start"]), float(w["end"]))
        for w in r.json().get("words", [])
        if w.get("type") == "word" and w.get("text", "").strip()
    ]
    return Transcript("eleven", words, time.time() - t0)


PROVIDERS = {"whisper": whisper_transcribe, "eleven": eleven_transcribe}


def transcribe(wav: str, provider: str) -> Transcript:
    if provider not in PROVIDERS:
        raise ValueError(f"Noma'lum provayder: {provider}")
    return PROVIDERS[provider](wav)
