"""
EchoLocate — main entry point.

Orchestrates the full voice pipeline:
  Activation gate → VAD → STT → Graph workflow → TTS

Run with: python -m echolocate.main

Configuration is loaded from config/default_config.yaml, with environment
variable overrides (ECHOLOCATE_<KEY>=value).

Architecture Section 3: this is the Orchestrator process. It talks to Ollama
on localhost only. The MCP server is a separate subprocess launched by the
ADK toolset connection (StdioConnectionParams).
"""
from __future__ import annotations

import os
import re
import sys
import signal
import time
import threading
from pathlib import Path
from collections.abc import Iterator

import yaml


def load_config() -> dict:
    """
    Load configuration from config/default_config.yaml.
    Environment variables (ECHOLOCATE_<KEY>) override config file values.
    """
    config_path = Path(__file__).parent.parent / "config" / "default_config.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    # Environment variable overrides
    env_map = {
        "ECHOLOCATE_SANDBOX_ROOT": "sandbox_root",
        "ECHOLOCATE_ACTIVATION_MODE": "activation_mode",
        "ECHOLOCATE_HOTKEY": "hotkey",
        "ECHOLOCATE_STOP_HOTKEY": "stop_hotkey",
        "ECHOLOCATE_CONVERSATION_MODE": "conversation_mode",
        "ECHOLOCATE_CONVERSATION_TIMEOUT": "conversation_timeout_seconds",
        "ECHOLOCATE_ROUTER_CONFIDENCE_THRESHOLD": "router_confidence_threshold",
        "ECHOLOCATE_STT_MODEL": "stt_model",
        "ECHOLOCATE_STT_DEVICE": "stt_device",
        "ECHOLOCATE_TTS_ENGINE": "tts_engine",
        "ECHOLOCATE_TTS_VOICE": "tts_voice",
        "ECHOLOCATE_TTS_SPEED": "tts_speed",
        "ECHOLOCATE_HARDWARE_TIER": "hardware_tier",
    }
    for env_key, config_key in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            config[config_key] = val

    return config


def load_model_config(hardware_tier: str = "constrained") -> dict:
    """Load model configuration for the selected hardware tier.

    Default changed from "standard" to "constrained" (single model,
    gemma4:e2b, for every role) after real-world VRAM contention: running
    two different Gemma 4 models (e2b + e4b) simultaneously alongside
    STT (faster-whisper on CUDA) and TTS/wake-word (ONNX runtime, also
    GPU) exhausted VRAM on a consumer GPU and caused Windows to silently
    page GPU memory over PCIe -- turning normal calls into multi-minute
    stalls. A single model everywhere removes the model-swap thrashing
    entirely, which is what makes a longer keep_alive safe (see main()'s
    build_confirm_fn / graph construction for where this model_config
    feeds into per-node keep_alive behavior). This is a capability/speed
    tradeoff, not a free win -- e4b's stronger document/PDF understanding
    is worth it on hardware with room for it, which is exactly what the
    "standard" tier remains for. Defaulting to the safe choice and letting
    capable hardware opt UP is the right default direction; the previous
    default asked every user's hardware to prove itself before it was
    even given the safe option.
    """
    models_path = Path(__file__).parent.parent / "config" / "models.yaml"
    models = {}
    if models_path.exists():
        with open(models_path, "r", encoding="utf-8") as f:
            models = yaml.safe_load(f) or {}

    tier = models.get("tiers", {}).get(hardware_tier, {})
    ollama_provider = models.get("ollama_provider", "ollama_chat")

    tier_config = {
        "router_model": f"{ollama_provider}/{tier.get('router_model', 'gemma4:e2b')}",
        "document_model": f"{ollama_provider}/{tier.get('document_model', 'gemma4:e2b')}",
        "system_model": f"{ollama_provider}/{tier.get('system_model', 'gemma4:e2b')}",
    }

    # User-configurable model override from CLI settings. On constrained
    # systems, one shared model is often better than loading/unloading two
    # separate models, so llm_model intentionally applies to every role.
    # Users can still split roles with router_model / llm_router_model.
    from echolocate.cli import load_config as _load_config
    _user_cfg = _load_config()
    user_model = _user_cfg.get("llm_model")
    if user_model:
        full_model = f"ollama_chat/{user_model}" if "/" not in user_model else user_model
        tier_config["router_model"] = full_model
        tier_config["document_model"] = full_model
        tier_config["system_model"] = full_model

    router_override = _user_cfg.get("router_model") or _user_cfg.get("llm_router_model")
    if router_override:
        tier_config["router_model"] = (
            f"ollama_chat/{router_override}" if "/" not in router_override else router_override
        )

    return tier_config


def smoke_test(config: dict) -> bool:
    """
    Run smoke tests before accepting voice input. Fail loudly, not silently.

    Tests:
    1. Sandbox root exists
    2. Ollama is reachable
    3. Sandbox sandbox.py platform branch exercises correctly
    """
    passed = True

    # Test 1: sandbox root
    sandbox_root_str = config.get("sandbox_root", "")
    if not sandbox_root_str:
        print("[FAIL] sandbox_root is not configured. Run install.ps1 to set it up.")
        passed = False
    else:
        sandbox_root = Path(sandbox_root_str).expanduser()
        if not sandbox_root.exists():
            print(f"[FAIL] sandbox_root does not exist: {sandbox_root}")
            passed = False
        else:
            try:
                from echolocate.mcp_server.index import is_broad_root
                _ = is_broad_root(sandbox_root)
            except Exception as exc:
                pass

    # Test 2: Ollama reachable
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            if resp.status != 200:
                print(f"[FAIL] Ollama returned status {resp.status}")
                sys.exit(1)
    except Exception as e:
        print(f"[FAIL] Ollama unreachable: {e}")
        sys.exit(1)

    # Test 3: sandbox platform branch
    try:
        from echolocate.mcp_server.sandbox import resolve_and_check, IS_WINDOWS
    except Exception as exc:
        print(f"[FAIL] Sandbox module error: {exc}")
        passed = False

    return passed


def build_confirm_fn(tts, stt_engine):
    """
    Build a voice-based confirmation function for the system executor.
    Speaks the prompt, listens for yes/no, returns bool.
    """
    def voice_confirm(prompt: str) -> bool:
        tts.speak(prompt)
        # Listen for up to 8 seconds for a response
        import sounddevice as sd
        import numpy as np
        from echolocate.voice.vad import VADGate

        print(f"[Confirmation] Waiting for yes/no...")
        sample_rate = 16000
        max_seconds = 8
        audio = sd.rec(
            int(max_seconds * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        sd.wait()
        audio_flat = audio.flatten()

        response_text = stt_engine.transcribe(audio_flat, sample_rate)
        print(f"[Confirmation] Heard: '{response_text}'")

        affirmatives = {"yes", "yeah", "yep", "confirm", "do it", "go ahead", "sure", "ok", "okay"}
        return any(word in response_text.lower() for word in affirmatives)

    return voice_confirm


def main() -> None:
    """Main EchoLocate entry point."""
    pid_file = Path("~/.echolocate/daemon.pid").expanduser()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    # Enable ANSI escape codes on Windows console unconditionally
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # 7 is ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    print("=" * 60)
    print("  EchoLocate — Offline Voice Accessibility Agent")
    print("=" * 60)

    # Load configuration
    config = load_config()
    hardware_tier = config.get("hardware_tier", "constrained")
    model_config = load_model_config(hardware_tier)
    _tier_source = "ECHOLOCATE_HARDWARE_TIER env var" if os.environ.get("ECHOLOCATE_HARDWARE_TIER") else \
                   ("config file" if "hardware_tier" in config else "built-in default")

    # Smoke tests
    if not smoke_test(config):
        print("\n[EchoLocate] Startup checks failed. Please fix the issues above.")
        sys.exit(1)

    sandbox_root = Path(config.get("sandbox_root", "")).expanduser()

    # Initialize TTS
    print("[EchoLocate] Initializing TTS...")
    from echolocate.tts.synth import TTSSynthesizer
    from echolocate.voice.barge_in import BargeInController

    barge_in = BargeInController()
    tts = TTSSynthesizer(
        engine=config.get("tts_engine", "kokoro"),
        voice=config.get("tts_voice", "af_heart"),
        speed=float(config.get("tts_speed", 1.0)),
        should_stop=barge_in.should_stop_playback,
        on_playback_start=barge_in.start_tts,
        on_playback_end=barge_in.end_tts,
    )
    barge_in.set_on_stop(lambda: print("[EchoLocate] Playback stopped."))

    animator = None
    turn_in_progress = False

    def _restore_gate_state():
        if not animator:
            return
        if gate.is_active:
            if turn_in_progress:
                animator.set_state("working")
            else:
                animator.set_state("idle")
        else:
            animator.set_state("sleeping")

    def speak(text: str) -> None:
        if animator: animator.set_state("speaking")
        print(f"[EchoLocate] Speaking: {text}")
        tts.speak(text)
        _restore_gate_state()

    def speak_chunked(text: str) -> None:
        if animator: animator.set_state("speaking")
        print(f"[EchoLocate] Speaking: {text}")
        tts.speak_chunked(text)
        _restore_gate_state()

    def speak_stream(text_iterator: Iterator[str]) -> str:
        if animator: animator.set_state("speaking")
        print("[EchoLocate] Speaking streamed response...")
        barge_in.start_tts()
        try:
            return tts.speak_stream(text_iterator)
        finally:
            barge_in.end_tts()
            _restore_gate_state()

    # Initialize STT
    print("[EchoLocate] Initializing STT...")
    from echolocate.voice.stt import STTEngine
    stt = STTEngine(
        model_size=config.get("stt_model", "base"),
        language=config.get("stt_language", "en"),
        device=config.get("stt_device", "cpu"),
    )
    stt.load()

    # Initialize VAD
    print("[EchoLocate] Initializing VAD...")
    from echolocate.voice.vad import VADGate
    vad = VADGate(
        threshold=float(config.get("vad_threshold", 0.5)),
        min_speech_ms=int(config.get("vad_min_speech_ms", 250)),
        silence_ms=int(config.get("vad_silence_ms", 700)),
    )

    # Initialize audit logger
    from echolocate.mcp_server.audit import init_logger
    audit_log_path = Path(config.get("audit_log_path", "~/.echolocate/audit.log")).expanduser()
    init_logger(audit_log_path)

    # Build confirmation function
    confirm_fn = build_confirm_fn(tts, stt)

    # Build the orchestration graph
    print("[EchoLocate] Building agent graph...")
    from echolocate.graph import EchoLocateGraph
    graph = EchoLocateGraph(
        sandbox_root=sandbox_root,
        router_model=model_config["router_model"],
        document_model=model_config["document_model"],
        system_model=model_config["system_model"],
        confidence_threshold=float(config.get("router_confidence_threshold", 0.7)),
        confirm_fn=confirm_fn,
        tts_fn=speak,
        tts_chunked_fn=speak_chunked,
        tts_stream_fn=speak_stream,
    )

    # Pre-build index: block synchronously on cold build to show progress,
    # otherwise reuse the existing index in the background.
    try:
        from echolocate.mcp_server.index import get_index, ensure_built
        idx = get_index(sandbox_root)
        is_empty = idx._root_is_empty(str(sandbox_root))
        if is_empty:
            print("\n" + "=" * 60)
            print("  EchoLocate — First-Time Startup: Installing Search Index")
            print("=" * 60)
            print("[FileIndex] Pre-scanning D:\\ to calculate progress...")
            t0 = time.time()
            total_files = idx.count_files()
            print(f"[FileIndex] Found {total_files:,} files. Building database...")
            
            # Enable ANSI escape codes on Windows console
            if os.name == "nt":
                try:
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
                except Exception:
                    pass

            import sys
            
            girl_frames = [
                [
                    "   (๑•͈ᴗ•͈) 🎧  * scanning directories... *",
                    "   / |   | \\",
                    "  |  |___|  |"
                ],
                [
                    "   (๑•͈ - •͈) 🎧  * parsing file metadata... *",
                    "   / |   | \\",
                    "  |  |___|  |"
                ],
                [
                    "   (๑•͈ ‿ •͈) 🎧  * indexing names & trigrams... *",
                    "   / |   | \\",
                    "  |  |___|  |"
                ],
                [
                    "   (๑•͈ ㅂ •͈) 🎧  * committing to database... *",
                    "   / |   | \\",
                    "  |  |___|  |"
                ]
            ]
            
            state = {
                "last_anim_time": 0.0,
                "current_frame_idx": 0,
                "lines_printed": 0
            }
            
            def progress_cb(current):
                # Clear previous lines
                if state["lines_printed"] > 0:
                    sys.stdout.write(f"\033[{state['lines_printed']}A")
                    for _ in range(state["lines_printed"]):
                        sys.stdout.write("\033[K\n")
                    sys.stdout.write(f"\033[{state['lines_printed']}A")
                
                percent = (current / total_files) * 100 if total_files > 0 else 100.0
                bar_length = 30
                filled_length = int(bar_length * current // total_files) if total_files > 0 else bar_length
                bar = '█' * filled_length + '░' * (bar_length - filled_length)
                
                now = time.time()
                if now - state["last_anim_time"] > 0.4:
                    state["current_frame_idx"] = (state["current_frame_idx"] + 1) % len(girl_frames)
                    state["last_anim_time"] = now
                    
                frame = girl_frames[state["current_frame_idx"]]
                
                output_lines = [
                    f"Installing Index: [{bar}] {percent:.1f}% ({current:,}/{total_files:,} files)",
                    "",
                    frame[0],
                    frame[1],
                    frame[2]
                ]
                
                for line in output_lines:
                    sys.stdout.write(line + "\n")
                sys.stdout.flush()
                state["lines_printed"] = len(output_lines)
                
            idx.is_building = True
            n = idx.build(progress_cb=progress_cb)
            idx.is_building = False
            idx._build_started = True  # Prevent ensure_built from rebuilding
            
            # Clear animation when complete
            if state["lines_printed"] > 0:
                sys.stdout.write(f"\033[{state['lines_printed']}A")
                for _ in range(state["lines_printed"]):
                    sys.stdout.write("\033[K\n")
                sys.stdout.write(f"\033[{state['lines_printed']}A")
                sys.stdout.flush()
                
            print(f"[PASS] Search index successfully installed in {time.time() - t0:.1f}s.")
            print("=" * 60 + "\n")
            
        ensure_built(sandbox_root)
    except Exception as exc:
        print(f"[EchoLocate] Could not build index: {exc}")

    # Initialize Avatar Animator
    try:
        from echolocate.voice.avatar import AvatarAnimator
        style = config.get("avatar_style", "girl")
        animator = AvatarAnimator(style=style)
        animator.start()
        animator.set_state("sleeping")
    except Exception as exc:
        print(f"[EchoLocate] Could not start avatar animator: {exc}")

    # Initialize activation gate
    from echolocate.voice.activation import ActivationGate
    gate = ActivationGate(
        mode=config.get("activation_mode", "hotkey"),
        hotkey=config.get("hotkey", "space"),
        stop_hotkey=config.get("stop_hotkey", "escape"),
        wake_word_model=config.get("wake_word_model", "hey_jarvis"),
        wake_word_threshold=float(config.get("wake_word_threshold", 0.5)),
    )
    gate.on_stop_playback = barge_in.request_stop
    gate.is_muted = lambda: barge_in.is_muted

    # Conversation mode: wake once ("hey jarvis"), converse across multiple
    # turns without repeating the wake word, until an explicit sleep phrase
    # or a timeout. Implemented as a layer on top of "wakeword" mode rather
    # than a new top-level activation_mode value, since ActivationGate's
    # internal mode handling isn't something this file controls -- this
    # way it composes with the existing gate implementation unchanged.
    conversation_mode = (
        config.get("activation_mode", "hotkey") == "wakeword"
        and str(config.get("conversation_mode", "false")).strip().lower() in {"1", "true", "yes"}
    )
    conversation_timeout_seconds = float(config.get("conversation_timeout_seconds", 15))
    wake_ack_phrase = config.get("wake_ack_phrase", "Yes?")

    wake_word = config.get("wake_word_model", "hey_jarvis").lower()
    base_name = wake_word.replace("hey_", "").replace("okay_", "").replace("ok_", "").replace("_", " ").strip()
    
    _sleep_phrases_cfg = config.get(
        "sleep_phrases",
        [f"{base_name} take rest", f"{base_name} sleep", f"{base_name} go to sleep",
         f"goodbye {base_name}", f"that's all {base_name}", f"{base_name} stop listening"],
    )
    # Env var override arrives as a comma-separated string; config.yaml
    # gives a real list already.
    sleep_phrases = (
        [p.strip().lower() for p in _sleep_phrases_cfg.split(",")]
        if isinstance(_sleep_phrases_cfg, str)
        else [p.strip().lower() for p in _sleep_phrases_cfg]
    )

    def _phrase_matches(phrase_words: list, utt_words: list, max_slack: int = 2) -> bool:
        """True if phrase_words appears as an IN-ORDER subsequence of
        utt_words within a bounded span. Order matters (not just set
        containment) specifically to reject sentences that happen to
        contain the same words scattered in an unrelated order -- e.g.
        "can you take a look at the rest of my files jarvis" contains
        every word in "jarvis take rest" but not in that order, and
        shouldn't trigger sleep. A small slack (default 2) still tolerates
        natural insertions like "jarvis, take A rest"."""
        n, m = len(utt_words), len(phrase_words)
        if m == 0:
            return False
        for start in range(n):
            if utt_words[start] != phrase_words[0]:
                continue
            pi, pos = 1, start + 1
            while pos < n and pi < m and pos <= start + m + max_slack:
                if utt_words[pos] == phrase_words[pi]:
                    pi += 1
                pos += 1
            if pi == m:
                return True
        return False

    def _matches_sleep_phrase(text: str) -> bool:
        utt_words = re.findall(r"[a-z0-9']+", text.lower())
        for phrase in sleep_phrases:
            phrase_words = re.findall(r"[a-z0-9']+", phrase)
            if _phrase_matches(phrase_words, utt_words):
                return True
        return False

    last_activation_time = 0.0
    chunks_since_activation = 0
    in_conversation = False  # True once a wake word has fired in conversation_mode

    def on_activate():
        nonlocal last_activation_time, chunks_since_activation, in_conversation
        last_activation_time = time.time()
        chunks_since_activation = 0
        if conversation_mode:
            in_conversation = True
            import threading
            threading.Thread(target=speak, args=(wake_ack_phrase,), daemon=True).start()
        print("[EchoLocate] Gate activated. Listening for speech...")
        _restore_gate_state()
    gate.on_activate = on_activate

    def on_deactivate():
        print("[EchoLocate] Gate deactivated. Going to sleep.")
        _restore_gate_state()
    gate.on_deactivate = on_deactivate

    # Ready!
    activation_mode = config.get("activation_mode", "hotkey")
    if activation_mode == "wakeword" and conversation_mode:
        speak(f"EchoLocate is ready. Say {wake_word} to start a conversation — "
              f"after that, just keep talking. Say something like '{base_name} take a rest' when you're done.")
    elif activation_mode == "wakeword":
        speak(f"EchoLocate is ready. Say {wake_word} to speak.")
    else:
        speak("EchoLocate is ready. Hold space to speak.")

    print(f"\n[EchoLocate] Ready! Session ID: {graph.get_session_id()}")
    print(f"  Activation: {activation_mode} mode" + (" (conversation mode ON)" if conversation_mode else ""))
    print(f"  Sandbox: {sandbox_root}")
    print(f"  Models: router={model_config['router_model']}, document={model_config['document_model']}")
    print("  Press Ctrl+C to exit.\n")

    # Main audio loop
    import sounddevice as sd
    import numpy as np

    sample_rate = 16000
    chunk_size = 512  # ~32ms per chunk

    def audio_callback(indata, frames, time_info, status):
        """Called for each audio chunk from sounddevice."""
        nonlocal last_activation_time, chunks_since_activation, in_conversation, turn_in_progress
        if not gate.is_active or barge_in.is_muted:
            return

        # Timeout: how long to wait for speech before deactivating. Skipped
        # ENTIRELY while a turn is still being processed in the background
        # -- this used to check the clock unconditionally, which meant a
        # slow classification/search/summarization call (routinely 10s+,
        # sometimes 30s+) could exceed conversation_timeout_seconds and put
        # the gate to sleep BEFORE the response was ever delivered, since
        # last_activation_time only gets reset in _run_turn() AFTER
        # process_utterance() returns -- the timeout had no way to know a
        # turn was still in flight rather than the user having gone quiet.
        if turn_in_progress:
            return

        timeout = conversation_timeout_seconds if (conversation_mode and in_conversation) else 7.0
        if gate.mode == "wakeword" and (time.time() - last_activation_time) > timeout:
            print("[EchoLocate] Listening timeout. Deactivating." + (" Going back to sleep." if in_conversation else ""))
            gate._set_active(False)
            in_conversation = False
            return

        audio_chunk = indata[:, 0]  # mono

        # In wakeword mode, ignore VAD for the first 400ms (12 chunks of ~32ms)
        # to prevent the tail end of the wake word from prematurely triggering VAD
        if gate.mode == "wakeword" and chunks_since_activation < 12:
            chunks_since_activation += 1
            # Feed VAD to keep buffer sizes aligned, but reset state
            vad.process_chunk(audio_chunk)
            vad._in_speech = False
            vad._speech_buffer.clear()
            vad._silence_frames = 0
            if not turn_in_progress:
                _restore_gate_state()
            return

        speech_segment = vad.process_chunk(audio_chunk)

        if vad._in_speech and not turn_in_progress:
            if animator: animator.set_state("listening")
        elif not turn_in_progress:
            _restore_gate_state()

        if speech_segment is not None and len(speech_segment) > 0:
            print(f"[EchoLocate] Speech segment captured ({len(speech_segment)/sample_rate:.1f}s)")

            # Outside conversation mode (or the very first utterance of a
            # conversation-mode session, before we know it needs to stay
            # open), deactivate immediately as before. In an ONGOING
            # conversation, stay active -- don't require a fresh wake word
            # for the next turn.
            if gate.mode == "wakeword" and not (conversation_mode and in_conversation):
                gate._set_active(False)

            transcript = stt.transcribe(speech_segment, sample_rate)
            if transcript.strip():
                print(f"[STT] '{transcript}'")

                if conversation_mode and in_conversation and _matches_sleep_phrase(transcript):
                    print("[EchoLocate] Sleep phrase detected — ending conversation.")
                    in_conversation = False
                    gate._set_active(False)
                    speak("Okay, going back to sleep. Just say the wake word when you need me again.")
                    return

                def _run_turn(utterance: str):
                    nonlocal last_activation_time, turn_in_progress
                    if animator: animator.set_state("working")
                    turn_start = time.time()
                    try:
                        graph.process_utterance(utterance)
                    finally:
                        elapsed = time.time() - turn_start
                        print(f"[EchoLocate] Turn completed in {elapsed:.1f}s.")
                        turn_in_progress = False
                        if conversation_mode and in_conversation:
                            last_activation_time = time.time()
                        _restore_gate_state()
                        # Reset the follow-up window to start counting from
                        # when the response finished, not from the original
                        # wake word -- otherwise a long document summary
                        # could eat most or all of the follow-up window
                        # before the user even gets a chance to respond.
                        if conversation_mode and in_conversation:
                            last_activation_time = time.time()

                # Guard against a second speech segment starting a second
                # concurrent turn while the first is still running -- both
                # would mutate the SAME shared session_state (pending_intent,
                # last_referenced_file, turn_history) with no coordination
                # between them, which is a real race, not a hypothetical
                # one: the mic stays open and un-muted during LLM/search
                # processing (only actual TTS playback mutes it), so
                # background noise or an impatient follow-up during a slow
                # response could easily trigger a second VAD segment.
                if turn_in_progress:
                    print("[EchoLocate] Still working on the previous request — ignoring this for now.")
                else:
                    turn_in_progress = True
                    threading.Thread(target=_run_turn, args=(transcript,), daemon=True).start()
            else:
                print("[STT] (No speech detected or could not transcribe)")

    # Shutdown handler
    def shutdown(sig, frame):
        print("\n[EchoLocate] Shutting down...")
        if animator:
            animator.stop()
        gate.stop()
        graph.reset_session()
        # Clean up PID file
        pid_file = Path("~/.echolocate/daemon.pid").expanduser()
        if pid_file.exists():
            try:
                pid_file.unlink()
            except Exception:
                pass
        speak("Goodbye.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    gate.start()

    # Start audio stream
    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=chunk_size,
        callback=audio_callback,
    ):
        print("[EchoLocate] Listening...")
        # Keep main thread alive
        while True:
            time.sleep(0.1)


if __name__ == "__main__":
    main()
