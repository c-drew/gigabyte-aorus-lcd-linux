"""Frames from ttfx (github.com/omacom-io/ttfx), the terminal text effects engine
behind the Omarchy screensaver.

ttfx's hidden `--parity-dump` mode writes every frame of an effect as
`<length>\\n<frame>\\n`, deterministically for a `--seed`, and on a virtual
clock, so time-based effects (matrix, thunderstorm) produce the same frames
however fast they are rendered. A frame is the canvas rows joined by
newlines; each coloured character is `ESC[38;2;R;G;Bm<ch>ESC[0m`.

The stream is parsed as it arrives and bounded three ways (frame count, frame
size, wall time): at an unlimited frame rate some effects run for thousands
of frames, and their raw output reaches gigabytes.
"""
import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import threading

DEFAULT_TEXT = "/usr/share/omarchy/logo.txt"     # the Omarchy screensaver's default text
SOURCE_RATE = 120                                # the Omarchy screensaver runs ttfx at 120 fps

_CELL = re.compile(r"\x1b\[38;2;(\d+);(\d+);(\d+)m([^\x1b]*)\x1b\[0m|([^\x1b]+)|\x1b\[[0-9;?]*[A-Za-z]")


class TtfxError(RuntimeError):
    pass


def find_binary(path=""):
    """`path`; else a ttfx installed alongside aorus-lcd (scripts/install.sh
    --ttfx), so the panel can use a newer one than the system's; else the one
    on PATH (Omarchy's, which its desktop screensaver uses)."""
    if not path:
        bundled = os.path.join(sys.prefix, "bin", "ttfx")
        if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            return bundled
    found = shutil.which(path or "ttfx")
    if not found:
        raise TtfxError("ttfx not found; install it (Omarchy ships it) or set its path")
    return found


@functools.lru_cache(maxsize=8)
def version(binary):
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10).stdout
    return out.split()[-1] if out.strip() else "unknown"


@functools.lru_cache(maxsize=8)
def effects(binary):
    """Effect names, from the binary's own help."""
    out = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=10).stdout
    try:
        block = out.split("Commands:", 1)[1].split("Options:", 1)[0]
    except IndexError:
        raise TtfxError(f"{binary} does not look like ttfx") from None
    names = [line.split()[0] for line in block.strip().splitlines() if line.strip()]
    return tuple(n for n in names if n != "help")


def parse_frame(raw, default_rgb=(200, 200, 200)):
    """Frame bytes -> rows of (column, char, (r, g, b)) for every visible cell."""
    rows = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        cells, col = [], 0
        for m in _CELL.finditer(line):
            if m.group(4) is not None:
                rgb = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                chars = m.group(4)
            elif m.group(5) is not None:
                rgb, chars = default_rgb, m.group(5)
            else:
                continue                                 # a bare escape: nothing to draw
            for ch in chars:
                if ch != " ":
                    cells.append((col, ch, rgb))
                col += 1
        rows.append(cells)
    return rows


def dump(text, effect, cols, rows, seed=1, frame_rate=SOURCE_RATE, max_frames=20000,
         binary=None, timeout=120.0, extra_args=(), effect_args=()):
    """Yield the raw bytes of each frame of `effect` played over `text` on a
    cols x rows canvas. Stops at `max_frames`; kills ttfx after `timeout` s."""
    binary = binary or find_binary()
    args = [binary, "--canvas-width", str(cols), "--canvas-height", str(rows),
            "--ignore-terminal-dimensions", "--anchor-canvas", "c", "--anchor-text", "c",
            "--parity-dump", "--seed", str(seed), "--frame-rate", str(frame_rate),
            "--max-frames", str(max_frames), *extra_args, effect, *effect_args]
    limit = cols * rows * 40 + rows + 64                  # the longest frame an SGR-per-cell dump can be
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    expired = threading.Event()

    def stop():
        try:
            os.killpg(proc.pid, signal.SIGKILL)          # ttfx and anything it started
        except ProcessLookupError:
            pass

    def kill():
        expired.set()
        stop()
    watchdog = threading.Timer(timeout, kill)
    watchdog.start()
    count = 0
    try:
        try:
            proc.stdin.write(text.encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass                                         # ttfx exited early; its stderr says why
        out = proc.stdout
        while count < max_frames:
            header = out.readline(16)
            if not header:
                break
            if not header.endswith(b"\n") or not header[:-1].isdigit():
                raise TtfxError(f"unexpected output from ttfx {effect}: {header[:16]!r}")
            size = int(header)
            if size > limit:
                raise TtfxError(f"ttfx {effect}: a {size}-byte frame (limit {limit})")
            frame = out.read(size + 1)
            if len(frame) != size + 1:
                break
            count += 1
            yield frame[:-1]
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            stop()
        proc.wait()
        err = proc.stderr.read().decode(errors="replace").strip()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
    if expired.is_set():
        raise TtfxError(f"ttfx {effect} timed out after {timeout:.0f}s ({count} frames)")
    if proc.returncode not in (0, None) and count == 0:
        raise TtfxError(f"ttfx {effect} failed: {err or f'exit {proc.returncode}'}")
