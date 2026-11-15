"""Local microphone recording with optional end-of-speech detection.

A low-cost energy gate ends an explicit recording after a silence interval.
This is NOT a continuous ambient listener or a full neural VAD; a button/hotkey
is always available as a fallback in a noisy room.
"""
from __future__ import annotations

import threading
import time
import os
import numpy as np


class Microphone:
    def __init__(self):
        self._stream = None
        self._chunks = []
        self._lock = threading.Lock()
        self.sample_rate = 16000
        self._started = 0.0
        self._heard_voice = False
        self._last_voice = 0.0
        self._latest_rms = 0.0
        self._noise_rms = []
        self._threshold = 0.012
        self._continuous = False
        self._first_voice = 0.0

    def start(self, *, continuous: bool = False):
        import sounddevice as sd
        if self._stream:
            raise RuntimeError("すでに録音中です")
        device = sd.query_devices(kind="input")
        self.sample_rate = int(device["default_samplerate"] or 16000)
        with self._lock:
            self._chunks = []
            self._started = time.monotonic()
            self._last_voice = 0.0
            self._heard_voice = False
            self._latest_rms = 0.0
            self._noise_rms = []
            self._threshold = 0.012
            self._continuous = continuous
            self._first_voice = 0.0

        def callback(indata, frames, time_info, status):
            chunk = indata[:, 0].copy()
            rms = float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0
            now = time.monotonic()
            with self._lock:
                self._chunks.append(chunk)
                # A hands-free session may listen to silence for minutes.
                # Keep at most ~2 seconds of pre-roll until speech begins.
                # Otherwise the microphone would accumulate unbounded RAM.
                if self._continuous and not self._heard_voice:
                    max_chunks = max(1, int(self.sample_rate * 2 / max(1, frames)) + 1)
                    if len(self._chunks) > max_chunks:
                        del self._chunks[:-max_chunks]
                self._latest_rms = rms
                # First half-second estimates room noise; thresholds stay bounded.
                # Never calibrate on a loud initial utterance: users often
                # start speaking immediately after pressing the mic button.
                if now - self._started < 0.55 and rms < 0.022:
                    self._noise_rms.append(rms)
                    if self._noise_rms:
                        self._threshold = max(0.008, min(0.035, float(np.median(self._noise_rms)) * 3.5))
                if rms > self._threshold and (now - self._started > 0.10):
                    if not self._heard_voice:
                        self._first_voice = now
                    self._heard_voice = True
                    self._last_voice = now

        stream = sd.InputStream(samplerate=self.sample_rate, channels=1,
                                dtype="float32", blocksize=1024, callback=callback)
        try:
            stream.start()
        except Exception:
            # PortAudio can fail after opening the input device. Close it before
            # allowing another attempt; otherwise the microphone stays locked.
            stream.close()
            raise
        self._stream = stream

    def activity(self) -> dict:
        with self._lock:
            elapsed = time.monotonic() - self._started if self._stream else 0.0
            silence = (time.monotonic() - self._last_voice) if self._heard_voice else 0.0
            return {"elapsed": elapsed, "heard_voice": self._heard_voice,
                    "silence": silence, "level": self._latest_rms, "threshold": self._threshold}

    def stop(self) -> np.ndarray:
        if not self._stream:
            raise RuntimeError("録音していません")
        stream, self._stream = self._stream, None
        stream.stop()
        stream.close()
        with self._lock:
            samples = np.concatenate(self._chunks) if self._chunks else np.empty(0, np.float32)
            self._chunks = []
        if samples.size == 0:
            return samples
        if self.sample_rate != 16000:
            out_length = max(1, round(samples.size * 16000 / self.sample_rate))
            samples = np.interp(np.arange(out_length) / 16000,
                                np.arange(samples.size) / self.sample_rate, samples).astype(np.float32)
        return samples


class SpeechRecognizer:
    def __init__(self, model_name="small"):
        self.model_name = model_name
        self.model = None

    def transcribe(self, samples: np.ndarray) -> str:
        if samples.size < 1600:
            raise ValueError("音声が短すぎます")
        # Skip near-digital silence before loading a model. This conservative
        # gate does not attempt to classify quiet speech using an RMS threshold
        # alone (which would reject soft-spoken users).
        if float(np.max(np.abs(samples))) < 0.006 and float(np.sqrt(np.mean(samples * samples))) < 0.001:
            return ""
        if self.model is None:
            from faster_whisper import WhisperModel
            # Cap native inference threads on laptops. A faster model should
            # not make the rest of Windows unusable while transcribing.
            threads = min(3, max(1, (os.cpu_count() or 4) // 2))
            self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                      cpu_threads=threads, num_workers=1)
        segments, _ = self.model.transcribe(
            samples, language="ja", beam_size=5, vad_filter=True,
            condition_on_previous_text=False,
            # Vocabulary hints, not a plausible imperative sentence: avoids
            # turning room noise into an invented desktop command.
            initial_prompt="Chrome、Windows、エクスプローラー、VS Code、ChatGPT、GitHub、PDF、Ollama。",
        )
        accepted = []
        for segment in segments:
            # Reject only very low-confidence / highly no-speech segments.
            # A normal quiet utterance must not be discarded just because its
            # no_speech probability is moderately elevated.
            logprob = getattr(segment, "avg_logprob", None)
            no_speech = getattr(segment, "no_speech_prob", None)
            if (isinstance(logprob, (float, int)) and
                isinstance(no_speech, (float, int)) and
                no_speech >= 0.90 and logprob < -0.80):
                continue
            accepted.append(segment.text)
        return "".join(accepted).strip()