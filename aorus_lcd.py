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
