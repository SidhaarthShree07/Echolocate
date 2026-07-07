from __future__ import annotations

import io
import shutil
import sys
import threading
import time
from typing import List, Optional, Tuple

from PIL import Image, ImageSequence

RESET = "\033[0m"


# --------------------------------------------------------------------------
# Frame loading — decode + fully composite every GIF frame up front.
# --------------------------------------------------------------------------
def load_gif_frames(path: str) -> Tuple[List[Image.Image], List[float]]:
    """
    Returns (frames, durations_in_seconds).

    Composites each frame onto a running RGBA canvas so GIFs that only store
    the changed pixels per frame (common "optimized" GIFs) still decode
    correctly instead of showing ghosting/garbage on non-first frames.
    """
    im = Image.open(path)
    frames: List[Image.Image] = []
    durations: List[float] = []
    canvas = Image.new("RGBA", im.size, (0, 0, 0, 0))

    for frame in ImageSequence.Iterator(im):
        rgba = frame.convert("RGBA")
        canvas = Image.alpha_composite(canvas, rgba)
        frames.append(canvas.copy())
        durations.append(frame.info.get("duration", 100) / 1000.0)

    return frames, durations


def _flatten_on_bg(img: Image.Image, bg=(0, 0, 0)) -> Image.Image:
    """Flatten transparency onto a solid background (terminal bg is unknown,
    so pick a sane default — override `bg` to match your terminal theme)."""
    if img.mode != "RGBA":
        return img.convert("RGB")
    flat = Image.new("RGB", img.size, bg)
    flat.paste(img, mask=img.split()[3])
    return flat


# --------------------------------------------------------------------------
# Tier 1: half-block truecolor renderer (universal fallback)
# --------------------------------------------------------------------------
def image_to_halfblocks(img: Image.Image, cols: int, char_aspect: float = 2.0) -> str:
    """
    Render one frame as a block of ANSI truecolor half-block characters.

    `char_aspect` is your terminal font's height/width ratio (~2.0 for most
    monospace fonts). It's what makes the output look correctly proportioned
    instead of squished/stretched — tune it slightly if your font differs.
    """
    img = _flatten_on_bg(img)
    src_aspect = img.height / img.width  # source pixels are square; this is what we must preserve

    # Each printed row = 2 pixel rows (half-block trick) and 1 char cell is
    # `char_aspect` times taller than wide, so:
    #   rows_px = 2 * cols * src_aspect / char_aspect
    rows_px = round(2 * cols * src_aspect / char_aspect)
    rows_px -= rows_px % 2                # must be even: 2 pixel rows per printed row
    rows_px = max(rows_px, 2)

    img = img.resize((cols, rows_px), Image.LANCZOS)
    px = img.load()

    out_lines = []
    for y in range(0, rows_px, 2):
        parts = []
        for x in range(cols):
            r1, g1, b1 = px[x, y]
            r2, g2, b2 = px[x, y + 1]
            parts.append(f"\033[38;2;{r1};{g1};{b1}m\033[48;2;{r2};{g2};{b2}m\u2580")
        parts.append(RESET)
        out_lines.append("".join(parts))
    return "\n".join(out_lines)


# --------------------------------------------------------------------------
# Tier 1b: Braille dot-matrix renderer (the "LED panel" look)
# --------------------------------------------------------------------------
_BRAILLE_BASE = 0x2800
_DOT_BITS = [
    [0x01, 0x08],
    [0x02, 0x10],
    [0x04, 0x20],
    [0x40, 0x80],
]

def image_to_braille(
    img: Image.Image,
    cols: int,
    char_aspect: float = 2.0,
    color: Optional[Tuple[int, int, int]] = None,
) -> str:
    img = _flatten_on_bg(img)
    gray = img.convert("L")
    src_aspect = img.height / img.width

    dot_cols = cols * 2
    dot_rows = round(4 * cols * src_aspect / char_aspect)
    dot_rows -= dot_rows % 4
    dot_rows = max(dot_rows, 4)

    gray = gray.resize((dot_cols, dot_rows), Image.LANCZOS)
    mono = gray.convert("1")  # dithers (Floyd-Steinberg) by default
    px = mono.load()

    prefix = f"\033[38;2;{color[0]};{color[1]};{color[2]}m" if color else ""
    lines = []
    for cy in range(0, dot_rows, 4):
        row_chars = []
        for cx in range(0, dot_cols, 2):
            bits = 0
            for ry in range(4):
                for rx in range(2):
                    if px[cx + rx, cy + ry] == 255:  # bright source pixel -> lit dot
                        bits |= _DOT_BITS[ry][rx]
            row_chars.append(chr(_BRAILLE_BASE + bits))
        lines.append(prefix + "".join(row_chars) + (RESET if color else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Tier 2: Sixel renderer (optional, needs `pip install sixel`)
# --------------------------------------------------------------------------
def image_to_sixel(img: Image.Image, target_px_width: int) -> str:
    """
    Encode one frame as a Sixel escape sequence string, sized to
    `target_px_width` pixels wide (aspect-preserved). Requires the `sixel`
    package (`pip install sixel`). Returns the raw escape-sequence bytes as
    a str; write it straight to stdout.
    """
    from sixel import SixelWriter  # imported lazily — optional dependency

    img = _flatten_on_bg(img)
    scale = target_px_width / img.width
    img = img.resize((target_px_width, max(1, round(img.height * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    out = io.StringIO()
    SixelWriter().draw(buf, output=out)
    return out.getvalue()


def terminal_supports_graphics() -> bool:
    """
    Best-effort, non-authoritative check for Sixel/Kitty/iTerm2 support.
    There's no fully reliable cross-terminal way to query this, so treat
    this as a hint — expose a manual override in your config for the
    (common) cases where it guesses wrong.
    """
    import os

    term = os.environ.get("TERM", "")
    term_program = os.environ.get("TERM_PROGRAM", "")
    if "kitty" in term or term_program == "iTerm.app" or term_program == "WezTerm":
        return True
    if term_program == "vscode":
        # Only true if the user has enabled terminal.integrated.enableImages
        # (and, on Windows, gpuAcceleration). We can't read VS Code settings
        # from here, so this stays a guess — surface it as a config toggle.
        return os.environ.get("ECHOLOCATE_FORCE_GRAPHICS", "") == "1"
    return False


# --------------------------------------------------------------------------
# Player — background thread, redraws in place (no flicker, no scrollback spam)
# --------------------------------------------------------------------------
class TerminalGifPlayer:
    def __init__(
        self,
        frames: List[Image.Image],
        durations: List[float],
        cols: Optional[int] = None,
        mode: str = "braille",   # "braille", "halfblock", or "sixel"
        char_aspect: float = 2.0,
        stream=None,
        label: str = "",
        dot_color: Optional[Tuple[int, int, int]] = None,
    ):
        self.stream = stream or sys.stdout
        cols = cols or min(48, max(16, shutil.get_terminal_size((80, 24)).columns - 4))
        self.mode = mode
        self._char_aspect = char_aspect
        self._dot_color = dot_color

        if mode == "sixel":
            self._rendered = [image_to_sixel(f, target_px_width=cols * 8) for f in frames]
        elif mode == "braille":
            self._rendered = [image_to_braille(f, cols, char_aspect, dot_color) for f in frames]
        else:
            self._rendered = [image_to_halfblocks(f, cols, char_aspect) for f in frames]
            
        if label:
            self._rendered = [f + "\n" + label for f in self._rendered]

        self._durations = [d if d > 0 else 0.1 for d in durations]
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lines_printed = 0
        self._lock = threading.Lock()

    def _erase_previous(self):
        if self._lines_printed:
            self.stream.write(f"\r\033[{self._lines_printed}A")
            self.stream.write("\033[J")

    def redraw_current(self):
        # Assumes caller holds self._lock!
        if not self._rendered: return
        frame_str = self._rendered[self._frame_index]
        self.stream.write(frame_str)  # No trailing newline so terminal doesn't scroll!
        self.stream.flush()
        self._lines_printed = frame_str.count("\n")

    def _loop(self):
        self._frame_index = 0
        while not self._stop.is_set():
            with self._lock:
                if not self._rendered:
                    continue
                self._erase_previous()
                self.redraw_current()
                sleep_time = self._durations[self._frame_index % len(self._durations)]
            
            time.sleep(sleep_time)
            
            with self._lock:
                if self._rendered:
                    self._frame_index = (self._frame_index + 1) % len(self._rendered)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, clear: bool = True):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if clear:
            self._erase_previous()
            self.stream.flush()
            self._lines_printed = 0

    def swap(self, frames: List[Image.Image], durations: List[float], cols: Optional[int] = None, label: str = ""):
        """Hot-swap to a different animation (e.g. on state change) without
        restarting the thread."""
        cols = cols or min(48, max(16, shutil.get_terminal_size((80, 24)).columns - 4))
        if self.mode == "sixel":
            rendered = [image_to_sixel(f, target_px_width=cols * 8) for f in frames]
        elif self.mode == "braille":
            rendered = [image_to_braille(f, cols, getattr(self, '_char_aspect', 2.0), getattr(self, '_dot_color', None)) for f in frames]
        else:
            rendered = [image_to_halfblocks(f, cols, getattr(self, '_char_aspect', 2.0)) for f in frames]
            
        if label:
            rendered = [f + "\n" + label for f in rendered]
            
        with self._lock:
            self._rendered = rendered
            self._durations = [d if d > 0 else 0.1 for d in durations]
            self._frame_index = 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Preview a GIF rendered in the terminal.")
    ap.add_argument("gif_path")
    ap.add_argument("--cols", type=int, default=48)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--mode", choices=["braille", "halfblock", "sixel"], default="braille")
    args = ap.parse_args()

    frames, durations = load_gif_frames(args.gif_path)
    player = TerminalGifPlayer(frames, durations, cols=args.cols, mode=args.mode)
    player.start()
    try:
        time.sleep(args.seconds)
    finally:
        player.stop()
