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


# ---- pixel encoders (need Pillow) ----------------------------------------------

def image_to_le565(im):
    """PIL RGB image already sized (W, H) -> little-endian RGB565 bytes."""
    rgb = im.tobytes("raw", "RGB")
    out = bytearray(FRAME_BYTES)
    j = 0
    for i in range(0, len(rgb), 3):
        v = ((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3) | (rgb[i + 2] >> 3)
        out[j] = v & 0xFF
        out[j + 1] = (v >> 8) & 0xFF
        j += 2
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
    frames, delays = [], []
    with Image.open(path) as im:
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            fr = im.convert("RGB").resize((W, H), Image.LANCZOS)
            frames.append(image_to_le565(fr))
            delays.append(im.info.get("duration", 100))
    return frames, delays


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
