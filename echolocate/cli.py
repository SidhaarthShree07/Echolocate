"""
EchoLocate — Global CLI Entry Point.

Provides a highly aesthetic, interactive console UI to control the background
daemon, run diagnostics, and edit settings without touching configuration files.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_FILE = CONFIG_DIR / "default_config.yaml"
PID_FILE = Path("~/.echolocate/daemon.pid").expanduser()

# ANSI Color Codes for Aesthetic Terminal
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_GRAY = "\033[90m"


def clean_screen():
    os.system("cls" if os.name == "nt" else "clear")


def get_venv_python():
    """Return the path to the python executable in the virtual environment."""
    project_root = Path(__file__).parent.parent
    if os.name == "nt":
        pythonw = project_root / ".venv" / "Scripts" / "pythonw.exe"
        if pythonw.exists():
            return str(pythonw)
        return str(project_root / ".venv" / "Scripts" / "python.exe")
    else:
        return str(project_root / ".venv" / "bin" / "python")


def is_ollama_running() -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def ensure_ollama_running() -> bool:
    if is_ollama_running():
        return True
    
    print(f"{C_YELLOW}Ollama service not detected. Booting Ollama in background...{C_RESET}")
    try:
        kwargs = {}
        cmd = ["ollama", "serve"]
        startupinfo = None
        
        if os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            app_path = Path(local_app_data) / "Programs" / "Ollama" / "ollama app.exe"
            if app_path.exists():
                cmd = [str(app_path)]
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
            else:
                kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS + CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            **kwargs
        )
        
        # Poll Ollama until it starts
        for i in range(12):
            print(f"Waiting for Ollama to initialize... ({i+1}/12)")
            time.sleep(1.5)
            if is_ollama_running():
                print(f"{C_GREEN}[PASS] Ollama initialized successfully.{C_RESET}")
                return True
        
        print(f"{C_RED}[WARN] Ollama launched but didn't respond in time. Proceeding...{C_RESET}")
        return False
    except Exception as e:
        print(f"{C_RED}[FAIL] Could not start Ollama: {e}{C_RESET}")
        return False


def is_process_running(pid: int) -> bool:
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                creationflags=0x08000000,
                text=True
            )
            return str(pid) in output
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def get_agent_status():
    if not PID_FILE.exists():
        return "NOT RUNNING", None
    
    try:
        pid = int(PID_FILE.read_text().strip())
        if is_process_running(pid):
            return "RUNNING", pid
        else:
            PID_FILE.unlink()
            return "NOT RUNNING", None
    except Exception:
        return "UNKNOWN", None


def start_daemon(silent=False):
    status, pid = get_agent_status()
    if status == "RUNNING":
        if not silent:
            print(f"{C_GREEN}EchoLocate is already running (PID: {pid}).{C_RESET}")
        return

    # Auto-start Ollama if needed
    ensure_ollama_running()

    if not silent:
        print(f"{C_CYAN}Starting EchoLocate background agent...{C_RESET}")
    
    python_exe = get_venv_python()
    main_module = "echolocate.main"

    DAEMON_LOG = Path("~/.echolocate/daemon.log").expanduser()
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)

    try:
        if os.name == "nt":
            # On Windows, pythonw.exe runs detached without a console, surviving parent exit.
            log_file = open(DAEMON_LOG, "a", encoding="utf-8")
            subprocess.Popen(
                [python_exe, "-u", "-m", main_module],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                cwd=str(Path(__file__).parent.parent),
                creationflags=0x00000008 | 0x08000000  # DETACHED_PROCESS + CREATE_NO_WINDOW
            )
        else:
            log_file = open(DAEMON_LOG, "a", encoding="utf-8")
            subprocess.Popen(
                [python_exe, "-u", "-m", main_module],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                cwd=str(Path(__file__).parent.parent),
                start_new_session=True
            )
        
        # Wait up to 3 seconds for the child process to write the PID file
        pid = "unknown"
        for _ in range(30):
            time.sleep(0.1)
            if PID_FILE.exists():
                try:
                    pid = int(PID_FILE.read_text().strip())
                    break
                except ValueError:
                    pass
        
        if not silent:
            print(f"{C_GREEN}[PASS] EchoLocate started successfully (PID: {pid}).{C_RESET}")
            print(f"       Logs are written to: {DAEMON_LOG}")
    except Exception as e:
        if not silent:
            print(f"{C_RED}[FAIL] Failed to start agent: {e}{C_RESET}")
        sys.exit(1)


def stop_daemon(silent=False):
    status, pid = get_agent_status()
    
    # Force kill any orphaned processes matching echolocate.main to prevent multiple instances
    if os.name == "nt":
        import subprocess
        subprocess.run(
            'wmic process where "commandline like \'%echolocate.main%\' and name like \'python%\'" call terminate',
            shell=True, capture_output=True
        )
    else:
        import subprocess
        subprocess.run(["pkill", "-f", "python.*echolocate.main"], capture_output=True)

    if status == "NOT RUNNING" or pid is None:
        if not silent:
            print(f"{C_YELLOW}EchoLocate is not running.{C_RESET}")
        return

    if not silent:
        print(f"{C_CYAN}Stopping EchoLocate agent (PID: {pid})...{C_RESET}")
    
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_TERMINATE = 1
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, -1)
                ctypes.windll.kernel32.CloseHandle(handle)
                if not silent:
                    print(f"{C_GREEN}[PASS] EchoLocate background process terminated.{C_RESET}")
            else:
                if not silent:
                    print(f"{C_RED}[FAIL] Could not terminate process.{C_RESET}")
        else:
            os.kill(pid, signal.SIGTERM)
            if not silent:
                print(f"{C_GREEN}[PASS] EchoLocate stopped.{C_RESET}")
    except Exception as e:
        if not silent:
            print(f"{C_RED}[FAIL] Error stopping process: {e}{C_RESET}")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


def run_diagnostics():
    clean_screen()
    print(f"{C_BOLD}{C_CYAN}====================================================={C_RESET}")
    print(f"{C_BOLD}{C_CYAN}              ECHOLOCATE DIAGNOSTICS                 {C_RESET}")
    print(f"{C_BOLD}{C_CYAN}====================================================={C_RESET}\n")

    # 1. Ollama status
    print("Checking Ollama status...")
    if is_ollama_running():
        print(f"  Ollama Server: {C_GREEN}RUNNING / HEALTHY{C_RESET}")
    else:
        print(f"  Ollama Server: {C_RED}NOT RUNNING{C_RESET}")

    # 2. Audio status
    print("Checking Audio subsystem...")
    try:
        import sounddevice as sd
        input_device = sd.query_devices(kind='input')
        output_device = sd.query_devices(kind='output')
        print(f"  Audio Library: {C_GREEN}LOADED{C_RESET}")
        print(f"  Default Input:  {C_GREEN}{input_device.get('name', 'None')}{C_RESET}")
        print(f"  Default Output: {C_GREEN}{output_device.get('name', 'None')}{C_RESET}")
    except Exception as e:
        print(f"  Audio: {C_RED}FAILED ({e}){C_RESET}")

    # 3. GPU detection
    print("Checking GPU availability...")
    gpu_available = False
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            print(f"  GPU (CUDA):  {C_GREEN}{gpu_name} ({vram_gb:.1f} GB VRAM){C_RESET}")
            gpu_available = True
        else:
            print(f"  GPU (CUDA):  {C_YELLOW}Not available (CPU only){C_RESET}")
    except ImportError:
        # Try a lightweight check via subprocess
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], 
                                     capture_output=True, text=True, timeout=3)
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                for line in lines:
                    parts = line.split(",")
                    if len(parts) >= 2:
                        name = parts[0].strip()
                        mem = parts[1].strip()
                        print(f"  GPU (NVIDIA): {C_GREEN}{name} / {mem} VRAM{C_RESET}")
                        gpu_available = True
            else:
                print(f"  GPU:  {C_YELLOW}No NVIDIA GPU detected. Running on CPU.{C_RESET}")
        except Exception:
            print(f"  GPU:  {C_YELLOW}Could not detect GPU. Running on CPU.{C_RESET}")
    
    if gpu_available:
        print(f"  {C_CYAN}Tip: Use 'Standard' tier with GPU device=cuda for best performance.{C_RESET}")
    else:
        print(f"  {C_CYAN}Tip: Use 'Constrained' tier (single model, no swap) for CPU inference.{C_RESET}")

    # 4. Model availability
    print("Checking local Ollama models...")
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode('utf-8'))
            models = [m['name'] for m in data.get('models', [])]
            for m in ["gemma4:e2b", "gemma4:e4b"]:
                if any(m in name for name in models):
                    print(f"  Model '{m}': {C_GREEN}AVAILABLE{C_RESET}")
                else:
                    print(f"  Model '{m}': {C_YELLOW}MISSING{C_RESET}")
    except Exception as e:
        print(f"  Models: {C_RED}COULD NOT CHECK (Ollama offline){C_RESET}")

    print(f"\nPress Enter to return to main menu...")
    input()


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, default_flow_style=False)


def handle_config_cli(args):
    config = load_config()
    if args.config_action == "get":
        if args.key:
            print(config.get(args.key, ""))
        else:
            print(yaml.safe_dump(config, default_flow_style=False).strip())
    elif args.config_action == "set":
        config[args.key] = args.value
        save_config(config)
        print(f"Set '{args.key}' to {args.value}")


def _detect_gpu():
    """Detect if an NVIDIA GPU is available. Returns (available: bool, name: str)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            line = result.stdout.strip().split("\n")[0]
            parts = line.split(",")
            name = parts[0].strip() if parts else "Unknown GPU"
            mem = parts[1].strip() if len(parts) > 1 else "?"
            return True, f"{name} ({mem})"
    except Exception:
        pass
    return False, "CPU only"


def edit_config_wizard():
    while True:
        config = load_config()
        clean_screen()

        current_device = config.get('stt_device', 'cpu')
        current_tier = config.get('hardware_tier', 'standard')
        gpu_avail, gpu_name = _detect_gpu()
        device_display = f"{current_device.upper()}" + (f" ({C_YELLOW}GPU detected: {gpu_name}{C_RESET})" if gpu_avail and current_device == 'cpu' else "")

        print(f"{C_BOLD}{C_CYAN}┌────────────────────────────────────────────────────────┐{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}│               CONFIGURATION EDITOR                     │{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}└────────────────────────────────────────────────────────┘{C_RESET}")
        print(f"  {C_BOLD}[1]{C_RESET} Sandbox Folder       -> {C_GREEN}{config.get('sandbox_root', 'Not Configured')}{C_RESET}")
        print(f"  {C_BOLD}[2]{C_RESET} Activation Mode      -> {C_GREEN}{config.get('activation_mode', 'hotkey')}{C_RESET}")
        mode = config.get('activation_mode', 'hotkey')
        if mode == 'hotkey':
            print(f"  {C_BOLD}[3]{C_RESET} Activation Hotkey    -> {C_GREEN}{config.get('hotkey', 'space')}{C_RESET}")
        else:
            print(f"  {C_BOLD}[3]{C_RESET} Wake Word            -> {C_GREEN}{config.get('wake_word_model', 'hey_jarvis')}{C_RESET}")
        print(f"  {C_BOLD}[4]{C_RESET} TTS Engine           -> {C_GREEN}{config.get('tts_engine', 'kokoro')}{C_RESET}")
        print(f"  {C_BOLD}[5]{C_RESET} TTS Voice            -> {C_GREEN}{config.get('tts_voice', 'af_heart')}{C_RESET}")
        print(f"  {C_BOLD}[6]{C_RESET} Hardware Tier        -> {C_GREEN}{current_tier}{C_RESET}")
        print(f"  {C_BOLD}[7]{C_RESET} Inference Device     -> {C_GREEN}{device_display}{C_RESET}")
        print(f"  {C_BOLD}[8]{C_RESET} LLM Model            -> {C_GREEN}{config.get('llm_model', 'gemma4:e2b')}{C_RESET}")
        print(f"  {C_BOLD}[9]{C_RESET} Avatar Style         -> {C_GREEN}{config.get('avatar_style', 'girl')}{C_RESET}")
        print(f"  {C_BOLD}[B]{C_RESET} Back to Main Menu")
        print(f"{C_GRAY}──────────────────────────────────────────────────────────{C_RESET}")
        
        choice = input(f"{C_BOLD}Select setting to edit (1-9 or B): {C_RESET}").strip().upper()
        if choice == "B":
            break
        elif choice == "1":
            print(f"\nThe sandbox folder dictates which files EchoLocate can access.")
            new_path = input("Enter new absolute sandbox path: ").strip()
            if new_path:
                p = Path(new_path).expanduser().absolute()
                p.mkdir(parents=True, exist_ok=True)

                try:
                    from echolocate.mcp_server.index import is_broad_root
                    broad = is_broad_root(p)
                except Exception:
                    broad = False

                if broad:
                    print(f"\n{C_YELLOW}'{p}' is a drive root or a very broad folder.{C_RESET}")
                    print(f"{C_YELLOW}That's supported — search stays fast via a maintained SQLite index")
                    print(f"instead of walking the whole drive on every query, and OS-critical")
                    print(f"folders (Windows, Program Files, etc.) are excluded automatically.")
                    print(f"But every destructive command (move/delete) also has this entire")
                    print(f"scope as its blast radius — there's no smaller sandbox underneath it.{C_RESET}")
                    confirm = input("Type CONFIRM to proceed anyway: ").strip()
                    if confirm != "CONFIRM":
                        print(f"{C_YELLOW}Sandbox path not changed.{C_RESET}")
                        time.sleep(1.5)
                        continue

                config['sandbox_root'] = str(p).replace("\\", "/")
                save_config(config)
                print(f"{C_GREEN}Sandbox path updated.{C_RESET}")
                if broad:
                    print(f"{C_GRAY}(Index will build on next start — may take a while the first time.){C_RESET}")
                time.sleep(1)

        elif choice == "2":
            print(f"\nSelect Activation Mode:")
            print("  [1] Push-to-Talk (Hold key to speak)")
            print("  [2] Hands-Free (Wake Word: 'Hey Jarvis')")
            print("  [3] Conversation Mode (Wake Word + Follow-up)")
            mode_choice = input("Choice (1-3): ").strip()
            if mode_choice == "1":
                config['activation_mode'] = "hotkey"
                config['conversation_mode'] = False
            elif mode_choice == "2":
                config['activation_mode'] = "wakeword"
                config['conversation_mode'] = False
            elif mode_choice == "3":
                config['activation_mode'] = "wakeword"
                config['conversation_mode'] = True
            save_config(config)
            print(f"{C_GREEN}Activation mode updated.{C_RESET}")
            time.sleep(1)
        elif choice == "3":
            if config.get('activation_mode', 'hotkey') == 'hotkey':
                key = input("\nEnter hotkey trigger (e.g. 'space', 'ctrl', 'alt'): ").strip().lower()
                if key:
                    config['hotkey'] = key
                    save_config(config)
                    print(f"{C_GREEN}Hotkey updated.{C_RESET}")
                    time.sleep(1)
            else:
                print("\nSelect built-in Wake Word:")
                print("  [1] hey_jarvis")
                print("  [2] hey_friday")
                print("  [3] hey_mycroft")
                print("  [4] hey_saturday")
                w_choice = input("Choice (1-4): ").strip()
                word_map = {"1": "hey_jarvis", "2": "hey_friday", "3": "hey_mycroft", "4": "hey_saturday"}
                
                if w_choice in word_map:
                    config['wake_word_model'] = word_map[w_choice]
                    save_config(config)
                    print(f"{C_GREEN}Wake word updated.{C_RESET}")
                    time.sleep(1)
        elif choice == "4":
            print(f"\nSelect TTS Engine:")
            print("  [1] Kokoro-82M (Aesthetic high quality, local)")
            print("  [2] Piper (Fast, local)")
            tts_choice = input("Choice (1-2): ").strip()
            if tts_choice == "1":
                config['tts_engine'] = "kokoro"
            elif tts_choice == "2":
                config['tts_engine'] = "piper"
            save_config(config)
            print(f"{C_GREEN}TTS engine updated.{C_RESET}")
            time.sleep(1)
        elif choice == "5":
            voice = input("\nEnter TTS voice identifier (e.g. 'af_heart', 'af_bella'): ").strip()
            if voice:
                config['tts_voice'] = voice
                save_config(config)
                print(f"{C_GREEN}Voice updated.{C_RESET}")
                time.sleep(1)
        elif choice == "6":
            clean_screen()
            print(f"{C_BOLD}\nSelect Hardware Tier:{C_RESET}")
            print(f"")
            print(f"  {C_BOLD}[1] Standard Tier{C_RESET} (Recommended for GPU or 16GB+ RAM)")
            print(f"      Uses Gemma 4 e2b (router) + Gemma 4 e4b (reasoning).")
            print(f"      Both models are already Q4-quantized by Ollama.")
            print(f"      On GPU: both fit in VRAM simultaneously — instant responses.")
            print(f"      On CPU: models swap in/out of RAM — slow (30-60s per response).")
            print(f"")
            print(f"  {C_BOLD}[2] Constrained Tier{C_RESET} (Recommended for CPU or 4-8GB RAM)")
            print(f"      Uses only Gemma 4 e2b (7.2GB, Q4_K_M) for all tasks.")
            print(f"      Single model stays in RAM — fast responses, no swapping.")
            print(f"      Slightly less accurate on large document tasks.")
            print(f"")
            if gpu_avail:
                print(f"  {C_CYAN}Your GPU detected: {gpu_name}. Standard tier is recommended.{C_RESET}")
            else:
                print(f"  {C_YELLOW}No GPU detected. Constrained tier is recommended for CPU.{C_RESET}")
            print(f"")
            tier_choice = input("Choice (1-2): ").strip()
            if tier_choice == "1":
                config['hardware_tier'] = "standard"
            elif tier_choice == "2":
                config['hardware_tier'] = "constrained"
            if tier_choice in ("1", "2"):
                save_config(config)
                print(f"{C_GREEN}Hardware tier updated. Restart the agent for changes to take effect.{C_RESET}")
                time.sleep(2)
        elif choice == "7":
            clean_screen()
            print(f"{C_BOLD}\nSelect Inference Device:{C_RESET}")
            print(f"")
            print(f"  This controls which hardware runs the speech-to-text (Whisper) model.")
            print(f"  Ollama manages its own GPU usage independently based on what is available.")
            print(f"")
            print(f"  {C_BOLD}[1] CPU{C_RESET} — Works on all machines. Slower transcription (~1-3s).")
            print(f"  {C_BOLD}[2] CUDA (NVIDIA GPU){C_RESET} — Fast transcription (~0.2s). Requires NVIDIA GPU.")
            print(f"  {C_BOLD}[3] Auto{C_RESET} — Use GPU if available, fall back to CPU automatically.")
            print(f"")
            if gpu_avail:
                print(f"  {C_GREEN}GPU detected: {gpu_name}. CUDA or Auto recommended.{C_RESET}")
            else:
                print(f"  {C_YELLOW}No NVIDIA GPU detected. Select CPU.{C_RESET}")
            print(f"")
            dev_choice = input("Choice (1-3): ").strip()
            if dev_choice == "1":
                config['stt_device'] = "cpu"
                new_device = "cpu"
            elif dev_choice == "2":
                config['stt_device'] = "cuda"
                new_device = "cuda"
            elif dev_choice == "3":
                config['stt_device'] = "auto"
                new_device = "auto"
            else:
                new_device = None
            if new_device:
                save_config(config)
                print(f"{C_GREEN}Inference device set to '{new_device}'. Restart the agent for changes to take effect.{C_RESET}")
                time.sleep(2)
        elif choice == "8":
            clean_screen()
            print(f"{C_BOLD}\nChange LLM Model:{C_RESET}")
            print(f"")
            print(f"  Current model: {C_GREEN}{config.get('llm_model', 'gemma4:e2b')}{C_RESET}")
            print(f"")
            print(f"  Enter the Ollama model tag (e.g. 'gemma4:e2b', 'gemma3:4b', 'llama3.1:8b').")
            print(f"  Make sure the model is already pulled in Ollama (run 'ollama pull <model>').")
            print(f"  This model will be used for ALL tasks (router, document, system).")
            print(f"")
            new_model = input("Model tag (or press Enter to cancel): ").strip()
            if new_model:
                config['llm_model'] = new_model
                save_config(config)
                print(f"{C_GREEN}LLM model set to '{new_model}'. Restart the agent for changes to take effect.{C_RESET}")
                time.sleep(2)
        elif choice == "9":
            clean_screen()
            print(f"{C_BOLD}\nChange Avatar Style:{C_RESET}")
            print(f"")
            print(f"  [1] Girl (Default animated companion)")
            print(f"  [2] Male (Male companion)")
            print(f"")
            av_choice = input("Choice (1-2): ").strip()
            if av_choice == "1":
                config['avatar_style'] = "girl"
                save_config(config)
                print(f"{C_GREEN}Avatar style set to 'girl'.{C_RESET}")
                time.sleep(1.5)
            elif av_choice == "2":
                config['avatar_style'] = "male"
                save_config(config)
                print(f"{C_GREEN}Avatar style set to 'male'.{C_RESET}")
                time.sleep(1.5)


def interactive_menu():
    while True:
        config = load_config()
        clean_screen()
        print(f"{C_BOLD}{C_CYAN}┌────────────────────────────────────────────────────────┐{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}│               E C H O L O C A T E                      │{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}│      Voice-Driven Offline Accessibility Agent          │{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}└────────────────────────────────────────────────────────┘{C_RESET}")
        print(f"  {C_BOLD}Sandbox:{C_RESET}   {config.get('sandbox_root', 'Not Configured')}")
        is_conv = str(config.get('conversation_mode', 'false')).strip().lower() in {'1', 'true', 'yes'}
        amode = config.get('activation_mode', 'hotkey')
        if amode == 'wakeword' and is_conv:
            mode_disp = "conversation (Wake Word + Follow-up)"
            trigger_disp = config.get('wake_word_model', 'hey_jarvis')
        elif amode == 'wakeword':
            mode_disp = "wakeword"
            trigger_disp = config.get('wake_word_model', 'hey_jarvis')
        else:
            mode_disp = "hotkey"
            trigger_disp = config.get('hotkey', 'space')

        print(f"  {C_BOLD}Mode:{C_RESET}      {mode_disp} (Trigger: {trigger_disp})")
        print(f"  {C_BOLD}Hardware:{C_RESET}  {config.get('hardware_tier', 'standard')} tier")
        print(f"  {C_BOLD}Avatar Style:{C_RESET} {config.get('avatar_style', 'girl')}")
        print(f"{C_GRAY}──────────────────────────────────────────────────────────{C_RESET}")
        print(f"  {C_BOLD}[1]{C_RESET} Start EchoLocate Agent")
        print(f"  {C_BOLD}[2]{C_RESET} Edit Settings / Configuration")
        print(f"  {C_BOLD}[3]{C_RESET} Run System Diagnostics (Check Ollama & Audio)")
        print(f"  {C_BOLD}[4]{C_RESET} Exit")
        print(f"{C_GRAY}──────────────────────────────────────────────────────────{C_RESET}")

        choice = input(f"{C_BOLD}Enter choice (1-4): {C_RESET}").strip()
        if choice == "1":
            ensure_ollama_running()
            from echolocate.main import main as run_main
            try:
                run_main()
            except (KeyboardInterrupt, SystemExit):
                print(f"\n{C_YELLOW}EchoLocate stopped.{C_RESET}")
            time.sleep(1.5)
        elif choice == "2":
            edit_config_wizard()
        elif choice == "3":
            run_diagnostics()
        elif choice == "4":
            print("\nGoodbye!")
            break


def main():
    try:
        interactive_menu()
    except (KeyboardInterrupt, EOFError):
        print(f"\n{C_YELLOW}Exiting EchoLocate CLI.{C_RESET}")


if __name__ == "__main__":
    main()
