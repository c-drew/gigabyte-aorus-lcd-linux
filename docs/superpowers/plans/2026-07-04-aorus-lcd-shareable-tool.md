# Aorus LCD Shareable Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the public-ready `aorus_lcd.py` tool (subcommand CLI, bus autodetection, image/text/gif upload) plus tests and README in `~/Dev/aorus-master-linux`, refactored from `~/Dev/aorus-lcd-re/linux/aorus_lcd.py`.

**Architecture:** One auditable Python file. Pure protocol functions (frame builders, pixel encoders, RLE) are unit-tested; hardware I/O is a thin layer over `smbus2` tested with a fake bus; the CLI is argparse subparsers dispatching to small handlers. Bus selection is safe-by-default: sysfs adapter-name match + mandatory 0x61 ACK before any write.

**Tech Stack:** Python 3 stdlib + `smbus2` (hardware), `Pillow` (media only), `pytest` (dev only).

## Global Constraints

- Repo root: `/home/alban/Dev/aorus-master-linux`. Source of truth being refactored: `/home/alban/Dev/aorus-lcd-re/linux/aorus_lcd.py` (read-only reference — never modify the RE repo).
- Single tool file `aorus_lcd.py` at repo root. No packaging (`pyproject.toml` etc. is out of scope).
- Writes go ONLY to i2c address 0x61. 0x71 (RGB controller) must never be written. No code path may write before a 0x61 ACK probe succeeds.
- `rle_encode_frame`, `gif_frame_table`, `make_f1_header` semantics are byte-exact-validated against GCC — port them **verbatim in behavior**; do not simplify their quirks (window scan, <4-px literal tails, `usize//256 + 1` chunk count including the full zero-pad chunk on exact multiples, `h[16] = min(255, delay)`).
- Dropped features (do NOT port): capture replay/parsing (`parse_capture`, `replay`, `synth_timing`, `frames_only`), `--extract-bin`, `--upload-bin`, capture-timestamp pacing.
- Experimental commands (`brightness`, `poweroff-mode`, `raw`, `raw-read`) must print a one-line "experimental" notice when run.
- Dev venv: `.venv` at repo root with `smbus2 Pillow pytest`. Test command: `.venv/bin/pytest tests/ -v`.
- Commit after every task with the trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## File structure

```
aorus_lcd.py              # the tool (built up across Tasks 2–6)
tests/test_encoders.py    # pure-function + fake-bus tests (built up across Tasks 2–6)
README.md                 # Task 7
LICENSE                   # Task 1 (MIT)
.gitignore                # Task 1
docs/superpowers/...      # spec + this plan (already committed)
```

`aorus_lcd.py` internal order (sections appended task by task):
1. module docstring, imports, constants (Task 2)
2. frame builders (Task 2)
3. pixel encoders (Task 3)
4. GIF RLE pipeline (Task 4)
5. bus discovery + low-level I/O + commands + upload sequencing (Task 5)
6. selftest + CLI (Task 6)

---

### Task 1: Repo scaffolding

**Files:**
- Create: `.gitignore`, `LICENSE`, `tests/` (dir), `.venv/` (untracked)

**Interfaces:**
- Produces: a repo where `.venv/bin/pytest tests/ -v` runs (collecting 0 tests) and `git status` is clean after commit.

- [ ] **Step 1: Create `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 2: Create `LICENSE`** (MIT — the copyright holder line was approved as "Alban"; the author may amend the exact name/handle before publishing)

```text
MIT License

Copyright (c) 2026 Alban

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Create the dev venv and install dependencies**

Run:
```bash
cd /home/alban/Dev/aorus-master-linux
python3 -m venv .venv
.venv/bin/pip install smbus2 Pillow pytest
mkdir -p tests
```
Expected: pip installs succeed (all three are pure-python or wheel installs).

- [ ] **Step 4: Verify pytest runs**

Run: `.venv/bin/pytest tests/ -v`
Expected: `no tests ran` (exit code 5 is fine at this stage).

- [ ] **Step 5: Commit**

```bash
git add .gitignore LICENSE
git commit -m "Scaffolding: MIT license, gitignore, dev venv layout"
```

---

### Task 2: Constants and protocol frame builders

**Files:**
- Create: `aorus_lcd.py`
- Create: `tests/test_encoders.py`

**Interfaces:**
- Produces (used by every later task):
  - constants `ADDR=0x61`, `RGB_ADDR=0x71`, `W=320`, `H=170`, `FRAME_PIXELS`, `FRAME_BYTES=108800`, `DESC` (12 bytes), `FB_STATIC=0x01300000`, `FB_TEXT=0x01320000`, `FB_GIF=0x00000000`, `MODE_STATIC=3`, `MODE_TEXT=4`, `MODE_GIF=5`, `MAGIC`, opcodes `OP_OPENLCD/OP_SETMODE/OP_SETDISP/OP_SETLOOP/OP_POWEROFF/OP_TEXTFX`, pacing `PACE_BEGIN=0.5`, `PACE_HEADER=1.0`, `PACE_CHUNK=0.01`
  - `cmd_frame(opcode: int, tail: bytes = b"") -> bytes` (256 B)
  - `f2_frame(flag: int) -> bytes` (256 B; flag 1=BEGIN, 2=END)
  - `make_f1_header(fb_addr, nchunks, nframes, delay, usize, flag=1, mode=None) -> bytes` (256 B)
  - `chunk_payload(pdata: bytes) -> list[bytes]` (each 256 B, zero-padded, `len(pdata)//256 + 1` chunks)
  - `build_upload(pdata, fb_addr, flag=1, nframes=0, delay=0, mode=None) -> list[bytes]` = `[BEGIN, F1, *chunks, END]`

- [ ] **Step 1: Write the failing tests** — create `tests/test_encoders.py`:

```python
"""Tests for aorus_lcd.py pure functions (no hardware needed)."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aorus_lcd as A

F2 = bytes([0xF2, 0xCB, 0x55, 0xAC, 0x38])
F1 = bytes([0xF1, 0xCB, 0x55, 0xAC, 0x38])


def test_cmd_frame():
    fr = A.cmd_frame(0xE5, b"\x04")
    assert len(fr) == 256
    assert fr[0] == 0xE5 and fr[1:5] == A.MAGIC and fr[5] == 0x04
    assert not any(fr[6:]), "tail must be zero-padded"


def test_f2_frames():
    b, e = A.f2_frame(1), A.f2_frame(2)
    assert b[:6] == F2 + b"\x01" and not any(b[6:])
    assert e[:6] == F2 + b"\x02" and not any(e[6:])
    assert len(b) == len(e) == 256


def test_f1_header_static_image():
    # 12-byte descriptor + 108800 px bytes = 108812 -> 426 chunks, auto mode 2
    usize = A.DESC_LEN + A.FRAME_BYTES
    h = A.make_f1_header(A.FB_STATIC, usize // 256 + 1, 0, 0, usize)
    assert h[:5] == F1 and len(h) == 256
    assert h[5:9] == (0x01300000).to_bytes(4, "big")
    assert h[9] == 1                                   # flag: static/text
    assert int.from_bytes(h[10:14], "big") == 426      # chunk count
    assert int.from_bytes(h[14:16], "big") == 0        # frame count
    assert h[16] == 0 and h[17] == 2 and h[18] == 0    # delay, auto mode>=20480, pad
    assert not any(h[19:])


def test_f1_header_gif_flags_and_delay_clamp():
    h = A.make_f1_header(A.FB_GIF, 10, 7, 999, 2500, flag=2, mode=2)
    assert h[5:9] == b"\x00\x00\x00\x00"
    assert h[9] == 2
    assert int.from_bytes(h[14:16], "big") == 7
    assert h[16] == 255, "delay stored as min(255, delay)"
    assert h[17] == 2


def test_f1_header_small_payload_auto_mode():
    h = A.make_f1_header(A.FB_STATIC, 2, 0, 0, 300)   # < 20480 -> mode 1
    assert h[17] == 1


def test_chunk_payload_pads_and_counts():
    p = bytes(range(256)) * 2 + b"\xAA" * 10           # 522 bytes -> 3 chunks
    chunks = A.chunk_payload(p)
    assert len(chunks) == len(p) // 256 + 1 == 3
    assert all(len(c) == 256 for c in chunks)
    assert b"".join(chunks)[: len(p)] == p
    assert not any(b"".join(chunks)[len(p):]), "padding must be zero"


def test_chunk_payload_exact_multiple_adds_full_pad_chunk():
    # protocol-observed: chunk count is ALWAYS usize//256 + 1, so an exact
    # multiple gets one extra all-zero chunk. Do not "fix" this.
    p = b"\x11" * 512
    chunks = A.chunk_payload(p)
    assert len(chunks) == 3
    assert chunks[2] == bytes(256)


def test_build_upload_shape():
    p = b"\x22" * 300
    frames = A.build_upload(p, A.FB_STATIC)
    assert frames[0][:6] == F2 + b"\x01"
    assert frames[1][:5] == F1
    assert frames[-1][:6] == F2 + b"\x02"
    assert len(frames) == 2 + (len(p) // 256 + 1) + 1
    assert int.from_bytes(frames[1][10:14], "big") == len(p) // 256 + 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/ -v`
Expected: collection error — `ModuleNotFoundError: No module named 'aorus_lcd'`.

- [ ] **Step 3: Create `aorus_lcd.py`** with docstring, constants, and frame builders:

```python
#!/usr/bin/env python3
"""aorus_lcd.py — control the "LCD Edge View" screen on the Gigabyte Aorus
Master RTX 5090 from Linux.

The panel hangs off the GPU's internal I2C controller bus, behind the same
controller Gigabyte Control Center talks to on Windows. This tool speaks the
legacy 0x61 protocol, reverse-engineered from live GCC traffic and ucVga.dll:

  Transport : Linux i2c-dev on the NVIDIA internal controller bus
              (located by adapter NAME, never a guessed /dev/i2c-N).
  Address   : 0x61. The RGB controller at 0x71 is NEVER touched.
  Panel     : 320x170, little-endian RGB565, row-major.
  Uploads   : F2(BEGIN) -> F1 header -> 256-byte chunks -> F2(END),
              then E5 SetMode selects what the panel displays.

ALPHA software, tested on exactly one card (Aorus Master RTX 5090). Writes are
refused unless 0x61 ACKs a zero-length probe on the selected bus first.

Requires: python3-smbus2 (pip install smbus2); Pillow for image/text/gif.
Run as root or in the 'i2c' group; `sudo modprobe i2c-dev` first.
"""
import argparse
import glob
import os
import sys
import time

ADDR = 0x61              # LCD controller — the only address this tool writes
RGB_ADDR = 0x71          # RGB controller — never write here
W, H = 320, 170
FRAME_PIXELS = W * H
FRAME_BYTES = FRAME_PIXELS * 2          # 108800 (LE-RGB565)

# 12-byte payload descriptor prepended before single-frame pixels (constant:
# encodes 320x170 + fixed fields; identical in GCC's image and text uploads).
DESC = bytes([0x01, 0x00, 0x0B, 0xA9, 0x01, 0x00, 0x40, 0x01, 0xAA, 0x00, 0x01, 0x00])
DESC_LEN = len(DESC)

# framebuffer targets in the F1 header (from live captures):
FB_STATIC = 0x01300000   # static image  -> SetMode 3
FB_TEXT   = 0x01320000   # text          -> SetMode 4
FB_GIF    = 0x00000000   # animated gif  -> SetMode 5 (live buffer, mode FIRST)
MODE_STATIC, MODE_TEXT, MODE_GIF = 3, 4, 5

# command opcodes (legacy GvLcdApi, recovered from ucVga.dll):
# every command frame is [opcode, CB 55 AC 38, params...] zero-padded to 256.
MAGIC = bytes([0xCB, 0x55, 0xAC, 0x38])
OP_OPENLCD  = 0xE7   # byte5: 1 = panel ON, 2 = panel OFF
OP_SETMODE  = 0xE5   # byte5 = mode+1 (modes 0..6 confirmed; 7 maps to 9)
OP_SETDISP  = 0xE1   # element bitmask + value (brightness) — EXPERIMENTAL
OP_SETLOOP  = 0xF3   # carousel: byte5 = arg, byte6.. = (mode+1) play order
OP_POWEROFF = 0xFA   # SetPCPowerOffMode — EXPERIMENTAL
OP_TEXTFX   = 0xAA   # sent after a text upload; applies the rainbow effect

# upload pacing the panel firmware needs (from working captures):
PACE_BEGIN, PACE_HEADER, PACE_CHUNK = 0.5, 1.0, 0.01

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    SMBus = None


# ---- protocol frame builders ---------------------------------------------------

def cmd_frame(opcode, tail=b""):
    """256-byte command frame: opcode + CB 55 AC 38 + params, zero-padded."""
    buf = bytearray(256)
    buf[0] = opcode
    buf[1:5] = MAGIC
    buf[5:5 + len(tail)] = tail
    return bytes(buf)


def f2_frame(flag):
    """Upload BEGIN (flag=1) / END (flag=2) marker frame."""
    b = bytearray(256)
    b[0:6] = bytes([0xF2, 0xCB, 0x55, 0xAC, 0x38, flag])
    return bytes(b)


def make_f1_header(fb_addr, nchunks, nframes, delay, usize, flag=1, mode=None):
    """19-byte F1 upload header (padded to 256). Field layout verified against
    live captures — keep exactly, including the min(255, delay) clamp and the
    20480-byte auto-mode threshold."""
    h = bytearray(256)
    h[0:5] = bytes([0xF1, 0xCB, 0x55, 0xAC, 0x38])
    h[5:9] = fb_addr.to_bytes(4, "big")     # framebuffer target
    h[9]   = flag                           # 1 = static/text, 2 = animated gif
    h[10:14] = nchunks.to_bytes(4, "big")   # chunk count = usize//256 + 1
    h[14:16] = nframes.to_bytes(2, "big")   # frame count (0 for static/text)
    h[16]  = min(255, delay)                # per-frame delay (ms, clamped)
    h[17]  = mode if mode is not None else (2 if usize >= 20480 else 1)
    h[18]  = 0
    return bytes(h)


def chunk_payload(pdata):
    """Split into 256-byte zero-padded chunks. Chunk count is usize//256 + 1
    (protocol-observed: an exact 256-multiple still gets a full pad chunk)."""
    nchunks = len(pdata) // 256 + 1
    out = []
    for c in range(nchunks):
        seg = pdata[c * 256:(c + 1) * 256]
        out.append(seg + bytes(256 - len(seg)) if len(seg) < 256 else seg)
    return out


def build_upload(pdata, fb_addr, flag=1, nframes=0, delay=0, mode=None):
    """Full upload frame list: BEGIN -> F1 header -> chunks -> END."""
    header = make_f1_header(fb_addr, len(pdata) // 256 + 1, nframes, delay,
                            len(pdata), flag=flag, mode=mode)
    return [f2_frame(1), header] + chunk_payload(pdata) + [f2_frame(2)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add aorus_lcd.py tests/test_encoders.py
git commit -m "Protocol constants and F1/F2/command frame builders (tested)"
```

---

### Task 3: Pixel encoders (image, text, gif decode)

**Files:**
- Modify: `aorus_lcd.py` (append a section after the frame builders)
- Modify: `tests/test_encoders.py` (append tests)

**Interfaces:**
- Consumes: `W`, `H`, `FRAME_BYTES` from Task 2.
- Produces:
  - `image_to_le565(im) -> bytes` — PIL RGB image sized (W,H) → 108800 LE-RGB565 bytes (the single shared converter; was duplicated 3× in the RE script)
  - `load_image_le565(path) -> bytes` — open/convert/LANCZOS-resize + convert
  - `render_text_le565(text, size=28, fg=(139, 141, 139), bg=(0, 0, 0)) -> bytes`
  - `gif_to_le565_frames(path) -> tuple[list[bytes], list[int]]` — (frames, per-frame delays in ms)

- [ ] **Step 1: Append failing tests** to `tests/test_encoders.py`:

```python
def _solid_image(rgb):
    from PIL import Image
    return Image.new("RGB", (A.W, A.H), rgb)


def test_image_to_le565_known_pixels():
    # pure red: RGB565 0xF800 -> LE bytes 00 F8
    red = A.image_to_le565(_solid_image((255, 0, 0)))
    assert len(red) == A.FRAME_BYTES
    assert red[:2] == b"\x00\xf8" and red == red[:2] * A.FRAME_PIXELS
    # pure green: 0x07E0 -> E0 07 ; pure blue: 0x001F -> 1F 00
    assert A.image_to_le565(_solid_image((0, 255, 0)))[:2] == b"\xe0\x07"
    assert A.image_to_le565(_solid_image((0, 0, 255)))[:2] == b"\x1f\x00"
    # low bits are truncated, not rounded: (7,3,7) -> 0
    assert A.image_to_le565(_solid_image((7, 3, 7)))[:2] == b"\x00\x00"


def test_load_image_le565_resizes(tmp_path):
    from PIL import Image
    p = tmp_path / "in.png"
    Image.new("RGB", (64, 64), (255, 255, 255)).save(p)
    out = A.load_image_le565(str(p))
    assert len(out) == A.FRAME_BYTES
    assert out[:2] == b"\xff\xff"


def test_render_text_le565():
    out = A.render_text_le565("HELLO", size=28, fg=(255, 255, 255), bg=(0, 0, 0))
    assert len(out) == A.FRAME_BYTES
    assert b"\xff\xff" in out, "some white text pixels"
    assert out[:2] == b"\x00\x00", "corner stays background"


def _write_test_gif(path, nframes=7):
    """Synthetic 320x170 animated gif: moving block over changing background."""
    from PIL import Image
    frames = []
    for i in range(nframes):
        im = Image.new("RGB", (A.W, A.H), (12 * i, 0, 120))
        for x in range(48):
            for y in range(48):
                im.putpixel(((i * 41 + x) % A.W, (40 + y) % A.H),
                            (255, 255 - 30 * i, 33 * i % 256))
        frames.append(im)
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=50, loop=0)


def test_gif_to_le565_frames(tmp_path):
    p = str(tmp_path / "t.gif")
    _write_test_gif(p)
    frames, delays = A.gif_to_le565_frames(p)
    assert len(frames) == len(delays) == 7
    assert all(len(f) == A.FRAME_BYTES for f in frames)
    assert delays == [50] * 7
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/ -v`
Expected: Task 2 tests PASS; the 4 new tests FAIL with `AttributeError: ... has no attribute 'image_to_le565'` (etc.).

- [ ] **Step 3: Append the encoder section** to `aorus_lcd.py`:

```python
# ---- pixel encoders (need Pillow) ----------------------------------------------

def image_to_le565(im):
    """PIL RGB image already sized (W, H) -> little-endian RGB565 bytes."""
    out = bytearray(FRAME_BYTES)
    i = 0
    for (r, g, b) in im.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[i] = v & 0xFF
        out[i + 1] = (v >> 8) & 0xFF
        i += 2
    return bytes(out)


def load_image_le565(path):
    """Load any image file -> 320x170 LE-RGB565 (LANCZOS resize)."""
    from PIL import Image
    return image_to_le565(Image.open(path).convert("RGB").resize((W, H), Image.LANCZOS))


def render_text_le565(text, size=28, fg=(139, 141, 139), bg=(0, 0, 0)):
    """Render `text` centered on a 320x170 canvas -> LE-RGB565. Defaults match
    GCC's own text upload: black background, ~#8b8d8b gray text (the panel's
    rainbow effect uses the gray as a luminance mask)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    font = None
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, size)
            break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()
    bb = d.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((W - tw) / 2, (H - th) / 2), text, font=font, fill=fg)
    return image_to_le565(im)


def gif_to_le565_frames(path):
    """Decode an animated gif -> (list of LE-RGB565 frames, per-frame delays ms)."""
    from PIL import Image
    im = Image.open(path)
    frames, delays = [], []
    for i in range(getattr(im, "n_frames", 1)):
        im.seek(i)
        fr = im.convert("RGB").resize((W, H), Image.LANCZOS)
        frames.append(image_to_le565(fr))
        delays.append(im.info.get("duration", 100))
    return frames, delays
```

Note: the RE script's `text_to_le565` had a `d.textsize` fallback for ancient Pillow; the venv installs current Pillow where `textbbox` always exists, so the fallback is intentionally dropped.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v`
Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add aorus_lcd.py tests/test_encoders.py
git commit -m "Pixel encoders: shared LE-RGB565 converter, text render, gif decode"
```

---

### Task 4: GIF RLE pipeline (byte-exact port)

**Files:**
- Modify: `aorus_lcd.py` (append after the encoders)
- Modify: `tests/test_encoders.py` (append tests, ported from `~/Dev/aorus-lcd-re/linux/test_gif_table.py`)

**Interfaces:**
- Consumes: `gif_to_le565_frames`, `build_upload`, `FB_GIF`, `W`, `H` from earlier tasks.
- Produces:
  - `rle_encode_frame(px: bytes) -> bytes` — GCC Compress_RLE, byte-identical (VERBATIM port; every quirk load-bearing)
  - `gif_frame_table(sizes, w=W, h=H, fmt=3) -> bytes`
  - `build_gif_payload(gif_path, delay=None) -> tuple[bytes, int, int]` — (payload, frame_count, header_delay_ms)
  - `build_gif_frames(gif_path, delay=None) -> tuple[list[bytes], int]` — full upload frame list + frame count

- [ ] **Step 1: Append failing tests** to `tests/test_encoders.py`:

```python
# ---- GIF RLE pipeline -----------------------------------------------------------

def _rle_decode(blob):
    """Reference decoder mirroring the firmware: head = u16 LE, bit15 = run flag,
    low 15 bits = pixel count. Run: head + 1 pixel; literal: head + count px."""
    out = bytearray()
    pos = 0
    while pos < len(blob):
        head = blob[pos] | (blob[pos + 1] << 8)
        cnt = head & 0x7FFF
        assert cnt, f"zero-count token at {pos}"
        if head & 0x8000:
            out += blob[pos + 2:pos + 4] * cnt
            pos += 4
        else:
            out += blob[pos + 2:pos + 2 + 2 * cnt]
            pos += 2 + 2 * cnt
    return bytes(out)


def _px(*words):
    return b"".join(w.to_bytes(2, "little") for w in words)


def test_rle_run():
    px = _px(*[0x1234] * 6)
    assert A.rle_encode_frame(px) == b"\x06\x80" + _px(0x1234)


def test_rle_literal():
    px = _px(1, 2, 1, 2, 1, 2)
    assert A.rle_encode_frame(px) == b"\x06\x00" + px


def test_rle_short_tail_is_literal_even_if_equal():
    # <4 px left in the window -> whole window is ONE literal, even all-equal.
    px = _px(7, 7, 7)
    assert A.rle_encode_frame(px) == b"\x03\x00" + px


def test_rle_literal_then_run():
    px = _px(1, 2) + _px(*[9] * 4) + _px(5, 6, 7, 8, 5, 6)
    got = A.rle_encode_frame(px)
    assert got == (b"\x02\x00" + _px(1, 2)
                   + b"\x04\x80" + _px(9)
                   + b"\x06\x00" + _px(5, 6, 7, 8, 5, 6))


def test_rle_round_trip_random_ish():
    # deterministic pseudo-random frame; encoder output must decode to input
    px = bytearray()
    v = 12345
    for _ in range(A.FRAME_PIXELS):
        v = (v * 1103515245 + 12345) & 0x7FFFFFFF
        px += ((v >> 7) & 0xFFFF).to_bytes(2, "little")
    px = bytes(px)
    assert _rle_decode(A.rle_encode_frame(px)) == px


def test_rle_rejects_tiny_frames():
    import pytest
    with pytest.raises(ValueError):
        A.rle_encode_frame(_px(1, 2))


def test_gif_frame_table_offsets():
    # sizes [10, 20], N=2: header = 2 + 10*2 = 22 bytes.
    # entry u32 = inclusive end offset: 22+10-1 = 31, then 32+20-1 = 51.
    t = A.gif_frame_table([10, 20])
    assert struct.unpack_from("<H", t, 0)[0] == 2
    e0 = struct.unpack_from("<IHHH", t, 2)
    e1 = struct.unpack_from("<IHHH", t, 12)
    assert e0 == (31, A.W, A.H, 3)
    assert e1 == (51, A.W, A.H, 3)
    assert len(t) == 22


def _walk_rle_frame(buf, pos):
    """Walk one full-frame RLE blob; return its byte size."""
    start, px = pos, 0
    while px < A.FRAME_PIXELS:
        head = buf[pos] | (buf[pos + 1] << 8)
        cnt = head & 0x7FFF
        assert cnt
        pos += 4 if head & 0x8000 else 2 + 2 * cnt
        px += cnt
    assert px == A.FRAME_PIXELS
    return pos - start


def test_gif_payload_self_consistent(tmp_path):
    p = str(tmp_path / "t.gif")
    _write_test_gif(p)
    payload, n, d = A.build_gif_payload(p)
    assert n == 7
    assert d == 50, "header delay = average gif frame duration in ms"
    assert struct.unpack_from("<H", payload, 0)[0] == 7
    pos = cum = 2 + 10 * n
    for i in range(n):
        u32, w, h, t = struct.unpack_from("<IHHH", payload, 2 + 10 * i)
        size = _walk_rle_frame(payload, pos)
        cum += size
        assert u32 == cum - 1, f"frame {i} end offset"
        assert (w, h, t) == (A.W, A.H, 3)
        pos += size
    assert pos == len(payload), "no leftover bytes"


def test_build_gif_frames_wraps_payload(tmp_path):
    p = str(tmp_path / "t.gif")
    _write_test_gif(p)
    frames, n = A.build_gif_frames(p)
    assert n == 7
    assert frames[0][:6] == F2 + b"\x01" and frames[-1][:6] == F2 + b"\x02"
    hdr = frames[1]
    assert hdr[0] == 0xF1 and hdr[9] == 2, "gif flag"
    assert int.from_bytes(hdr[14:16], "big") == n
    payload, _, _ = A.build_gif_payload(p)
    body = b"".join(frames[2:-1])
    nchunks = len(payload) // 256 + 1
    assert int.from_bytes(hdr[10:14], "big") == nchunks
    assert len(body) == nchunks * 256
    assert body[:len(payload)] == payload
    assert not any(body[len(payload):])


# ---- optional ground truth against GCC's animation.bin --------------------------

def _find_anim():
    for p in (os.environ.get("AORUS_ANIM_BIN", ""),
              "/mnt/win/Program Files/GIGABYTE/Control Center/Lib/GBT_VGA/Assets/animation.bin"):
        if p and os.path.exists(p):
            return p
    return None


def test_encoder_matches_gcc_ground_truth():
    """Re-encode every RLE blob in GCC's own animation.bin; must be byte-identical.
    Skips unless the Windows mount or $AORUS_ANIM_BIN is available."""
    import pytest
    path = _find_anim()
    if not path:
        pytest.skip("animation.bin not found (mount Windows or set $AORUS_ANIM_BIN)")
    anim = open(path, "rb").read()
    n = struct.unpack_from("<H", anim, 0)[0]
    pos = 2 + 10 * n
    sizes = []
    for _ in range(n):
        s = _walk_rle_frame(anim, pos)
        sizes.append(s)
        pos += s
    assert pos == len(anim)
    assert A.gif_frame_table(sizes) == anim[:2 + 10 * n], "frame table"
    off = 2 + 10 * n
    for i, s in enumerate(sizes):
        blob = anim[off:off + s]
        off += s
        assert A.rle_encode_frame(_rle_decode(blob)) == blob, f"frame {i} re-encode"
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/ -v`
Expected: previous tests PASS; new ones FAIL with `AttributeError` on `rle_encode_frame` etc.

- [ ] **Step 3: Append the RLE section** to `aorus_lcd.py` — port VERBATIM from the RE script (comments included; they document validated firmware behavior):

```python
# ---- animated GIF pipeline -------------------------------------------------------
# Payload = <frameCount:2 LE> + table[frameCount] of <endOffset:4><w:2><h:2><fmt:2>
# + concatenated RLE frames. The u32 is the INCLUSIVE end offset of that frame's
# RLE blob within the payload: 2 + 10*N + sum(size[0..i]) - 1. Confirmed 60/60
# against GCC's Assets/animation.bin and in ucVga.dll IL (ImageMaker.SaveZipData).
# There is no checksum anywhere.

def rle_encode_frame(px):
    """RLE-encode one LE-RGB565 frame EXACTLY like GCC's Compress_RLE (byte-identical;
    validated by re-encoding every animation.bin blob). True token grammar:
    head = u16 LE, bit15 = run flag, low 15 bits = pixel count (can exceed 255!):
      run    : <count|0x8000 : u16 LE> <pixel : 2B>     (emitted only for >=3 equal px)
      literal: <count : u16 LE> <count pixels>
    Scanning (per findSameData/findMaxSameData IL): windows of <= 0x7FFF px; within a
    window the literal extends to the first >=3-equal-pixel repeat and the run is
    maximal but never crosses the window end; if the window has no repeat — or fewer
    than 4 px remain in it — the WHOLE window is one literal (so a trailing <4 px tail
    is a literal even if its pixels are all equal). The firmware decoder is the mirror
    of this encoder, so exact reproduction matters — do not 'optimize' the quirks."""
    mv = memoryview(px).cast("H")           # u16 pixel view (little-endian host)
    n = len(mv)
    if n < 3:
        raise ValueError("frame too small for Compress_RLE semantics")
    out = bytearray()
    i = 0
    while i < n:
        wend = i + min(0x7FFF, n - i)       # window [i, wend)
        wlen = wend - i
        if wlen < 4:
            diff, same = wlen, 0
        else:
            j = i
            while True:
                if j + 2 == wend:           # no repeat before window end: all literal
                    diff, same = wlen, 0
                    break
                if mv[j] == mv[j + 1] == mv[j + 2]:
                    rs = j
                    j += 2
                    while j < wend - 1 and mv[j] == mv[j + 1]:
                        j += 1
                    diff, same = rs - i, j + 1 - rs
                    break
                j += 1
        if diff:
            out += diff.to_bytes(2, "little")
            out += px[2 * i:2 * (i + diff)]
        if same:
            out += (same | 0x8000).to_bytes(2, "little")
            out += px[2 * (i + diff):2 * (i + diff) + 2]
        i += diff + same
    return bytes(out)


def gif_frame_table(sizes, w=W, h=H, fmt=3):
    """[frameCount:2 LE] + one 10-byte entry per frame:
    [endOffset:4 LE][w:2][h:2][fmt:2] (fmt 3 = RLE)."""
    n = len(sizes)
    out = bytearray(n.to_bytes(2, "little"))
    cum = 2 + 10 * n
    for s in sizes:
        cum += s
        out += (cum - 1).to_bytes(4, "little")
        out += w.to_bytes(2, "little") + h.to_bytes(2, "little") + fmt.to_bytes(2, "little")
    return bytes(out)


def build_gif_payload(gif_path, delay=None):
    """Decode gif -> RLE-compress each frame -> <frameCount> + table + blobs.
    Returns (payload, frame_count, header_delay_ms). Header delay unit is
    MILLISECONDS (GCC GetGifDelay = gif centiseconds*10, stored raw, IL-confirmed)."""
    frames, delays = gif_to_le565_frames(gif_path)
    rle = [rle_encode_frame(f) for f in frames]
    d = delay if delay is not None else min(255, max(1, round(sum(delays) / len(delays))))
    payload = gif_frame_table([len(r) for r in rle]) + b"".join(rle)
    return payload, len(frames), d


def build_gif_frames(gif_path, delay=None):
    """Animated-gif upload frame list (fb 0x00000000, flag 2) + frame count."""
    payload, n, d = build_gif_payload(gif_path, delay)
    return build_upload(payload, FB_GIF, flag=2, nframes=n, delay=d, mode=2), n
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v`
Expected: all PASS; `test_encoder_matches_gcc_ground_truth` either PASSes (Windows mount present) or SKIPs — both acceptable; note which in the task report.

- [ ] **Step 5: Commit**

```bash
git add aorus_lcd.py tests/test_encoders.py
git commit -m "GIF pipeline: byte-exact RLE encoder, frame table, payload builder"
```

---

### Task 5: Bus discovery, hardware I/O, commands, upload sequencing

**Files:**
- Modify: `aorus_lcd.py` (append after the GIF section)
- Modify: `tests/test_encoders.py` (append tests)

**Interfaces:**
- Consumes: `cmd_frame`, `build_upload`, opcodes, pacing constants.
- Produces:
  - `SYS_I2C_DEV = "/sys/class/i2c-dev"`, `NVIDIA_BUS_PREFIX = "NVIDIA i2c adapter 1 at"`
  - `list_adapters(sys_root=SYS_I2C_DEV) -> list[tuple[int, str]]`
  - `find_nvidia_bus(sys_root=SYS_I2C_DEV) -> int | None`
  - `probe(bus_n) -> tuple[bool, str]`
  - `resolve_bus(bus_arg: int | None) -> int` — exits via `sys.exit(str)` on failure; never returns an unverified bus
  - `write_frame(bus, data) -> None`
  - `read_cmd(bus, opcode, tail=b"\x03", nbytes=8) -> bytes`
  - `send_upload(bus, frames, chunk_delay=PACE_CHUNK) -> None`
  - `upload_content(bus, frames, mode, is_gif, set_display_mode=True, chunk_delay=PACE_CHUNK) -> None`
  - `open_lcd(bus, on)`, `set_mode(bus, m)`, `set_brightness(bus, value, mask=0xFF)`, `set_carousel(bus, modes, arg=0)`, `power_off_mode(bus)`

- [ ] **Step 1: Append failing tests** to `tests/test_encoders.py`:

```python
# ---- bus discovery / command layer (no hardware: fake sysfs + fake bus) ---------

def _fake_sysfs(tmp_path, names):
    root = tmp_path / "i2c-dev"
    for n, name in names.items():
        d = root / f"i2c-{n}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
    return str(root)


def test_find_nvidia_bus(tmp_path):
    root = _fake_sysfs(tmp_path, {
        0: "SMBus PIIX4 adapter port 0 at 0b00",
        3: "NVIDIA i2c adapter 1 at 1:00.0",
        4: "NVIDIA i2c adapter 6 at 1:00.0",
    })
    assert A.find_nvidia_bus(sys_root=root) == 3
    assert A.list_adapters(sys_root=root)[0] == (0, "SMBus PIIX4 adapter port 0 at 0b00")


def test_find_nvidia_bus_absent(tmp_path):
    root = _fake_sysfs(tmp_path, {0: "SMBus PIIX4 adapter port 0 at 0b00"})
    assert A.find_nvidia_bus(sys_root=root) is None


class FakeBus:
    """Records every i2c write as raw bytes."""
    def __init__(self):
        self.writes = []

    def i2c_rdwr(self, *msgs):
        for m in msgs:
            self.writes.append(bytes(m))


def test_command_frames_on_the_wire():
    bus = FakeBus()
    A.open_lcd(bus, True)
    A.open_lcd(bus, False)
    A.set_mode(bus, 3)
    A.set_mode(bus, 7)      # quirk: mode 7 sends 9+1
    A.set_carousel(bus, [0, 1, 4], arg=2)
    A.power_off_mode(bus)
    ops = [(w[0], w[5], w[6:9]) for w in bus.writes]
    assert ops[0][:2] == (0xE7, 1)
    assert ops[1][:2] == (0xE7, 2)
    assert ops[2][:2] == (0xE5, 4)
    assert ops[3][:2] == (0xE5, 10)
    assert ops[4] == (0xF3, 2, bytes([1, 2, 5]))
    assert ops[5][0] == 0xFA
    assert all(w[1:5] == A.MAGIC and len(w) == 256 for w in bus.writes)


def test_set_brightness_mask_and_value():
    bus = FakeBus()
    A.set_brightness(bus, 200, mask=0x05)
    w = bus.writes[0]
    assert w[0] == 0xE1
    assert w[5:13] == bytes([1, 0, 1, 0, 0, 0, 0, 0])
    assert w[13] == 200


def test_send_upload_pacing(monkeypatch):
    sleeps = []
    monkeypatch.setattr(A.time, "sleep", sleeps.append)
    bus = FakeBus()
    frames = A.build_upload(b"\x33" * 300, A.FB_STATIC)   # BEGIN,F1,c1,c2,END
    A.send_upload(bus, frames)
    assert bus.writes == frames
    assert sleeps[:2] == [A.PACE_BEGIN, A.PACE_HEADER]
    assert all(s == A.PACE_CHUNK for s in sleeps[2:])


def test_upload_content_mode_ordering(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    frames = A.build_upload(b"\x44" * 300, A.FB_STATIC)
    # image/text: SetMode AFTER the upload
    bus = FakeBus()
    A.upload_content(bus, frames, A.MODE_STATIC, is_gif=False)
    assert bus.writes[-1][0] == 0xE5 and bus.writes[-1][5] == A.MODE_STATIC + 1
    assert bus.writes[:-1] == frames
    # gif: SetMode BEFORE streaming (fb 0 is a live buffer)
    bus = FakeBus()
    A.upload_content(bus, frames, A.MODE_GIF, is_gif=True)
    assert bus.writes[0][0] == 0xE5 and bus.writes[0][5] == A.MODE_GIF + 1
    assert bus.writes[1:] == frames
    # --no-mode: upload only
    bus = FakeBus()
    A.upload_content(bus, frames, A.MODE_STATIC, is_gif=False, set_display_mode=False)
    assert bus.writes == frames
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/ -v`
Expected: new tests FAIL with `AttributeError` (`find_nvidia_bus`, `open_lcd`, ...).

- [ ] **Step 3: Append the hardware section** to `aorus_lcd.py`:

```python
# ---- bus discovery ---------------------------------------------------------------

SYS_I2C_DEV = "/sys/class/i2c-dev"
NVIDIA_BUS_PREFIX = "NVIDIA i2c adapter 1 at"   # the internal controller bus (LCD @0x61)


def list_adapters(sys_root=SYS_I2C_DEV):
    """[(bus_number, adapter_name), ...] from sysfs, sorted by bus number."""
    out = []
    for path in glob.glob(os.path.join(sys_root, "i2c-*")):
        try:
            with open(os.path.join(path, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        out.append((int(path.rsplit("-", 1)[1]), name))
    return sorted(out)


def find_nvidia_bus(sys_root=SYS_I2C_DEV):
    """Bus number of the NVIDIA internal controller bus, or None. Matching by
    NAME (not /dev/i2c-N position) so a module-load reorder can't point us at a
    chipset SMBus."""
    for n, name in list_adapters(sys_root):
        if name.startswith(NVIDIA_BUS_PREFIX):
            return n
    return None


def probe(bus_n):
    """Low-risk presence check: a 0-length write (SMBus quick) to 0x61.
    ACK => controller present. (0x61 is not a monitor DDC address.)"""
    try:
        with SMBus(bus_n) as bus:
            bus.i2c_rdwr(i2c_msg.write(ADDR, b""))
        return True, "ACK (device present at 0x61)"
    except FileNotFoundError:
        return False, "bus not present (is i2c-dev loaded? sudo modprobe i2c-dev)"
    except PermissionError:
        return False, "permission denied (run as root, or add yourself to the 'i2c' group)"
    except OSError as e:
        return False, f"no ACK ({e})"


def resolve_bus(bus_arg):
    """Return a bus number verified to ACK at 0x61, or exit with a clear error.
    With bus_arg=None, autodetect the NVIDIA bus by adapter name."""
    if bus_arg is not None:
        ok, detail = probe(bus_arg)
        if not ok:
            sys.exit(f"/dev/i2c-{bus_arg} @0x61: {detail}")
        return bus_arg
    n = find_nvidia_bus()
    if n is None:
        seen = "\n".join(f"  /dev/i2c-{k}: {v}" for k, v in list_adapters()) \
               or "  (none — is i2c-dev loaded? sudo modprobe i2c-dev)"
        sys.exit("could not find the NVIDIA internal i2c bus "
                 f'(adapter name starting "{NVIDIA_BUS_PREFIX}").\n'
                 f"Adapters seen:\n{seen}\n"
                 "If your card exposes a different name, pass --bus N explicitly.")
    ok, detail = probe(n)
    if not ok:
        sys.exit(f"found NVIDIA bus /dev/i2c-{n} but: {detail}")
    print(f"using /dev/i2c-{n} (autodetected NVIDIA internal bus)")
    return n


# ---- hardware I/O ----------------------------------------------------------------

def write_frame(bus, data):
    """One raw I2C write of `data` to 0x61 (matches GCC's GvWriteI2C block write)."""
    bus.i2c_rdwr(i2c_msg.write(ADDR, bytes(data)))


def read_cmd(bus, opcode, tail=b"\x03", nbytes=8):
    """Write a command frame, then read `nbytes` back from 0x61 (GvReadI2C
    pattern, e.g. EB 03 / ED 03 status queries)."""
    write_frame(bus, cmd_frame(opcode, tail))
    msg = i2c_msg.read(ADDR, nbytes)
    bus.i2c_rdwr(msg)
    return bytes(msg)


# ---- panel commands ----------------------------------------------------------------

def open_lcd(bus, on):
    write_frame(bus, cmd_frame(OP_OPENLCD, bytes([1 if on else 2])))


def set_mode(bus, m):
    """E5 SetMode: byte5 = mode+1. Modes 0..6 confirmed on hardware
    (3 = static image, 4 = text, 5 = gif, 6 = chibi); mode 7 maps to 9 (GCC quirk)."""
    write_frame(bus, cmd_frame(OP_SETMODE, bytes([(9 if m == 7 else m) + 1])))


def set_brightness(bus, value, mask=0xFF):
    """E1 SetDisplay: byte5..12 = 1 per set bit of `mask`, byte13 = value.
    EXPERIMENTAL: element-mask semantics inferred from the decompile, not
    hardware-confirmed."""
    tail = bytearray(9)
    for i in range(8):
        if mask & (1 << i):
            tail[i] = 1
    tail[8] = value & 0xFF
    write_frame(bus, cmd_frame(OP_SETDISP, bytes(tail)))


def set_carousel(bus, modes, arg=0):
    """F3 SetLoop: cycle built-in modes. byte5 = arg (interval/param),
    byte6.. = (mode+1) in play order. Modes must be 0..6."""
    tail = bytearray([arg & 0xFF])
    for m in modes:
        if 0 <= m <= 6:
            tail.append((m & 0xFF) + 1)
    write_frame(bus, cmd_frame(OP_SETLOOP, bytes(tail)))


def power_off_mode(bus):
    write_frame(bus, cmd_frame(OP_POWEROFF))


# ---- upload sequencing --------------------------------------------------------------

def send_upload(bus, frames, chunk_delay=PACE_CHUNK):
    """Write the upload frames with the pacing the panel firmware needs:
    0.5 s after BEGIN, 1.0 s after the F1 header, ~10 ms between chunks."""
    for fr in frames:
        write_frame(bus, fr)
        if fr[0] == 0xF2 and fr[5] == 0x01:
            time.sleep(PACE_BEGIN)
        elif fr[0] == 0xF1:
            time.sleep(PACE_HEADER)
        else:
            time.sleep(chunk_delay)


def upload_content(bus, frames, mode, is_gif, set_display_mode=True,
                   chunk_delay=PACE_CHUNK):
    """Stream an upload and select its display mode. ORDER MATTERS: a gif
    streams to framebuffer 0 — a live buffer — so the panel must already be in
    gif mode and listening as frames arrive (SetMode BEFORE; switching after
    shows black). Image/text store to numbered framebuffers, so their SetMode
    goes AFTER the upload (the capture-confirmed sequence)."""
    if is_gif and set_display_mode:
        set_mode(bus, mode)
        time.sleep(0.2)
    send_upload(bus, frames, chunk_delay)
    if not is_gif and set_display_mode:
        set_mode(bus, mode)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add aorus_lcd.py tests/test_encoders.py
git commit -m "Bus autodetection by adapter name, panel commands, paced uploads"
```

---

### Task 6: CLI (subcommands) and selftest

**Files:**
- Modify: `aorus_lcd.py` (append final section)
- Modify: `tests/test_encoders.py` (append tests)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `run_selftest() -> int` — returns number of failures, prints PASS/FAIL lines; no hardware, no Pillow
  - `build_parser() -> argparse.ArgumentParser` — subcommands `probe on off mode image text gif carousel brightness poweroff-mode raw raw-read selftest`; each subparser sets `func` via `set_defaults(func=...)`
  - `main(argv=None)` — entry point; prints help when no subcommand
  - handlers `cli_probe, cli_on, cli_off, cli_mode, cli_image, cli_text, cli_gif, cli_carousel, cli_brightness, cli_poweroff_mode, cli_raw, cli_raw_read, cli_selftest` (each takes the parsed `args` namespace)

- [ ] **Step 1: Append failing tests** to `tests/test_encoders.py`:

```python
# ---- CLI wiring / selftest --------------------------------------------------------

def test_selftest_passes():
    assert A.run_selftest() == 0


def test_parser_wiring():
    p = A.build_parser()
    a = p.parse_args(["mode", "3"])
    assert a.func is A.cli_mode and a.mode == 3 and a.bus is None
    a = p.parse_args(["--bus", "2", "image", "x.png"])
    assert a.func is A.cli_image and a.bus == 2 and a.file == "x.png"
    a = p.parse_args(["text", "hi", "--size", "40", "--color", "ff0000",
                      "--bg", "000000", "--no-effect"])
    assert a.func is A.cli_text and a.size == 40 and a.no_effect
    a = p.parse_args(["gif", "x.gif", "--frame-delay", "80", "--no-mode"])
    assert a.func is A.cli_gif and a.frame_delay == 80 and a.no_mode
    a = p.parse_args(["carousel", "0,1,4", "--arg", "2"])
    assert a.func is A.cli_carousel and a.modes == "0,1,4" and a.arg == 2
    a = p.parse_args(["raw", "aa 01 02"])
    assert a.func is A.cli_raw and a.hexbytes == "aa 01 02"
    a = p.parse_args(["raw-read", "eb 03", "--len", "16"])
    assert a.func is A.cli_raw_read and a.len == 16
    for name, fn in [("probe", A.cli_probe), ("on", A.cli_on), ("off", A.cli_off),
                     ("brightness", None), ("poweroff-mode", A.cli_poweroff_mode),
                     ("selftest", A.cli_selftest)]:
        argv = [name] if name not in ("brightness",) else [name, "128"]
        a = p.parse_args(argv)
        if fn:
            assert a.func is fn


def test_parse_hex_bytes():
    assert A.parse_hex_bytes("aa 01 02") == bytes([0xAA, 0x01, 0x02])
    assert A.parse_hex_bytes("eb,03") == bytes([0xEB, 0x03])


def test_parse_color():
    assert A.parse_color("ff8000") == (255, 128, 0)
    assert A.parse_color("#ff8000") == (255, 128, 0)
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/ -v`
Expected: new tests FAIL with `AttributeError` on `run_selftest` / `build_parser`.

- [ ] **Step 3: Append the selftest + CLI section** to `aorus_lcd.py`:

```python
# ---- selftest (pure functions, no hardware, no Pillow) ------------------------------

def run_selftest():
    """Quick self-check of the protocol encoders. Returns the failure count."""
    def px(*words):
        return b"".join(w.to_bytes(2, "little") for w in words)

    checks = [
        ("cmd_frame", lambda: cmd_frame(0xE5, b"\x04")[:6] == bytes([0xE5]) + MAGIC + b"\x04"),
        ("f2_begin", lambda: f2_frame(1)[:6] == bytes([0xF2]) + MAGIC + b"\x01"),
        ("f1_fields", lambda: (lambda h: h[5:9] == b"\x01\x30\x00\x00"
                               and int.from_bytes(h[10:14], "big") == 426
                               and h[16] == 0 and h[17] == 2)
                              (make_f1_header(FB_STATIC, 426, 0, 0, DESC_LEN + FRAME_BYTES))),
        ("f1_delay_clamp", lambda: make_f1_header(FB_GIF, 1, 1, 999, 100, flag=2, mode=2)[16] == 255),
        ("chunk_pad", lambda: len(chunk_payload(b"x" * 512)) == 3
                              and chunk_payload(b"x" * 512)[2] == bytes(256)),
        ("rle_run", lambda: rle_encode_frame(px(*[0x1234] * 6)) == b"\x06\x80" + px(0x1234)),
        ("rle_literal", lambda: rle_encode_frame(px(1, 2, 1, 2, 1, 2)) == b"\x06\x00" + px(1, 2, 1, 2, 1, 2)),
        ("rle_short_tail", lambda: rle_encode_frame(px(7, 7, 7)) == b"\x03\x00" + px(7, 7, 7)),
        ("gif_table", lambda: gif_frame_table([10, 20])
                              == b"\x02\x00"
                              + (31).to_bytes(4, "little") + px(W, H, 3)
                              + (51).to_bytes(4, "little") + px(W, H, 3)),
    ]
    failed = 0
    for name, fn in checks:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            name += f" ({e.__class__.__name__}: {e})"
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return failed


# ---- CLI ----------------------------------------------------------------------------

def parse_hex_bytes(s):
    return bytes(int(x, 16) for x in s.replace(",", " ").split())


def parse_color(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _experimental(what):
    print(f"note: '{what}' is EXPERIMENTAL — semantics inferred from the decompile, "
          "not fully hardware-confirmed.")


def _open_bus(args):
    if SMBus is None:
        sys.exit("smbus2 not installed:  pip install smbus2")
    return SMBus(resolve_bus(args.bus))


def cli_probe(args):
    if SMBus is None:
        sys.exit("smbus2 not installed:  pip install smbus2")
    if args.bus is not None:
        ok, detail = probe(args.bus)
        print(f"/dev/i2c-{args.bus} @0x61: {detail}")
        sys.exit(0 if ok else 1)
    n = find_nvidia_bus()
    if n is None:
        seen = "\n".join(f"  /dev/i2c-{k}: {v}" for k, v in list_adapters()) \
               or "  (none — is i2c-dev loaded? sudo modprobe i2c-dev)"
        print(f"no NVIDIA internal bus found. Adapters seen:\n{seen}")
        sys.exit(1)
    ok, detail = probe(n)
    print(f"/dev/i2c-{n} (NVIDIA internal bus) @0x61: {detail}")
    sys.exit(0 if ok else 1)


def cli_on(args):
    with _open_bus(args) as bus:
        open_lcd(bus, True)
    print("panel ON (E7 01)")


def cli_off(args):
    with _open_bus(args) as bus:
        open_lcd(bus, False)
    print("panel OFF (E7 02)")


def cli_mode(args):
    with _open_bus(args) as bus:
        set_mode(bus, args.mode)
    print(f"SetMode {args.mode}")


def cli_image(args):
    pixels = load_image_le565(args.file)
    frames = build_upload(DESC + pixels, FB_STATIC)
    print(f"uploading {args.file} ({len(frames)} i2c writes) ...")
    with _open_bus(args) as bus:
        upload_content(bus, frames, MODE_STATIC, is_gif=False,
                       set_display_mode=not args.no_mode, chunk_delay=args.chunk_delay)
    print("done" + ("" if args.no_mode else " (SetMode 3)"))


def cli_text(args):
    pixels = render_text_le565(args.text, size=args.size,
                               fg=parse_color(args.color), bg=parse_color(args.bg))
    frames = build_upload(DESC + pixels, FB_TEXT)
    print(f'uploading text "{args.text}" ({len(frames)} i2c writes) ...')
    with _open_bus(args) as bus:
        upload_content(bus, frames, MODE_TEXT, is_gif=False,
                       set_display_mode=not args.no_mode, chunk_delay=args.chunk_delay)
        if not args.no_mode and not args.no_effect:
            write_frame(bus, cmd_frame(OP_TEXTFX))   # panel rainbow/LED effect
            print("text effect applied (AA)")
    print("done")


def cli_gif(args):
    frames, n = build_gif_frames(args.file, args.frame_delay)
    print(f"uploading {args.file}: {n} frames, {len(frames)} i2c writes ...")
    with _open_bus(args) as bus:
        upload_content(bus, frames, MODE_GIF, is_gif=True,
                       set_display_mode=not args.no_mode, chunk_delay=args.chunk_delay)
    print("done")


def cli_carousel(args):
    modes = [int(x) for x in args.modes.split(",") if x.strip()]
    bad = [m for m in modes if not 0 <= m <= 6]
    if bad:
        sys.exit(f"carousel modes must be 0..6, got {bad}")
    with _open_bus(args) as bus:
        set_carousel(bus, modes, args.arg)
    print(f"carousel {modes} arg={args.arg} (F3)")


def cli_brightness(args):
    _experimental("brightness")
    with _open_bus(args) as bus:
        set_brightness(bus, args.value)
    print(f"SetDisplay brightness {args.value} (E1)")


def cli_poweroff_mode(args):
    _experimental("poweroff-mode")
    with _open_bus(args) as bus:
        power_off_mode(bus)
    print("SetPCPowerOffMode (FA)")


def cli_raw(args):
    _experimental("raw")
    b = parse_hex_bytes(args.hexbytes)
    with _open_bus(args) as bus:
        write_frame(bus, cmd_frame(b[0], bytes(b[1:])))
    print(f"sent {b[0]:#04x} params {bytes(b[1:]).hex(' ') or '(none)'}")


def cli_raw_read(args):
    _experimental("raw-read")
    b = parse_hex_bytes(args.hexbytes)
    with _open_bus(args) as bus:
        r = read_cmd(bus, b[0], bytes(b[1:]), args.len)
    print(f"read {b[0]:#04x} {bytes(b[1:]).hex(' ')} -> {r.hex(' ')}")


def cli_selftest(args):
    sys.exit(1 if run_selftest() else 0)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="aorus_lcd.py",
        description="Control the Aorus Master RTX 5090 'LCD Edge View' from Linux "
                    "(legacy 0x61 protocol).")
    ap.add_argument("--bus", type=int, default=None, metavar="N",
                    help="/dev/i2c-N to use (default: autodetect the NVIDIA bus by name)")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("probe", help="find the NVIDIA bus and check the LCD controller ACKs") \
       .set_defaults(func=cli_probe)
    sub.add_parser("on", help="turn the panel on").set_defaults(func=cli_on)
    sub.add_parser("off", help="turn the panel off").set_defaults(func=cli_off)

    p = sub.add_parser("mode", help="select display mode 0..7 (3=image, 4=text, 5=gif, 6=chibi)")
    p.add_argument("mode", type=int, choices=range(0, 8))
    p.set_defaults(func=cli_mode)

    def add_upload_opts(p):
        p.add_argument("--no-mode", action="store_true",
                       help="upload only; skip the display-mode switch")
        p.add_argument("--chunk-delay", type=float, default=PACE_CHUNK, metavar="SEC",
                       help=f"delay between 256-byte chunks (default {PACE_CHUNK})")

    p = sub.add_parser("image", help="show a static image (resized to 320x170)")
    p.add_argument("file", help="image file (png/jpg/anything Pillow opens)")
    add_upload_opts(p)
    p.set_defaults(func=cli_image)

    p = sub.add_parser("text", help="render and show a text message")
    p.add_argument("text")
    p.add_argument("--size", type=int, default=28, help="font size (default 28)")
    p.add_argument("--color", default="8b8d8b",
                   help="text color RRGGBB (default 8b8d8b — GCC's gray, needed "
                        "for the rainbow effect)")
    p.add_argument("--bg", default="000000", help="background RRGGBB (default black)")
    p.add_argument("--no-effect", action="store_true",
                   help="skip the panel's rainbow effect (AA) after upload")
    add_upload_opts(p)
    p.set_defaults(func=cli_text)

    p = sub.add_parser("gif", help="play an animated gif (RLE-compressed upload)")
    p.add_argument("file")
    p.add_argument("--frame-delay", type=int, default=None, metavar="MS",
                   help="per-frame delay override in ms (default: from the gif)")
    add_upload_opts(p)
    p.set_defaults(func=cli_gif)

    p = sub.add_parser("carousel", help="cycle built-in modes, e.g. 0,1,4")
    p.add_argument("modes", help="comma-separated mode list (each 0..6)")
    p.add_argument("--arg", type=int, default=0, help="F3 byte5 param (likely interval)")
    p.set_defaults(func=cli_carousel)

    p = sub.add_parser("brightness", help="[experimental] set display brightness (E1)")
    p.add_argument("value", type=int)
    p.set_defaults(func=cli_brightness)

    sub.add_parser("poweroff-mode", help="[experimental] SetPCPowerOffMode (FA)") \
       .set_defaults(func=cli_poweroff_mode)

    p = sub.add_parser("raw", help='[experimental] send a raw command frame, e.g. "aa 01 02"')
    p.add_argument("hexbytes", help="opcode + params as hex bytes")
    p.set_defaults(func=cli_raw)

    p = sub.add_parser("raw-read", help='[experimental] send a command frame then read back, e.g. "eb 03"')
    p.add_argument("hexbytes")
    p.add_argument("--len", type=int, default=8, help="bytes to read back (default 8)")
    p.set_defaults(func=cli_raw_read)

    sub.add_parser("selftest", help="run the built-in encoder self-checks (no hardware)") \
       .set_defaults(func=cli_selftest)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not hasattr(args, "func"):
        ap.print_help()
        sys.exit(2)
    try:
        args.func(args)
    except ImportError as e:
        sys.exit(f"missing dependency: {e.name} (pip install Pillow)"
                 if e.name == "PIL" else str(e))
    except PermissionError:
        sys.exit("permission denied opening the i2c device — run as root or add "
                 "yourself to the 'i2c' group.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Sanity-run the CLI without hardware**

Run: `.venv/bin/python aorus_lcd.py selftest && .venv/bin/python aorus_lcd.py --help`
Expected: `9/9 passed`, exit 0; help lists all 13 subcommands.

- [ ] **Step 6: Commit**

```bash
git add aorus_lcd.py tests/test_encoders.py
git commit -m "Subcommand CLI, selftest, friendly errors"
```

---

### Task 7: README

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the final CLI surface from Task 6 (subcommand names and options must match `build_parser()` exactly — verify with `--help` before writing).

- [ ] **Step 1: Write `README.md`** following the approved spec outline. Required sections and load-bearing content (exact prose is the implementer's to write, but every point below must appear):

  1. **Title + one-liner**: control the "LCD Edge View" screen on the Gigabyte Aorus Master RTX 5090 from Linux — no Gigabyte software needed. A short demo-style example near the top (`sudo ./aorus_lcd.py image wallpaper.png`).
  2. **Status warning**: ALPHA; protocol reverse-engineered from Gigabyte Control Center i2c traffic + `ucVga.dll` decompile; tested on exactly one card (Aorus Master RTX 5090, legacy 0x61 protocol). Other Gigabyte LCD cards may use a different protocol (the newer 0x76 "LcdEx" one) — this tool will simply not find/ACK them or won't work; it never writes without an ACK at 0x61.
  3. **Safety**: writes only to i2c address 0x61 (the LCD controller); never touches 0x71 (the RGB controller); the GPU bus is found by sysfs adapter *name* (`NVIDIA i2c adapter 1 at ...`), never by guessing `/dev/i2c-0`; a zero-length probe must ACK before any write. Nothing here can flash firmware; worst observed failure mode is the panel showing the wrong content until the next upload/power cycle.
  4. **Install**: `sudo modprobe i2c-dev` (and how to persist via `/etc/modules-load.d/`), `python3 -m venv .venv && .venv/bin/pip install smbus2 Pillow`, run as root or add user to the `i2c` group.
  5. **Usage**: one copy-paste example per subcommand (`probe`, `on`/`off`, `mode`, `image`, `text` incl. `--color/--bg/--size/--no-effect`, `gif` incl. `--frame-delay`, `carousel`, `brightness` [experimental], `poweroff-mode` [experimental], `raw`/`raw-read` [experimental], `selftest`), plus `--bus N` override and `--no-mode`.
  6. **How it works / protocol reference** (short): 256-byte command frames `[opcode CB 55 AC 38 params...]`; upload sequence F2-BEGIN → F1 header → 256-byte chunks → F2-END; framebuffer targets (0x01300000 image / 0x01320000 text / 0x00000000 gif live buffer); mode table 0..6 with 3/4/5/6 named; gif payload = frame count + 10-byte table entries (inclusive end offset, w, h, fmt=3) + RLE blobs; RLE grammar (u16 LE head, bit15 run flag, 15-bit count); the mode-before vs mode-after ordering rule for gif vs image/text. Credit: reverse-engineered from GCC captures; no Gigabyte code included.
  7. **Troubleshooting**: `probe` says no NVIDIA bus (i2c-dev not loaded, or non-Gigabyte/unsupported card); permission denied (root / `i2c` group); gif shows black (use the default mode ordering — don't `--no-mode` then switch); panel shows nothing after boot (panel retains the last static image; run `mode 3` after boot to pin it — a systemd example may come later).
  8. **License**: MIT.

- [ ] **Step 2: Verify README examples against the real CLI**

Run: `.venv/bin/python aorus_lcd.py --help` and each subcommand's `--help`; confirm every example in the README parses (spot-check at least `image`, `text`, `gif` examples with `--help`-level dry reading — no hardware writes).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "README: usage, safety story, protocol reference, troubleshooting"
```

---

### Task 8: Hardware verification (this machine has the card)

**Files:** none (verification only; fix-forward commits if bugs surface).

**Interfaces:**
- Consumes: the finished tool. Root access via sudo on this host (the RTX 5090 machine).

- [ ] **Step 1: Probe**

Run: `sudo .venv/bin/python aorus_lcd.py probe`
Expected: `/dev/i2c-N (NVIDIA internal bus) @0x61: ACK (device present at 0x61)`, exit 0.

- [ ] **Step 2: Mode switch**

Run: `sudo .venv/bin/python aorus_lcd.py mode 3`
Expected: prints `SetMode 3`; panel switches to the stored static image.

- [ ] **Step 3: Static image upload**

Run:
```bash
.venv/bin/python -c "from PIL import Image; Image.effect_mandelbrot((320,170),(-2,-1.2,1,1.2),90).convert('RGB').save('/tmp/claude-1000/-home-alban-Dev-aorus-master-linux/5be17c99-84c5-4b6f-b2bd-fbde5bb18b53/scratchpad/test.png')"
sudo .venv/bin/python aorus_lcd.py image /tmp/claude-1000/-home-alban-Dev-aorus-master-linux/5be17c99-84c5-4b6f-b2bd-fbde5bb18b53/scratchpad/test.png
```
Expected: upload prints frame count, `done (SetMode 3)`; panel shows the image. **Ask the user to visually confirm.**

- [ ] **Step 4: Text upload**

Run: `sudo .venv/bin/python aorus_lcd.py text "AORUS ON LINUX"`
Expected: panel shows the text with the rainbow effect. **Ask the user to visually confirm.**

- [ ] **Step 5: GIF upload**

Run: `sudo .venv/bin/python aorus_lcd.py gif /home/alban/Dev/aorus-lcd-re/linux/test-move.gif`
Expected: panel shows "loading" during streaming, then plays the animation. **Ask the user to visually confirm.**

- [ ] **Step 6: Restore the user's previous panel content**

Run: `sudo .venv/bin/python aorus_lcd.py mode 3` (back to static image mode, the boot-service default on this host).

- [ ] **Step 7: Record results**

If any step fails: stop, diagnose with superpowers:systematic-debugging, fix, add a regression test where possible, commit the fix. When all pass, note pass/fail per step in the final report (no commit needed if no code changed).
