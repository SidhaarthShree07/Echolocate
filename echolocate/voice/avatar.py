from __future__ import annotations
import ctypes
import os
import sys
import threading
import time
from pathlib import Path

# ===========================================================================
# CONFIGURATION
# ===========================================================================
AVATAR_STYLE = "girl"   # "girl" or "boy"
FPS = 10                # Animation speed
AVATAR_COLUMNS = 35     # Canvas width for term_image (true-to-size pixel art 1:1 mapping)

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
STATE_COLORS = {
    "idle": "\033[90m",       # Grey
    "listening": "\033[96m",  # Cyan
    "speaking": "\033[92m",   # Green
    "sleeping": "\033[94m",   # Blue
    "working": "\033[93m",    # Yellow
}

VALID_STATES = ("idle", "listening", "speaking", "sleeping", "working")

def _enable_windows_ansi() -> None:
    if os.name != "nt": return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass

def _hide_cursor(stream) -> None:
    stream.write("\033[?25l")
    stream.flush()

def _show_cursor(stream) -> None:
    stream.write("\033[?25h")
    stream.flush()


class AvatarStdoutWrapper:
    """Wraps a write stream (stdout/stderr) to draw the avatar at the bottom while logs flow above."""
    def __init__(self, original_stream, animator):
        self.original_stream = original_stream
        self.animator = animator
        self.buffer = []
        
    def write(self, data):
        self.buffer.append(data)
        if "\n" in data or "\r" in data:
            self.flush()
            
    def flush(self):
        if self.buffer:
            full_data = "".join(self.buffer)
            self.buffer.clear()
            
            # Lock the player to prevent tearing
            player = self.animator.player
            if player and player._thread:
                with player._lock:
                    player._erase_previous()
                    self.original_stream.write(full_data)
                    player.redraw_current()
            else:
                self.original_stream.write(full_data)
                
        self.original_stream.flush()


class AvatarAnimator:
    def __init__(self, style: str = AVATAR_STYLE, fps: int = FPS):
        if style not in ("girl", "boy", "male"):
            style = "girl"
        if style == "male": style = "boy"
            
        self.style = style
        self.fps = fps  # Not strictly used now, we use GIF inherent durations
        
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.stream = sys.stdout

        self._state = "idle"
        self._stdout_wrapper = None
        self._stderr_wrapper = None
        
        self.player = None
        self.states_data = {}
        self._load_images()

    def _load_images(self):
        try:
            from .avatar_render import load_gif_frames, terminal_supports_graphics
            assets_dir = Path(__file__).parent.parent.parent / "assets"
            for state in VALID_STATES:
                filepath = assets_dir / f"{self.style}_{state}.gif"
                if not filepath.exists():
                    filepath = assets_dir / f"{self.style}_idle.gif"
                
                if filepath.exists():
                    try:
                        frames, durations = load_gif_frames(str(filepath))
                        self.states_data[state] = (frames, durations)
                    except Exception as e:
                        print(f"Error loading {filepath}: {e}")
                        
            # Alias working to idle frames
            if "idle" in self.states_data:
                self.states_data["working"] = self.states_data["idle"]
        except ImportError:
            self.states_data = {}

    def set_state(self, state: str) -> None:
        if state not in VALID_STATES:
            raise ValueError(f"Unknown state {state!r}.")
        
        if state != self._state:
            self._state = state
            if self.player:
                data = self.states_data.get(state)
                if data:
                    frames, durs = data
                    
                    label_text = f"{BOLD}[ {state.upper()} ]{RESET}".center(AVATAR_COLUMNS + 10)
                    color = STATE_COLORS.get(state, "")
                    formatted_label = f"\033[2K\r{color}{label_text}{RESET}"
                    
                    self.player.swap(frames, durs, cols=AVATAR_COLUMNS, label=formatted_label)

    def start(self) -> None:
        if self.player: return
        
        # Ensure raw stdout can handle Unicode half-blocks on Windows
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
            
        _enable_windows_ansi()
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.stream = self.original_stdout
        _hide_cursor(self.stream)
        
        self.stream.write("\n" * (AVATAR_COLUMNS // 2))
        
        self._stdout_wrapper = AvatarStdoutWrapper(self.original_stdout, self)
        self._stderr_wrapper = AvatarStdoutWrapper(self.original_stderr, self)
        sys.stdout = self._stdout_wrapper
        sys.stderr = self._stderr_wrapper
        
        from .avatar_render import TerminalGifPlayer, terminal_supports_graphics
        mode = "sixel" if terminal_supports_graphics() else "braille"
        
        data = self.states_data.get(self._state)
        if data:
            frames, durs = data
            label_text = f"{BOLD}[ {self._state.upper()} ]{RESET}".center(AVATAR_COLUMNS + 10)
            color = STATE_COLORS.get(self._state, "")
            formatted_label = f"\033[2K\r{color}{label_text}{RESET}"
            
            self.player = TerminalGifPlayer(
                frames, durs, 
                cols=AVATAR_COLUMNS, mode=mode, 
                stream=self.original_stdout,
                label=formatted_label,
                dot_color=(90, 230, 220)
            )
            self.player.start()

    def stop(self) -> None:
        if self.player:
            self.player.stop(clear=True)
            self.player = None
            
        if sys.stdout is self._stdout_wrapper:
            sys.stdout = self.original_stdout
        if sys.stderr is self._stderr_wrapper:
            sys.stderr = self.original_stderr
            
        _show_cursor(self.stream)

    def __enter__(self) -> "AvatarAnimator":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
