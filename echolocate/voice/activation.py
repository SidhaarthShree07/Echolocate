"""
EchoLocate — activation gate.

Controls when audio capture is active. Two modes:
  - "hotkey"   (default): push-to-talk via a configured key combination.
    Audio capture runs only while the key is held. This is the default because
    it eliminates continuous mic access, is more reliable for a motor-impaired
    user who prefers a deliberate physical trigger, and removes wake-word
    calibration as a build-week variable.
  - "wakeword": continuous listening via openWakeWord. Activates when the
    wake word is detected. One-flag opt-in via config.

Mode is set by config/default_config.yaml or ECHOLOCATE_ACTIVATION_MODE env var.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional
from pathlib import Path


class ActivationGate:
    """
    Controls audio capture activation based on the configured mode.

    Usage:
        gate = ActivationGate(mode="hotkey", hotkey="space")
        gate.on_activate = lambda: print("activated!")
        gate.on_deactivate = lambda: print("deactivated!")
        gate.start()
    """

    def __init__(
        self,
        mode: str = "hotkey",
        hotkey: str = "space",
        stop_hotkey: str = "escape",
        wake_word_model: str = "hey_jarvis",
        wake_word_threshold: float = 0.5,
    ) -> None:
        self.mode = mode
        self.hotkey = hotkey
        self.stop_hotkey = stop_hotkey
        self.wake_word_model = wake_word_model
        self.wake_word_threshold = wake_word_threshold

        self.on_activate: Optional[Callable[[], None]] = None
        self.on_deactivate: Optional[Callable[[], None]] = None
        self.on_stop_playback: Optional[Callable[[], None]] = None
        self.is_muted: Callable[[], bool] = lambda: False

        self._active = False
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def is_active(self) -> bool:
        return self._active

    def start(self) -> None:
        """Start the activation gate in a background thread."""
        self._running = True
        if self.mode == "hotkey":
            self._thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        elif self.mode == "wakeword":
            self._thread = threading.Thread(target=self._wakeword_loop, daemon=True)
        else:
            raise ValueError(f"Unknown activation mode: {self.mode!r}")
        self._thread.start()

    def stop(self) -> None:
        """Stop the activation gate."""
        self._running = False
        self._active = False

    def _set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if active and self.on_activate:
            self.on_activate()
        elif not active and self.on_deactivate:
            self.on_deactivate()

    def _hotkey_loop(self) -> None:
        """
        Push-to-talk loop using the keyboard library.
        Activates while the hotkey is pressed, deactivates on release.
        """
        try:
            import keyboard  # type: ignore
        except ImportError:
            print("[ActivationGate] 'keyboard' package not installed. "
                  "Install with: pip install keyboard")
            return

        # Register stop-playback hotkey (fires during TTS barge-in mute)
        if self.on_stop_playback:
            keyboard.add_hotkey(self.stop_hotkey, self.on_stop_playback)

        print(f"[EchoLocate] Push-to-talk mode. Hold '{self.hotkey}' to speak.")
        while self._running:
            is_held = keyboard.is_pressed(self.hotkey)
            self._set_active(is_held)
            time.sleep(0.02)  # 20ms polling — responsive without busy-waiting

    def _wakeword_loop(self) -> None:
        """
        Continuous wake-word detection using openWakeWord.
        Activates for one utterance when the wake word is detected.
        """
        try:
            import openwakeword  # type: ignore
            from openwakeword.model import Model  # type: ignore
        except ImportError:
            print("[ActivationGate] 'openwakeword' package not installed. "
                  "Install with: pip install openwakeword  (or set activation_mode=hotkey)")
            return

        try:
            import sounddevice as sd  # type: ignore
            import numpy as np
            import os
            # Look for local custom models inside the repository's assets/wakewords folder
            model_path = self.wake_word_model
            repo_root = Path(__file__).parent.parent.parent
            wakewords_dir = repo_root / "assets" / "wakewords"
            
            local_v01 = str(wakewords_dir / f"{self.wake_word_model}_v0.1.onnx")
            local_simple = str(wakewords_dir / f"{self.wake_word_model}.onnx")
            
            if not os.path.exists(model_path):
                if os.path.exists(local_v01):
                    model_path = local_v01
                elif os.path.exists(local_simple):
                    model_path = local_simple

            try:
                model = Model(wakeword_models=[model_path], inference_framework="onnx")
            except ValueError as e:
                if "Could not find pretrained model" in str(e):
                    print(f"\n[ActivationGate] ERROR: Custom wake word '{model_path}' requires a '{model_path}.onnx' file!")
                    print("[ActivationGate] Please generate it using the openWakeWord HuggingFace Space or Colab.")
                    print("[ActivationGate] Falling back to default 'hey_jarvis' for this session...\n")
                    model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
                    self.wake_word_model = "hey_jarvis"
                else:
                    raise
        except ImportError:
            print("[ActivationGate] 'sounddevice'/'numpy' not installed.")
            return

        chunk_size = 1280  # samples at 16kHz = 80ms
        print(f"[EchoLocate] Wake-word mode. Say '{self.wake_word_model}' to activate.")

        with sd.InputStream(samplerate=16000, channels=1, dtype="int16") as stream:
            last_diag_time = time.time()
            max_score_2s = 0.0
            sum_rms_2s = 0.0
            count_2s = 0

            while self._running:
                chunk, _ = stream.read(chunk_size)
                audio = chunk.flatten().astype("int16")
                
                # If TTS is speaking, feed silence to flush the model's sliding window
                if self.is_muted():
                    audio.fill(0)

                predictions = model.predict(audio)

                # Calculate volume (Root Mean Square) for 16-bit integer audio
                rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
                sum_rms_2s += rms
                count_2s += 1

                # Check if wake word score exceeds threshold
                # Match keys dynamically (e.g., 'hey_jarvis_v0.1' contains 'hey_jarvis')
                score = 0.0
                for k, v in predictions.items():
                    if self.wake_word_model in k:
                        score = v
                        break

                if score > max_score_2s:
                    max_score_2s = score

                if score >= self.wake_word_threshold and not self._active:
                    print(f"\n[ActivationGate] Wake word detected: '{self.wake_word_model}' (score: {score:.2f})")
                    self._set_active(True)
                    # Active for one utterance — deactivation happens via VAD
                    # when the speech segment ends (handled by caller)

                # Print diagnostics every 2.0 seconds
                now = time.time()
                if now - last_diag_time >= 2.0:
                    
                    # Reset accumulators
                    last_diag_time = now
                    max_score_2s = 0.0
                    sum_rms_2s = 0.0
                    count_2s = 0
