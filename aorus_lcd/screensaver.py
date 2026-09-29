"""The Omarchy screensaver (ttfx effects over the Omarchy logo) as a panel animation.

The panel cannot be streamed to: its I2C link moves ~40 KB/s, a frame is
106 KB raw. What it can do is play an animation from its own flash, so the
screensaver is rendered ahead of time: each effect is dumped from ttfx,
resampled from the screensaver's 120 fps to the panel's frame rate, drawn as a
text grid, RLE-encoded, and packed into one GIF upload that fits the flash
slot (protocol.GIF_MAX_PAYLOAD). The panel then loops it on its own.

The grid is 81 x 21 cells of 4 x 8 px: the Omarchy logo is 81 columns wide
and 320 px leaves room for 80, so four columns are drawn 3 px wide, chosen
among columns the text leaves blank so its letters keep their shape.
"""
import hashlib
import random
import sys
from array import array
from dataclasses import dataclass, field

from . import protocol as P
from . import ttfx
from .glyphs import CELL_H, CELL_W, bitmap, brightness

RENDERER_VERSION = 4
BLACK = 0
# Effects that read well on a 320x170 panel, in the order they are packed.
DEFAULT_EFFECTS = ("decrypt", "matrix", "burn", "beams", "rain", "synthgrid", "waves", "spotlights",
                   "blackhole", "fireworks", "rings", "vhstape", "thunderstorm", "swarm", "wipe",
                   "slide", "middleout", "sweep", "print", "highlight")


def rgb565(rgb, scale=1.0):
    r, g, b = rgb
    if scale != 1.0:
        r, g, b = int(r * scale), int(g * scale), int(b * scale)
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def text_size(text):
    lines = text.rstrip("\n").split("\n")
    return max(len(line) for line in lines), len(lines)


MAX_SCALE = 8


def fitting_scale(text, inset=(0, 0, 0, 0)):
    """The biggest text size (1..MAX_SCALE) at which `text` spans at most ~92% of
    the width and ~60% of the height inside the inset (the effects need room)."""
    width, height = text_size(text)
    left, top, right, bottom = inset
    avail_w, avail_h = P.W - left - right, P.H - top - bottom
    for scale in range(MAX_SCALE, 1, -1):
        if width * CELL_W * scale <= 0.92 * avail_w and height * CELL_H * scale <= 0.6 * avail_h:
            return scale
    return 1


def make_grid(text, inset=(0, 0, 0, 0), cols=None, rows=21, scale=1, font=""):
    """The grid for `text` inside the inset. At scale 1: as many 4 x 8 cells as
    fit, never fewer columns than the text is wide (cells then shrink to 3 px).
    At scale 2..4, cells (and glyphs) are that many times bigger. Scale 0 picks
    the biggest that fits."""
    scale = scale or fitting_scale(text, inset)
    width, height = text_size(text)
    left, top, right, bottom = inset
    avail_w, avail_h = P.W - left - right, P.H - top - bottom
    if scale > 1:
        cols, rows = avail_w // (CELL_W * scale), avail_h // (CELL_H * scale)
        if width > cols or height > rows:
            raise ValueError(f"the text is {width}x{height} characters; at {scale}x the panel "
                             f"fits {cols}x{rows}")
        return Grid(cols, rows, (), inset, scale, font)
    cols = cols or max(width, avail_w // CELL_W)
    rows = min(rows, avail_h // CELL_H)
    if width > cols or height > rows:
        raise ValueError(f"the text is {width}x{height} characters; inside that border the panel "
                         f"fits {cols}x{rows}")
    if cols * (CELL_W - 1) > avail_w:
        raise ValueError(f"the text is {width} characters wide: the left and right border can take "
                         f"at most {P.W - cols * (CELL_W - 1)} px together")
    return Grid(cols, rows, blank_columns(text, cols), inset)


def center_text(grid, text, binary=None):
    """ttfx centres text on its canvas with a rounding that can be a whole cell
    off (at big text sizes, 24 x 48 px). Find where it really puts the text
    (the end of the `print` effect) and shift the grid so the text sits in the
    middle of the area inside the border."""
    last = None
    try:
        for raw in ttfx.dump(text, "print", grid.cols, grid.rows, 1, binary=binary):
            last = raw
    except ttfx.TtfxError:
        return grid                      # an old or unusual ttfx: keep its own placement
    cells = [(r, c) for r, row in enumerate(ttfx.parse_frame(last or b"")) for c, _ch, _rgb in row]
    if not cells:
        return grid
    r0, r1 = min(r for r, _ in cells), max(r for r, _ in cells)
    c0, c1 = min(c for _, c in cells), max(c for _, c in cells)
    left, top, right, bottom = grid.area
    dx = (left + right) / 2 - (grid.xs[c0] + grid.xs[c1] + grid.ws[c1]) / 2
    dy = (top + bottom) / 2 - (grid.top + r0 * grid.ch + grid.top + (r1 + 1) * grid.ch) / 2
    grid.shift(round(dx), round(dy))
    return grid


def blank_columns(text, cols):
    """Canvas columns the text never draws in (it is centred, like --anchor-text c)."""
    lines = text.rstrip("\n").split("\n")
    width = max(len(line) for line in lines)
    left = (cols - width) // 2
    used = {left + i for line in lines for i, ch in enumerate(line) if ch != " "}
    return [c for c in range(cols) if c not in used]


class Grid:
    """Where each terminal cell lands on the 320 x 170 panel, inside an
    `inset` (left, top, right, bottom px) that keeps it clear of the bezel."""

    def __init__(self, cols, rows, prefer_narrow=(), inset=(0, 0, 0, 0), scale=1, font=""):
        self.font = font
        left, top, right, bottom = inset
        width, height = P.W - left - right, P.H - top - bottom
        self.scale, self.cw, self.ch = scale, CELL_W * scale, CELL_H * scale
        narrowest = self.cw - (1 if scale == 1 else 0)
        if cols * narrowest > width or rows * self.ch > height:
            raise ValueError(f"a {cols}x{rows} grid does not fit {width}x{height} px")
        self.cols, self.rows = cols, rows
        widths = [self.cw] * cols
        narrow = max(0, cols * CELL_W - width) if scale == 1 else 0
        if narrow > cols // 2:
            widths = [CELL_W - 1] * cols         # mostly narrow: all even, smaller text beats uneven
        else:
            for c in self._pick_narrow(narrow, prefer_narrow):
                widths[c] = CELL_W - 1
        x = left + (width - sum(widths)) // 2
        self.xs, self.ws = [], widths
        for w in widths:
            self.xs.append(x)
            x += w
        self.top = top + (height - rows * self.ch) // 2
        self.area = (left, top, P.W - right, P.H - bottom)
        self._segments = {}

    def shift(self, dx, dy):
        """Move the whole grid by (dx, dy) px; cells that leave the panel are not drawn."""
        self.xs = [x + dx for x in self.xs]
        self.top += dy

    def _pick_narrow(self, n, prefer):
        """n columns to draw 3 px wide: the preferred ones (columns the text
        never uses) first, spread out, then the rest spread over the others."""
        pool = sorted({c for c in prefer if 0 <= c < self.cols})
        if n <= len(pool):
            return [pool[(2 * k + 1) * len(pool) // (2 * n)] for k in range(n)]
        rest = [c for c in range(self.cols) if c not in pool]
        m = n - len(pool)
        return pool + [rest[(2 * k + 1) * len(rest) // (2 * m)] for k in range(m)]

    def _cell_segments(self, ch, gy, width):
        """(is_foreground, pixels) runs of pixel row `gy` of a cell `width` px
        wide (a 3 px cell drops the glyph's rightmost column)."""
        key = (ch, gy, width)
        segs = self._segments.get(key)
        if segs is None:
            mask = bitmap(ch, self.cw, self.ch, self.font)[gy]
            segs = []
            for px in range(width):
                on = bool(mask >> (self.cw - 1 - px) & 1)
                if segs and segs[-1][0] == on:
                    segs[-1][1] += 1
                else:
                    segs.append([on, 1])
            segs = self._segments[key] = tuple((on, n) for on, n in segs)
        return segs

    def runs(self, frame):
        """A parsed ttfx frame -> (colours, counts): the panel image as runs."""
        colors, counts = [BLACK], [0]
        xs, ws, cell_segments = self.xs, self.ws, self._cell_segments

        def put(c, n):
            if colors[-1] == c:
                counts[-1] += n
            else:
                colors.append(c)
                counts.append(n)

        styled = []
        for r in range(self.rows):
            cells = frame[r] if r < len(frame) else ()
            styled.append([(c, ch, rgb565(rgb, brightness(ch))) for c, ch, rgb in cells
                           if c < self.cols and 0 <= xs[c] and xs[c] + ws[c] <= P.W])
        for y in range(P.H):
            r, gy = divmod(y - self.top, self.ch)
            if not 0 <= r < self.rows or not styled[r]:
                put(BLACK, P.W)
                continue
            x = 0
            for c, ch, color in styled[r]:
                x0, w = xs[c], ws[c]
                if x0 > x:
                    put(BLACK, x0 - x)
                for on, n in cell_segments(ch, gy, w):
                    put(color if on else BLACK, n)
                x = x0 + w
            put(BLACK, P.W - x)
        return colors, counts

    def encode(self, frame):
        """RLE blob for one frame (the firmware's format: 15-bit counts, runs of >= 3)."""
        return encode_runs(*self.runs(frame))

    def pixels(self, frame):
        """LE-RGB565 pixels for one frame (previews, tests)."""
        colors, counts = self.runs(frame)
        return b"".join(c.to_bytes(2, "little") * n for c, n in zip(colors, counts))


def encode_runs(colors, counts):
    out = bytearray()
    lit = array("H")

    def flush():
        while lit:
            chunk = lit[:0x7FFF]
            del lit[:0x7FFF]
            if sys.byteorder == "big":
                chunk.byteswap()
            out.extend(len(chunk).to_bytes(2, "little"))
            out.extend(chunk.tobytes())

    for c, n in zip(colors, counts):
        if n >= 3:
            flush()
            while n:
                k = min(n, 0x7FFF)
                out.extend((k | 0x8000).to_bytes(2, "little") + c.to_bytes(2, "little"))
                n -= k
        elif n:
            lit.extend([c] * n)
    flush()
    return bytes(out)


BLACK_FRAME = encode_runs([BLACK], [P.FRAME_PIXELS])


# ---- delta frames -------------------------------------------------------------------
# The panel paints a frame's pixels from the top-left in raster order and stops
# where the frame's data ends; the LCD keeps whatever it showed below that. So
# a frame only needs to redraw up to its last pixel that differs from the frame
# before it, and a repeated frame costs one 1-pixel run (4 bytes). The first
# frame played after an upload (and after each loop) is always drawn whole.

def last_difference(a, b):
    """Index of the last pixel where two LE-RGB565 frames differ, or -1."""
    a, b = memoryview(a), memoryview(b)
    if a == b:
        return -1
    lo, hi = 0, len(a)                   # a[lo:] differs from b[lo:], a[hi:] does not
    while hi - lo > 64:
        mid = (lo + hi) // 2
        if a[mid:] == b[mid:]:
            hi = mid
        else:
            lo = mid
    for i in range(hi - 1, lo - 1, -1):
        if a[i] != b[i]:
            return i // 2
    return lo // 2


def prefix_blob(pixels, n):
    """RLE of the first n pixels of an LE-RGB565 frame (n >= 1)."""
    values = array("H", pixels[:2 * n])
    if sys.byteorder == "big":
        values.byteswap()
    colors, counts = [], []
    for v in values:
        if colors and colors[-1] == v:
            counts[-1] += 1
        else:
            colors.append(v)
            counts.append(1)
    return encode_runs(colors, counts)


def delta_frames(frames):
    """Full RLE frames in play order -> the first whole, each other one cut
    after its last pixel that changed since the previous frame."""
    if not frames:
        return []
    out, prev = [frames[0]], P.rle_decode_frame(frames[0])
    for blob in frames[1:]:
        cur = P.rle_decode_frame(blob)
        n = last_difference(prev, cur) + 1
        cut = prefix_blob(cur, max(1, n))
        out.append(cut if len(cut) < len(blob) else blob)
        prev = cur
    return out


@dataclass
class Clip:
    effect: str
    blobs: list = field(default_factory=list)
    source_frames: int = 0

    @property
    def size(self):
        return sum(len(b) for b in self.blobs)


@dataclass
class Animation:
    clips: list
    skipped: list
    fps: int
    frames: list             # encoded frames in play order
    delay: int = 0           # frame delay in ms for the panel; 0 = 1000 / fps

    @property
    def delay_ms(self):
        return max(1, min(255, self.delay or round(1000 / self.fps)))

    @property
    def seconds(self):
        return len(self.frames) / self.fps

    @property
    def blobs(self):
        """Frames in upload order, as delta frames (see delta_frames). Playback
        shows frames 1..N-1 and wraps to 1, so frame 0 is a black placeholder
        that is never shown (see protocol.GIF_MAX_FRAMES)."""
        key = id(self.frames), len(self.frames)
        if getattr(self, "_delta_key", None) != key:
            self._delta, self._delta_key = delta_frames(self.frames), key
        return [BLACK_FRAME] + self._delta

    @property
    def size(self):
        return P.gif_payload_size(self.blobs)

    def upload(self):
        return P.gif_upload_encoded(self.blobs, self.delay_ms)

    def images(self, step=1):
        """The frames the panel plays, in order, as PIL images."""
        return [to_image(b) for b in self.frames[::step]]

    def save_preview(self, path, step=1):
        """An animated GIF of what the panel will play (every `step`-th frame)."""
        frames = self.images(step)
        frames[0].save(path, save_all=True, append_images=frames[1:], loop=0,
                       duration=self.delay_ms * step, optimize=False)


def to_image(blob):
    from PIL import Image
    return Image.frombytes("RGB", (P.W, P.H), P.rle_decode_frame(blob), "raw", "BGR;16")


def shown(i, fps, source_rate=ttfx.SOURCE_RATE):
    """Whether source frame i starts a new panel frame when resampling to `fps`."""
    return i == 0 or (i * fps) // source_rate != ((i - 1) * fps) // source_rate


def render_clip(text, effect, grid, fps, seed=1, hold_ms=1000, binary=None, cache=None, max_frames=20000,
                effect_args=()):
    """One effect as encoded panel frames, ending on a hold of its final frame."""
    clip = Clip(effect)
    cache = {} if cache is None else cache
    last, last_kept = None, -1
    for i, raw in enumerate(ttfx.dump(text, effect, grid.cols, grid.rows, seed, binary=binary,
                                      max_frames=max_frames, effect_args=effect_args)):
        clip.source_frames += 1
        last = raw
        if shown(i, fps):
            clip.blobs.append(_encode(grid, raw, cache))
            last_kept = i
    if last is None:
        return clip
    if last_kept != clip.source_frames - 1:
        clip.blobs.append(_encode(grid, last, cache))
    clip.blobs += [clip.blobs[-1]] * round(hold_ms * fps / 1000)
    return clip


def _encode(grid, raw, cache):
    key = hashlib.blake2b(raw, digest_size=16).digest()
    blob = cache.get(key)
    if blob is None:
        blob = cache[key] = grid.encode(ttfx.parse_frame(raw))
    return blob


def build(text=None, effects=DEFAULT_EFFECTS, fps=30, seed=1, hold_ms=1000, budget=None,
          binary=None, shuffle=False, progress=None, cols=None, rows=21, frames_exact=None, delay_ms=0,
          inset=(0, 0, 0, 0), scale=1, font=""):
    """Render effects in order, keeping each one that still fits the budget.
    `frames_exact` pads (with the last frame) or trims the loop to that many
    frames."""
    if text is None:
        with open(ttfx.DEFAULT_TEXT, encoding="utf-8") as f:
            text = f.read()
    if not text.strip():
        raise ValueError("the screensaver text is empty")
    binary = ttfx.find_binary(binary or "")
    known = ttfx.effects(binary)
    unknown = [e for e in effects if e not in known]
    if unknown:
        raise ValueError(f"unknown ttfx effects: {', '.join(unknown)}")
    grid = center_text(make_grid(text, inset, cols, rows, scale, font), text, binary)
    budget = min(budget or P.GIF_MAX_PAYLOAD, P.GIF_MAX_PAYLOAD)
    order = list(effects)
    if shuffle:
        random.Random(seed).shuffle(order)
    frames, clips, skipped, cache = [], [], [], {}
    used = P.gif_payload_size([BLACK_FRAME])
    for n, effect in enumerate(order):
        if progress:
            progress(effect, n, len(order))
        clip = render_clip(text, effect, grid, fps, seed + n, hold_ms, binary, cache)
        extra = clip.size + 10 * len(clip.blobs)
        if not clip.blobs or used + extra > budget or len(frames) + len(clip.blobs) > P.GIF_MAX_PLAYED:
            skipped.append((effect, len(clip.blobs)))
            continue
        clips.append(clip)
        frames += clip.blobs
        used += extra
    if not clips:
        raise ValueError(f"no effect fits in {P.GIF_MAX_PLAYED} frames at {fps} fps; "
                         "lower the frame rate or the hold")
    if frames_exact:
        if not 1 <= frames_exact <= P.GIF_MAX_PLAYED:
            raise ValueError(f"a loop has 1..{P.GIF_MAX_PLAYED} frames")
        frames = (frames + [frames[-1]] * frames_exact)[:frames_exact]
    return Animation(clips, skipped, fps, frames, delay_ms)


# ---- playlist loops -------------------------------------------------------------------
# A playlist shows one effect at a time, each as its own loop of at most
# LOOP_FRAMES frames, played as fast as the panel draws them.
LOOP_FRAMES = P.GIF_MAX_PLAYED
PANEL_FPS = 18                   # LCD fw 1.3 draws a whole 320x170 frame in ~55 ms
FPS_CHOICES = (18, 9, 6, 3)      # one, two, three or six draws of a whole frame


def frame_delay(fps):
    """The panel's frame delay for `fps`. The firmware redraws the current frame
    until the delay has passed, then moves on: a whole frame (~55 ms a draw)
    shows for the whole draws that reach the delay, and a delta frame, which
    draws in a moment, for about the delay itself. Just under 1000/fps keeps the
    two alike for FPS_CHOICES."""
    return max(1, round(1000 / fps) - 4)
# Effects whose default run outlasts a loop, shortened (they still end on the text).
EFFECT_ARGS = {"matrix": ("--rain-time", "3", "--resolve-delay", "1"), "thunderstorm": ("--storm-time", "5")}


def _effect_frames(text, effect, grid, fps, seed, binary, cache):
    clip = render_clip(text, effect, grid, fps, seed, 0, binary, cache,
                       effect_args=EFFECT_ARGS.get(effect, ()))
    return clip.blobs


def build_loop(text=None, effect="decrypt", seed=1, hold_ms=1500, inset=(0, 0, 0, 0), binary=None,
               fps=PANEL_FPS, frames=LOOP_FRAMES, budget=P.GIF_MAX_PAYLOAD, max_runs=12, scale=0, font=""):
    """One playlist loop: `effect` played over and over with fresh seeds, each
    run ending on a `hold_ms` pause on the finished text; as many runs as fit
    in `frames` frames and `budget` bytes. The loop is just those runs (not
    padded), so the panel goes straight back to the effect after the pause. An
    effect too long for the loop is sampled more sparsely (it plays faster)."""
    if text is None:
        with open(ttfx.DEFAULT_TEXT, encoding="utf-8") as f:
            text = f.read()
    binary = ttfx.find_binary(binary or "")
    if effect not in ttfx.effects(binary):
        raise ValueError(f"unknown ttfx effect {effect!r}")
    grid = center_text(make_grid(text, inset, scale=scale, font=font), text, binary)
    hold = max(1, round(hold_ms * fps / 1000))
    cache = {}
    first = _effect_frames(text, effect, grid, fps, seed, binary, cache)
    if not first:
        raise ValueError(f"ttfx {effect} produced no frames")
    capture_fps = fps
    if len(first) + hold > frames:
        capture_fps = fps * (frames - hold) / len(first)
        first = _effect_frames(text, effect, grid, capture_fps, seed, binary, cache)[:frames - hold]
    runs = [first]
    while len(runs) < max_runs:
        used = sum(len(r) + hold for r in runs)
        nxt = _effect_frames(text, effect, grid, capture_fps, seed + len(runs), binary, cache)
        if used + len(nxt) + hold > frames:
            break
        runs.append(nxt)

    def assemble(chosen):
        out = []
        for r in chosen:
            out += r + [r[-1]] * hold
        return out[:frames]

    def size(loop):
        return P.gif_payload_size(delta_frames(loop))

    loop = assemble(runs)
    while size(loop) > budget and len(runs) > 1:
        runs.pop()
        loop = assemble(runs)
    while size(loop) > budget and capture_fps > 3:
        capture_fps *= 0.8               # dense effects (matrix): fewer, faster frames
        runs = [_effect_frames(text, effect, grid, capture_fps, seed, binary, cache)[:frames - hold]]
        loop = assemble(runs)
    if size(loop) > budget:
        raise ValueError(f"ttfx {effect} is {size(loop) / 1e6:.1f} MB as a loop; "
                         f"an upload takes at most {budget / 1e6:.1f} MB")
    clips = [Clip(effect, r) for r in runs]
    return Animation(clips, [], fps, loop, frame_delay(fps))
