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
