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
