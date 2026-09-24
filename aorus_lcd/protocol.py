"""The LCD's "legacy 0x61" protocol as pure byte builders (no I/O).

Recovered from Gigabyte Control Center traffic and ucVga.dll by
albancreton/aorus-master-linux and CodeTorchAI/AorusLcd. Every command is a
frame [opcode, CB 55 AC 38, params...] zero-padded to 256 bytes. Content
uploads are F2(BEGIN) -> F1 header -> 256-byte chunks -> F2(END), where the
payload is a frame table followed by the pixel blobs.
"""
from dataclasses import dataclass

W, H = 320, 170
FRAME_PIXELS = W * H
FRAME_BYTES = FRAME_PIXELS * 2              # little-endian RGB565
FRAME_SIZE = 256
MAGIC = bytes([0xCB, 0x55, 0xAC, 0x38])

# Opcodes (GvLcdApi)
OP_OPEN_LCD = 0xE7        # [5] 1 = on, 2 = off
OP_SET_MODE = 0xE5        # [5] mode + 1
OP_SET_DISPLAY = 0xE1     # overlay widget flags [5..12] + rotation interval [13]
OP_SENSOR_FEED = 0xE3     # live values for the overlay widgets
OP_SET_TEMPLATE = 0xEA    # overlay colour and positions
OP_SAVE = 0xAA            # persist config to panel NVRAM (also the text effect)
OP_SET_LOOP = 0xF3        # carousel
OP_UPLOAD_MARK = 0xF2     # [5] 1 = BEGIN, 2 = END
OP_UPLOAD_HEADER = 0xF1
OP_GET_FW = 0xD6          # read 4
OP_GET_MODE = 0xDE        # read 4
OP_GET_DISPLAY = 0xDF     # read 4
OP_GET_TEMPLATE = 0xEB    # [5] type, read 4 (EB 03 doubles as presence probe)
OP_GET_TEMPLATE2 = 0xED   # read 4

# Display modes (E5 / DE)
MODE_FAITH1, MODE_FAITH2, MODE_FAITH3 = 0, 1, 2
MODE_IMAGE, MODE_TEXT, MODE_GIF, MODE_CHIBI = 3, 4, 5, 6

# Upload framebuffer targets (F1 header)
FB_IMAGE = 0x01300000
FB_TEXT = 0x01320000
FB_GIF = 0x00000000       # live buffer: SetMode 5 must come BEFORE streaming

# Frame-table pixel formats
FMT_RAW = 1               # uncompressed LE RGB565 (what GCC uses for image/text)
FMT_RLE = 3               # Compress_RLE (what GCC uses for gif frames)

# Pacing observed in GCC captures (seconds)
PACE_BEGIN, PACE_HEADER, PACE_CHUNK = 0.5, 1.0, 0.01

# F1 header byte 17 picks how the panel erases its flash before writing
# (EKYavsil's firmware analysis): 1 = 4 KB sectors, 2 = 64 KB blocks. GCC uses
# 2 for anything >= 20480 bytes, and the firmware's 64 KB path gives up on
# the erase too early: the tail of the upload is lost (half-drawn images, GIFs
# stuck on "Loading"). This project always uses 4 KB sectors.
ERASE_SECTOR, ERASE_BLOCK = 1, 2
ERASE_UNIT = {ERASE_SECTOR: 0x1000, ERASE_BLOCK: 0x10000}
ERASE_SECONDS = {ERASE_SECTOR: 0.1, ERASE_BLOCK: 0.6}     # generous per-erase budget

# Overlay widgets (E1 flag order == bit order used by DF read-back)
WIDGETS = ("temp", "clock", "usage", "fan", "vram_clock", "vram", "fps", "power")
TEMPLATE_GIF, TEMPLATE_IMAGE, TEMPLATE_PET = 1, 2, 3


def cmd_frame(opcode, tail=b""):
    """[opcode, CB 55 AC 38, tail...] zero-padded to 256 bytes."""
    if len(tail) > FRAME_SIZE - 5:
        raise ValueError("command tail too long")
    return bytes([opcode]) + MAGIC + bytes(tail) + bytes(FRAME_SIZE - 5 - len(tail))


def upload_marker(begin):
    return cmd_frame(OP_UPLOAD_MARK, bytes([1 if begin else 2]))


def gcc_erase_mode(payload_len):
    """What Gigabyte Control Center sends in F1 byte 17."""
    return ERASE_BLOCK if payload_len >= 20480 else ERASE_SECTOR


def erase_count(payload_len, erase_mode):
    """How many erases the firmware performs before it accepts data."""
    return (chunk_count(payload_len) * FRAME_SIZE) // ERASE_UNIT[erase_mode] + 1


def header_pause(payload_len, erase_mode):
    """Seconds to wait after F1 so every erase finishes before data arrives
    (GCC always waits 1.0 s, too short for big uploads)."""
    return max(PACE_HEADER, 0.3 + erase_count(payload_len, erase_mode) * ERASE_SECONDS[erase_mode])


def upload_header(fb_addr, payload_len, flag=1, nframes=0, delay_ms=0, erase_mode=None):
    """F1 header. `erase_mode` None means what GCC would send."""
    if erase_mode is None:
        erase_mode = gcc_erase_mode(payload_len)
    tail = (fb_addr.to_bytes(4, "big") + bytes([flag])
            + chunk_count(payload_len).to_bytes(4, "big") + nframes.to_bytes(2, "big")
            + bytes([min(255, max(0, delay_ms)), erase_mode, 0]))
    return cmd_frame(OP_UPLOAD_HEADER, tail)


def chunk_count(payload_len):
    """GCC sends len//256 + 1 chunks: an exact multiple still gets a pad chunk."""
    return payload_len // FRAME_SIZE + 1


def chunks(payload):
    padded = payload + bytes(chunk_count(len(payload)) * FRAME_SIZE - len(payload))
    return [padded[i:i + FRAME_SIZE] for i in range(0, len(padded), FRAME_SIZE)]


def frame_table(sizes, fmt, w=W, h=H):
    """[count u16][per frame: inclusive end offset u32, w u16, h u16, fmt u16], LE.
    For one raw frame this is GCC's constant image/text descriptor."""
    out = bytearray(len(sizes).to_bytes(2, "little"))
    end = 2 + 10 * len(sizes)
    for size in sizes:
        end += size
        out += ((end - 1).to_bytes(4, "little") + w.to_bytes(2, "little")
                + h.to_bytes(2, "little") + fmt.to_bytes(2, "little"))
    return bytes(out)


def rle_encode_frame(px):
    """RLE-encode one LE-RGB565 frame byte-identically to GCC's Compress_RLE.

    Token head = u16 LE: bit 15 = run flag, low 15 bits = pixel count.
      run:     <count|0x8000> <pixel>        (only for >= 3 equal pixels)
      literal: <count> <count pixels>
    The scan works in windows of <= 0x7FFF pixels; within a window the
    literal extends to the first >= 3-pixel repeat and the run never crosses
    the window end; a window with no repeat, or fewer than 4 pixels left, is
    one literal. The firmware decoder mirrors these quirks, so keep them.
    (Validated 60/60 frames against GCC's animation.bin by albancreton.)"""
    mv = memoryview(px).cast("H")
    n = len(mv)
    if n < 3:
        raise ValueError("frame too small for Compress_RLE semantics")
    out = bytearray()
    i = 0
    while i < n:
        wend = i + min(0x7FFF, n - i)
        wlen = wend - i
        if wlen < 4:
            diff, same = wlen, 0
        else:
            j = i
            while True:
                if j + 2 == wend:
                    diff, same = wlen, 0
                    break
                if mv[j] == mv[j + 1] == mv[j + 2]:
                    start = j
                    j += 2
                    while j < wend - 1 and mv[j] == mv[j + 1]:
                        j += 1
                    diff, same = start - i, j + 1 - start
                    break
                j += 1
        if diff:
            out += diff.to_bytes(2, "little") + px[2 * i:2 * (i + diff)]
        if same:
            out += (same | 0x8000).to_bytes(2, "little") + px[2 * (i + diff):2 * (i + diff) + 2]
        i += diff + same
    return bytes(out)


def rle_decode_frame(blob, pixels=FRAME_PIXELS):
    """Inverse of rle_encode_frame (for tests and sanity checks)."""
    out = bytearray()
    i = 0
    while i < len(blob):
        head = int.from_bytes(blob[i:i + 2], "little")
        count = head & 0x7FFF
        if head & 0x8000:
            out += blob[i + 2:i + 4] * count
            i += 4
        else:
            out += blob[i + 2:i + 2 + 2 * count]
            i += 2 + 2 * count
    if len(out) != 2 * pixels:
        raise ValueError(f"decoded {len(out) // 2} pixels, expected {pixels}")
    return bytes(out)


@dataclass(frozen=True)
class Upload:
    """A content upload: which framebuffer, which mode shows it, the payload."""
    kind: str             # "image", "text" or "gif"
    fb_addr: int
    mode: int
    payload: bytes
    flag: int = 1
    nframes: int = 0
    delay_ms: int = 0
    erase_mode: int = ERASE_SECTOR

    @property
    def frames(self):
        return ([upload_marker(True),
                 upload_header(self.fb_addr, len(self.payload), self.flag, self.nframes,
                               self.delay_ms, self.erase_mode)]
                + chunks(self.payload) + [upload_marker(False)])

    @property
    def header_pause(self):
        return header_pause(len(self.payload), self.erase_mode or gcc_erase_mode(len(self.payload)))

    def estimate_seconds(self, chunk_delay=PACE_CHUNK, write_seconds=0.0065):
        """Rough wall time: pacing plus ~6.5 ms per 256-byte write at 400 kHz."""
        n = chunk_count(len(self.payload))
        return PACE_BEGIN + self.header_pause + n * (chunk_delay + write_seconds) + 0.8

    @property
    def mode_first(self):
        """GIF streams into a live buffer: select the mode before uploading."""
        return self.kind == "gif"


def still_upload(pixels, kind="image"):
    """GCC's image (mode 3) / text (mode 4) framebuffer upload, uncompressed.
    Kept for reference: on LCD firmware 1.3 these uploads can complete without
    drawing anything, and they do not decode RLE, so content.still() sends
    stills as two-frame GIFs instead."""
    if len(pixels) != FRAME_BYTES:
        raise ValueError(f"expected {FRAME_BYTES} bytes of pixels")
    fb, mode = (FB_TEXT, MODE_TEXT) if kind == "text" else (FB_IMAGE, MODE_IMAGE)
    return Upload(kind, fb, mode, frame_table([len(pixels)], FMT_RAW) + pixels)


def gif_upload(frames, delay_ms):
    """Animated upload from LE-RGB565 frames (always RLE, like GCC)."""
    blobs = [rle_encode_frame(f) for f in frames]
    payload = frame_table([len(b) for b in blobs], FMT_RLE) + b"".join(blobs)
    return Upload("gif", FB_GIF, MODE_GIF, payload, flag=2, nframes=len(frames),
                  delay_ms=min(255, max(1, int(delay_ms))))


# ---- commands ---------------------------------------------------------------------

def open_lcd(on):
    return cmd_frame(OP_OPEN_LCD, bytes([1 if on else 2]))


def set_mode(mode):
    """Modes 0..6; 7 (built-in carousel) is sent as internal 9 like GCC."""
    if not 0 <= mode <= 7:
        raise ValueError("mode must be 0..7")
    return cmd_frame(OP_SET_MODE, bytes([(9 if mode == 7 else mode) + 1]))


def set_loop(modes, interval=0):
    """F3 carousel; an empty list disables it."""
    return cmd_frame(OP_SET_LOOP, bytes([interval & 0xFF] + [m + 1 for m in modes if 0 <= m <= 6]))


def set_display(widgets, interval_s=3):
    """E1: which overlay widgets the firmware rotates through, and how often.
    No widgets turns the overlay off."""
    unknown = set(widgets) - set(WIDGETS)
    if unknown:
        raise ValueError(f"unknown widgets {sorted(unknown)}; choose from {', '.join(WIDGETS)}")
    flags = bytes(1 if w in widgets else 0 for w in WIDGETS)
    interval = max(0, min(255, interval_s)) if widgets else 0
    return cmd_frame(OP_SET_DISPLAY, flags + bytes([interval]))


def set_template(color, image_pos=(0, 0), data_pos=(0, 0), enabled=True, kind=TEMPLATE_IMAGE):
    """EA: overlay colour (r, g, b) and positions, 16-bit big-endian."""
    r, g, b = color
    tail = bytes([kind, r, g, b])
    for v in (*image_pos, *data_pos):
        tail += max(0, min(0xFFFF, int(v))).to_bytes(2, "big")
    return cmd_frame(OP_SET_TEMPLATE, tail + bytes([1 if enabled else 0]))


@dataclass
class Sample:
    """One reading for the E3 feed. Units: °C, MHz, %, RPM (or %), MHz, %, fps, W."""
    temp: int = 0
    clock: int = 0
    usage: int = 0
    fan: int = 0
    vram_clock: int = 0
    vram: int = 0
    fps: int = 0
    power: int = 0


def _u8(v):
    return max(0, min(255, int(round(v))))


def _u16(v):
    return max(0, min(0xFFFF, int(round(v)))).to_bytes(2, "big")


def sensor_feed(s):
    """E3: [temp][clock u16][usage][fan u16][vram clock u16][vram %][fps u16][power u16], BE."""
    tail = (bytes([_u8(s.temp)]) + _u16(s.clock) + bytes([_u8(s.usage)]) + _u16(s.fan)
            + _u16(s.vram_clock) + bytes([_u8(s.vram)]) + _u16(s.fps) + _u16(s.power))
    return cmd_frame(OP_SENSOR_FEED, tail)


def save():
    return cmd_frame(OP_SAVE)


def query(opcode, tail=b""):
    """Query frames are ordinary commands; the reply is read separately (4 bytes)."""
    return cmd_frame(opcode, tail)


def parse_mode(reply):
    """DE reply -> (mode, on)."""
    mode = reply[1] - 1
    return (7 if mode == 9 else mode), reply[2] == 1


def parse_display(reply):
    """DF reply -> (widgets, interval)."""
    return [w for i, w in enumerate(WIDGETS) if reply[1] & (1 << i)], reply[2]


def parse_firmware(reply):
    """D6 reply -> 'major.minor'."""
    return f"{reply[1] >> 4}.{reply[1] & 0xF}"
