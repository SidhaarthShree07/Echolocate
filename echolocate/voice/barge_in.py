"""
EchoLocate — barge-in mute controller.

While TTS is playing, the microphone is muted so the agent cannot transcribe
its own voice output. This avoids the feedback loop of TTS speech entering
the STT pipeline as a spurious command.

Design (Architecture Section 4.1):
  - Mute is at the application level (stop reading from the audio stream),
    not a global OS mute. This avoids any OS-level permission issues and
    is reversible instantly.
  - A dedicated stop hotkey (escape by default) allows the user to interrupt
    TTS playback even while the mic is muted — important for motor-impaired
    users who can't easily speak a new command mid-playback.
  - Barge-in deactivation is signalled via a threading.Event, not polling,
    so the pipeline resumes immediately when TTS ends.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class BargeInController:
    """
    Manages mic-mute state during TTS playback.

    The activation gate's on_stop_playback callback fires the stop event,
    which the TTS engine monitors to interrupt playback.
    """

    def __init__(self) -> None:
        self._muted = threading.Event()
        self._stop_playback = threading.Event()
        self._on_stop: Optional[Callable[[], None]] = None
        self._unmute_time = 0.0
        self._lock = threading.Lock()
        self._tts_depth = 0

    @property
    def is_muted(self) -> bool:
        """True while TTS is playing and for 0.5s after, to ignore room echo."""
        import time
        if self._muted.is_set():
            return True
        return time.monotonic() < self._unmute_time

    def start_tts(self) -> None:
        """Call before TTS playback begins. Mutes the mic input."""
        with self._lock:
            self._tts_depth += 1
            self._stop_playback.clear()
            self._muted.set()

    def end_tts(self) -> None:
        """Call after TTS playback completes or is interrupted."""
        import time
        with self._lock:
            self._tts_depth = max(0, self._tts_depth - 1)
            if self._tts_depth == 0:
                self._unmute_time = time.monotonic() + 0.5
                self._muted.clear()
                self._stop_playback.clear()

    def request_stop(self) -> None:
        """
        Called by the stop hotkey to interrupt TTS mid-playback.
        Sets the stop event; TTS engine checks this flag while speaking.
        """
        self._stop_playback.set()
        if self._on_stop:
            self._on_stop()

    def set_on_stop(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when stop_playback is requested."""
        self._on_stop = callback

    def should_stop_playback(self) -> bool:
        """
        Returns True if a stop-playback request is pending.
        TTS engine polls this during sentence-by-sentence output.
        """
        return self._stop_playback.is_set()

    def wait_for_mic_unmute(self, timeout: Optional[float] = None) -> bool:
        """
        Block until the mic is unmuted (TTS playback ends).
        Returns True if unmuted, False if timeout expired.
        """
        if not self.is_muted:
            return True
        unmuted = threading.Event()
        # Poll with short interval — no clean "wait until not set" in stdlib
        import time
        start = time.monotonic()
        while self._muted.is_set():
            time.sleep(0.05)
            if timeout and (time.monotonic() - start) > timeout:
                return False
        return True
