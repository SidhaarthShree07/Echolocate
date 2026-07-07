"""
EchoLocate — TTS synthesizer.

Primary: Kokoro-82M via kokoro-onnx (StyleTTS2-based, Apache 2.0, ~82M params).
Fallback: Piper TTS (auto-engaged if Kokoro fails to initialize).
Final fallback: print to console (degraded but never silent).

Kokoro is confirmed cross-platform (Windows/Linux/macOS) via onnxruntime
wheels — not just assumed. Architecture Section 4.5 explains the Piper
replacement and the retention of Piper as a fallback.

Playback is synchronous by design (PRD Section 18 Tier 3 — streaming TTS
is a stretch goal, not MVP). The agent waits for full synthesis before speaking.
Sentence-boundary chunking is used for "read aloud" mode to allow natural
pauses and faster barge-in detection.
"""
from __future__ import annotations

import re
import sys
import queue
import threading
from typing import Iterable, Optional, Callable


class TTSSynthesizer:
    """
    Manages TTS with Kokoro-82M primary and Piper fallback.

    Usage:
        tts = TTSSynthesizer(voice="af_heart", speed=1.0)
        tts.speak("Hello, I found your file.")
    """

    def __init__(
        self,
        engine: str = "kokoro",
        voice: str = "af_heart",
        speed: float = 1.0,
        should_stop: Optional[Callable[[], bool]] = None,
        on_playback_start: Optional[Callable[[], None]] = None,
        on_playback_end: Optional[Callable[[], None]] = None,
    ) -> None:
        self.engine = engine
        self.voice = voice
        self.speed = speed
        # Callable that returns True when playback should be interrupted
        self.should_stop = should_stop or (lambda: False)
        self.on_playback_start = on_playback_start or (lambda: None)
        self.on_playback_end = on_playback_end or (lambda: None)

        self._kokoro = None
        self._piper = None
        self._active_engine = None

        self._init_engine()

    def _init_engine(self) -> None:
        """Try to initialize Kokoro; fall back to Piper, then text."""
        if self.engine == "kokoro" or self.engine == "auto":
            try:
                self._init_kokoro()
                self._active_engine = "kokoro"
                print("[TTS] Kokoro-82M initialized.")
                return
            except Exception as exc:
                print(f"[TTS] Kokoro failed to initialize ({exc}). Trying Piper fallback...")

        try:
            self._init_piper()
            self._active_engine = "piper"
            print("[TTS] Piper initialized (fallback).")
            return
        except Exception as exc:
            print(f"[TTS] Piper also failed ({exc}). Falling back to console output.")
            self._active_engine = "text"

    def _init_kokoro(self) -> None:
        from kokoro_onnx import Kokoro  # type: ignore
        from pathlib import Path
        repo_root = Path(__file__).parent.parent.parent
        model_path = repo_root / "models" / "tts" / "kokoro-v1.0.onnx"
        voices_path = repo_root / "models" / "tts" / "voices-v1.0.bin"
        self._kokoro = Kokoro(str(model_path), str(voices_path))

    def _init_piper(self) -> None:
        # Piper is an optional fallback — not in default requirements.txt
        # Only engaged if kokoro-onnx fails
        import piper  # type: ignore
        self._piper = piper

    def speak(self, text: str) -> None:
        """
        Synthesize and play *text*.

        For read-aloud mode (long text), use speak_chunked() to get
        sentence-boundary pausing and stop-key interruption.
        """
        if not text.strip():
            return

        if self._active_engine == "kokoro":
            self._speak_kokoro(text)
        elif self._active_engine == "piper":
            self._speak_piper(text)
        else:
            # Final fallback: print to console
            print(f"\n[EchoLocate says]: {text}\n")

    def speak_chunked(self, text: str) -> None:
        """
        Speak *text* sentence by sentence. Checks should_stop() between
        sentences to allow fast interruption (barge-in / stop hotkey).

        Used by Document node for read-aloud mode.
        """
        sentences = _split_sentences(text)
        for sentence in sentences:
            if self.should_stop():
                print("[TTS] Playback interrupted by stop signal.")
                break
            if sentence.strip():
                self.speak(sentence)

    def speak_stream(self, text_iterator: Iterable[str]) -> str:
        """
        Speak generated text as complete sentences arrive from an iterator.

        A producer thread consumes LLM chunks and pushes cleaned sentences to
        a queue; this foreground consumer speaks each sentence synchronously.
        Returns the full generated text for transcript history.
        """
        from echolocate.nodes.document import _markdown_to_spoken

        sentence_queue: queue.Queue[Optional[str]] = queue.Queue()
        full_chunks: list[str] = []

        def _producer() -> None:
            buffer = ""
            try:
                for chunk in text_iterator:
                    if self.should_stop():
                        break
                    if not chunk:
                        continue
                    full_chunks.append(chunk)
                    buffer += chunk
                    while True:
                        match = re.search(r"(.+?(?:[.!?](?=\s|$)|\n+))", buffer, flags=re.DOTALL)
                        if not match:
                            break
                        sentence = match.group(1)
                        buffer = buffer[match.end():]
                        spoken = _markdown_to_spoken(sentence)
                        if spoken:
                            sentence_queue.put(spoken)
                tail = _markdown_to_spoken(buffer)
                if tail:
                    sentence_queue.put(tail)
            finally:
                sentence_queue.put(None)

        producer = threading.Thread(target=_producer, daemon=True)
        producer.start()

        while True:
            sentence = sentence_queue.get()
            if sentence is None:
                break
            if self.should_stop():
                print("[TTS] Streaming playback interrupted by stop signal.")
                break
            self.speak(sentence)

        producer.join(timeout=2.0)
        return "".join(full_chunks).strip()

    def _speak_kokoro(self, text: str) -> None:
        """Synthesize with Kokoro and play via sounddevice."""
        try:
            import sounddevice as sd  # type: ignore
            samples, sample_rate = self._kokoro.create(
                text,
                voice=self.voice,
                speed=self.speed,
                lang="en-us",
            )
            if self.should_stop():
                return
            self.on_playback_start()
            try:
                sd.play(samples, samplerate=sample_rate)
                sd.wait()
            finally:
                self.on_playback_end()
        except Exception as exc:
            print(f"[TTS] Kokoro playback error: {exc} — text: {text!r}")
            # Degrade to console on playback error
            print(f"\n[EchoLocate says]: {text}\n")

    def _speak_piper(self, text: str) -> None:
        """Synthesize with Piper (fallback)."""
        try:
            import io
            import sounddevice as sd  # type: ignore
            import soundfile as sf  # type: ignore
            import subprocess

            # Piper reads text from stdin, outputs WAV to stdout
            proc = subprocess.run(
                ["piper", "--output-raw"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout:
                audio_buf = io.BytesIO(proc.stdout)
                data, sr = sf.read(audio_buf, dtype="float32")
                if self.should_stop():
                    return
                self.on_playback_start()
                try:
                    sd.play(data, samplerate=sr)
                    sd.wait()
                finally:
                    self.on_playback_end()
            else:
                print(f"\n[EchoLocate says]: {text}\n")
        except Exception as exc:
            print(f"[TTS] Piper playback error: {exc}")
            print(f"\n[EchoLocate says]: {text}\n")

    def set_speed(self, speed: float) -> None:
        """Update speech rate (persisted for session lifetime)."""
        self.speed = max(0.5, min(2.0, speed))

    @property
    def active_engine_name(self) -> str:
        return self._active_engine or "none"


def _split_sentences(text: str) -> list[str]:
    """
    Split text into sentences for chunked TTS output.

    Uses a simple regex pattern for sentence boundary detection.
    Not perfect — but good enough for the voice-output use case where
    a slightly imperfect split just creates a short pause in odd places.
    """
    # Split on . ! ? followed by whitespace or end of string
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s for s in sentences if s.strip()]
