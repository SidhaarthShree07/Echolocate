"""
EchoLocate — Silero VAD wrapper (via silero-vad-lite).

Segments audio into speech-containing chunks BEFORE passing to faster-whisper.
This is a Tier 1 core requirement — Whisper-family models hallucinate
plausible-sounding text when fed silence or background noise. A hallucinated
transcript is a spurious command entering the router, not a cosmetic issue.

Uses silero-vad-lite (NOT the official silero-vad PyPI package):
  - silero-vad (official): hard-depends on torch+torchaudio even in ONNX mode
  - silero-vad-lite: bundles ONNX Runtime + model directly, zero Python deps

Architecture Section 4.1 explains the promotion from Tier 2 → Tier 1.
"""
from __future__ import annotations

import numpy as np
from typing import Optional


class VADGate:
    """
    Wrapper around silero-vad-lite.

    Buffers incoming audio chunks and yields complete speech segments
    (start-of-speech to end-of-silence).
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        silence_ms: int = 1200,
        sample_rate: int = 16000,
    ) -> None:
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.silence_ms = silence_ms
        self.sample_rate = sample_rate

        self._vad = self._load_vad()
        self._speech_buffer: list[np.ndarray] = []
        self._silence_frames: int = 0
        self._in_speech: bool = False

        # Compute frame counts
        chunk_ms = 32  # silero-vad-lite default chunk = 512 samples @ 16kHz = 32ms
        self._chunk_samples = int(sample_rate * chunk_ms / 1000)
        self._silence_frames_threshold = int(silence_ms / chunk_ms)
        self._min_speech_frames = int(min_speech_ms / chunk_ms)

    def _load_vad(self):
        try:
            from silero_vad_lite import SileroVAD  # type: ignore
            vad = SileroVAD(sample_rate=self.sample_rate)
            return vad
        except ImportError:
            raise ImportError(
                "silero-vad-lite is required. Install with: pip install silero-vad-lite\n"
                "Do NOT use the official silero-vad package — it requires PyTorch."
            )

    def process_chunk(self, audio_chunk: np.ndarray) -> Optional[np.ndarray]:
        """
        Process one audio chunk (16kHz, mono, int16 or float32).

        Returns a complete speech segment as a numpy array when a speech
        segment closes (speech detected, then silence_ms of quiet). Returns
        None while buffering.

        Args:
            audio_chunk: Audio samples (any length; will be processed in
                         internal chunk_samples windows).
        """
        # Normalize to float32 in [-1, 1] for Silero
        if audio_chunk.dtype == np.int16:
            audio_f32 = audio_chunk.astype(np.float32) / 32768.0
        else:
            audio_f32 = audio_chunk.astype(np.float32)

        # Process in fixed-size windows
        results = []
        pos = 0
        while pos + self._chunk_samples <= len(audio_f32):
            window = audio_f32[pos:pos + self._chunk_samples]
            
            # Simple noise gate: if window is extremely quiet (RMS < 0.0005, which is ~16 in int16),
            # classify it as silence to prevent false VAD triggers from mic hum/noise.
            rms = np.sqrt(np.mean(window ** 2))
            if rms < 0.0005:
                speech_prob = 0.0
            else:
                speech_prob = self._vad.process(window)
                
            is_speech = speech_prob >= self.threshold

            if is_speech:
                if not self._in_speech:
                    self._in_speech = True
                self._speech_buffer.append(window)
                self._silence_frames = 0
            else:
                if self._in_speech:
                    self._silence_frames += 1
                    self._speech_buffer.append(window)  # include trailing silence

                    if self._silence_frames >= self._silence_frames_threshold:
                        # Speech segment ended
                        segment = np.concatenate(self._speech_buffer)
                        self._speech_buffer = []
                        self._silence_frames = 0
                        self._in_speech = False

                        # Check minimum duration
                        if len(segment) >= self._min_speech_frames * self._chunk_samples:
                            results.append(segment)

            pos += self._chunk_samples

        return results[0] if results else None

    def reset(self) -> None:
        """Reset state between sessions."""
        self._speech_buffer = []
        self._silence_frames = 0
        self._in_speech = False
