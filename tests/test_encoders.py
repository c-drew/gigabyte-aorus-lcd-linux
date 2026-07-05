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
