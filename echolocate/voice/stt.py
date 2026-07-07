"""
EchoLocate — faster-whisper STT wrapper.

Transcribes only VAD-identified speech segments (never raw mic stream).
All transcription runs locally — no audio or transcript ever leaves the device.

Model choice: base or small (configurable). Base is recommended for the demo —
better accuracy than tiny, fast enough on CPU, small enough to co-exist with
the Gemma 4 models in RAM.
"""
from __future__ import annotations

import numpy as np
from typing import Optional


class STTEngine:
    """
    Wraps faster-whisper for local, offline speech-to-text.

    Only VAD-gated audio segments are passed here — raw mic input is never
    transcribed directly (prevents hallucination on silence/noise).
    """

    def __init__(
        self,
        model_size: str = "base",
        language: str = "en",
        device: str = "cpu",
        compute_type: str = "int8",  # "int8" for CPU, "float16" for GPU
    ) -> None:
        self.model_size = model_size
        self.language = language
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def load(self) -> None:
        """Load the Whisper model. Called once at startup."""
        _setup_cuda_dlls()
        try:
            from faster_whisper import WhisperModel  # type: ignore
            from huggingface_hub import snapshot_download  # type: ignore
        except ImportError:
            raise ImportError(
                "faster-whisper and huggingface-hub are required. Install with: pip install faster-whisper huggingface-hub"
            )

        # Resolve 'auto' device: try CUDA first, fall back to CPU
        resolved_device = self.device
        if self.device == "auto":
            try:
                import torch
                resolved_device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                import subprocess, sys
                try:
                    result = subprocess.run(
                        ["nvidia-smi"], capture_output=True, timeout=2
                    )
                    resolved_device = "cuda" if result.returncode == 0 else "cpu"
                except Exception:
                    resolved_device = "cpu"

        # Auto-select compute_type based on resolved device
        if self.compute_type == "int8" and resolved_device == "cuda":
            # int8 on CUDA is unstable for Whisper; use float16 instead
            compute_type = "float16"
        else:
            compute_type = self.compute_type

        # Resolve project-relative download directory for models
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        whisper_model_dir = os.path.join(project_root, "models", "whisper")
        os.makedirs(whisper_model_dir, exist_ok=True)

        # Suppress Hugging Face symlink warnings and enable progress bars
        os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        try:
            from huggingface_hub.utils import disable_progress_bars
            disable_progress_bars()
        except Exception:
            pass

        # Cleanup other models in the whisper directory to save storage
        _cleanup_old_whisper_models(self.model_size, whisper_model_dir)

        # Download the model files before instantiating WhisperModel
        repo_id = f"Systran/faster-whisper-{self.model_size}"
        try:
            model_path = snapshot_download(
                repo_id,
                cache_dir=whisper_model_dir,
                local_files_only=False,
            )
        except Exception as exc:
            model_path = self.model_size  # Fallback to default faster-whisper resolution

        try:
            self._model = WhisperModel(
                model_path,
                device=resolved_device,
                compute_type=compute_type,
            )
            # Warm up / verify CUDA library dependencies actually load correctly
            if resolved_device == "cuda":
                # Dry run with 1 second of dummy audio to trigger DLL load / forward pass
                list(self._model.transcribe(np.zeros(16000, dtype=np.float32)))
        except Exception as exc:
            if resolved_device == "cuda":
                # Graceful CPU fallback
                resolved_device = "cpu"
                compute_type = "int8"
                self._model = WhisperModel(
                    model_path,
                    device=resolved_device,
                    compute_type=compute_type,
                )
                print(f"[STT] Model loaded on {resolved_device} (fallback).")
            else:
                raise exc


    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """
        Transcribe a speech segment to text.

        Args:
            audio: Audio samples (float32, 16kHz, mono). Must be a VAD-identified
                   speech segment, NOT raw mic input.
            sample_rate: Audio sample rate (should always be 16000 for Whisper).

        Returns:
            Transcribed text string, stripped of leading/trailing whitespace.
            Returns "" if transcription is empty or model is not loaded.
        """
        if self._model is None:
            raise RuntimeError("STTEngine not loaded — call .load() first")

        if len(audio) == 0:
            return ""

        # Normalize to float32 in [-1, 1]
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        segments, _info = self._model.transcribe(
            audio,
            language=self.language if self.language else None,
            beam_size=5,
            vad_filter=False,  # We do our own VAD; don't double-filter
        )

        text = " ".join(seg.text for seg in segments).strip()
        return text

    def is_loaded(self) -> bool:
        return self._model is not None


def _setup_cuda_dlls() -> None:
    """
    On Windows, register CUDA/cuDNN DLL paths from installed nvidia pip packages
    or CUDA_PATH environment variable so faster-whisper/ctranslate2 can find them.
    """
    import os
    import sys

    if sys.platform != "win32":
        return

    # Check for nvidia pip packages (e.g. nvidia-cublas-cu12, nvidia-cudnn-cu12)
    # Since 'nvidia' is a namespace package, its __file__ is None. We walk sys.path instead.
    added_paths = []
    for path in sys.path:
        if not path:
            continue
        nvidia_dir = os.path.join(path, "nvidia")
        if os.path.isdir(nvidia_dir):
            for root, dirs, files in os.walk(nvidia_dir):
                if os.path.basename(root) in ("bin", "lib"):
                    if any(f.endswith(".dll") for f in files):
                        try:
                            # os.add_dll_directory is only available on Python 3.8+ on Windows
                            os.add_dll_directory(root)
                            added_paths.append(root)
                        except Exception as e:
                            print(f"[STT] Error adding DLL directory {root}: {e}")
            break

    # Prepend to PATH so CTranslate2's custom DLL loader finds them
    if added_paths:
        os.environ["PATH"] = ";".join(added_paths) + ";" + os.environ.get("PATH", "")

    # Also fall back to system CUDA_PATH if present
    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        bin_path = os.path.join(cuda_path, "bin")
        if os.path.isdir(bin_path):
            try:
                os.add_dll_directory(bin_path)
                os.environ["PATH"] = bin_path + ";" + os.environ.get("PATH", "")
            except Exception as e:
                pass


def _cleanup_old_whisper_models(current_model_size: str, target_dir: str) -> None:
    """Delete any other whisper models in the target directory to save storage."""
    import os
    import shutil
    if not os.path.isdir(target_dir):
        return
    normalized_size = current_model_size.lower()
    for name in os.listdir(target_dir):
        full_path = os.path.join(target_dir, name)
        if not os.path.isdir(full_path):
            continue
        is_whisper_model = "whisper" in name.lower() or "models--" in name.lower()
        contains_current_size = normalized_size in name.lower()
        if is_whisper_model and not contains_current_size:
            print(f"[STT] Cleaning up old unused model storage: {name}...")
            try:
                shutil.rmtree(full_path)
            except Exception as e:
                print(f"[STT] Failed to delete old model {name}: {e}")



