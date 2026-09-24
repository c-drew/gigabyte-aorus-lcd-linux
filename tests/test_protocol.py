"""Protocol byte builders. The encoder tests are ported from
albancreton/aorus-master-linux, whose layouts were verified against GCC captures."""
import os
import struct

import pytest

from aorus_lcd import protocol as P

F2 = bytes([0xF2, 0xCB, 0x55, 0xAC, 0x38])
F1 = bytes([0xF1, 0xCB, 0x55, 0xAC, 0x38])


def _px(*words):
    return b"".join(w.to_bytes(2, "little") for w in words)


def test_cmd_frame():
    fr = P.cmd_frame(0xE5, b"\x04")
    assert len(fr) == 256 and fr[:6] == bytes([0xE5]) + P.MAGIC + b"\x04"
    assert not any(fr[6:])


def test_upload_markers():
    assert P.upload_marker(True)[:6] == F2 + b"\x01"
    assert P.upload_marker(False)[:6] == F2 + b"\x02"


def test_upload_header_raw_still_image():
    usize = 12 + P.FRAME_BYTES
    h = P.upload_header(P.FB_IMAGE, usize)
    assert h[:5] == F1 and h[5:9] == (0x01300000).to_bytes(4, "big") and h[9] == 1
    assert int.from_bytes(h[10:14], "big") == 426
    assert int.from_bytes(h[14:16], "big") == 0 and h[16] == 0 and h[17] == 2 and h[18] == 0
    assert not any(h[19:])


def test_upload_header_gif_and_delay_clamp():
    h = P.upload_header(P.FB_GIF, 2500, flag=2, nframes=7, delay_ms=999, erase_mode=2)
    assert h[5:9] == bytes(4) and h[9] == 2 and int.from_bytes(h[14:16], "big") == 7
    assert h[16] == 255 and h[17] == 2


def test_small_payload_uses_sector_erase():
    assert P.upload_header(P.FB_IMAGE, 300)[17] == 1


def test_chunks_pad_and_count():
    p = bytes(range(256)) * 2 + b"\xAA" * 10
    c = P.chunks(p)
    assert len(c) == 3 and all(len(x) == 256 for x in c)
    assert b"".join(c)[:len(p)] == p and not any(b"".join(c)[len(p):])


def test_exact_multiple_gets_a_pad_chunk():
    c = P.chunks(b"\x11" * 512)
    assert len(c) == 3 and c[2] == bytes(256)


def test_raw_frame_table_is_gccs_descriptor():
    assert P.frame_table([P.FRAME_BYTES], P.FMT_RAW) == bytes(
        [0x01, 0x00, 0x0B, 0xA9, 0x01, 0x00, 0x40, 0x01, 0xAA, 0x00, 0x01, 0x00])


def test_frame_table_offsets():
    t = P.frame_table([10, 20], P.FMT_RLE)
    assert struct.unpack_from("<H", t)[0] == 2 and len(t) == 22
    assert struct.unpack_from("<IHHH", t, 2) == (31, P.W, P.H, 3)
    assert struct.unpack_from("<IHHH", t, 12) == (51, P.W, P.H, 3)


def test_rle_run():
    assert P.rle_encode_frame(_px(*[0x1234] * 6)) == b"\x06\x80" + _px(0x1234)


def test_rle_literal():
    assert P.rle_encode_frame(_px(1, 2, 1, 2, 1, 2)) == b"\x06\x00" + _px(1, 2, 1, 2, 1, 2)


def test_rle_short_tail_is_literal_even_if_equal():
    assert P.rle_encode_frame(_px(7, 7, 7)) == b"\x03\x00" + _px(7, 7, 7)


def test_rle_literal_then_run():
    got = P.rle_encode_frame(_px(1, 2) + _px(*[9] * 4) + _px(5, 6, 7, 8, 5, 6))
    assert got == (b"\x02\x00" + _px(1, 2) + b"\x04\x80" + _px(9) + b"\x06\x00" + _px(5, 6, 7, 8, 5, 6))


def test_rle_round_trip():
    px, v = bytearray(), 12345
    for _ in range(P.FRAME_PIXELS):
        v = (v * 1103515245 + 12345) & 0x7FFFFFFF
        px += ((v >> 7) & 0xFFFF).to_bytes(2, "little")
    assert P.rle_decode_frame(P.rle_encode_frame(bytes(px))) == bytes(px)


def test_rle_rejects_tiny_frames():
    with pytest.raises(ValueError):
        P.rle_encode_frame(_px(1, 2))


def test_still_upload_is_uncompressed():
    up = P.still_upload(bytes(P.FRAME_BYTES), "text")
    assert up.mode == P.MODE_TEXT and up.fb_addr == P.FB_TEXT
    assert up.payload[:12] == P.frame_table([P.FRAME_BYTES], P.FMT_RAW)
    assert len(up.frames) == 2 + 426 + 1


def test_gif_upload_shape():
    frames = [bytes(P.FRAME_BYTES), b"\xff" * P.FRAME_BYTES]
    up = P.gif_upload(frames, 80)
    assert up.mode_first and up.fb_addr == P.FB_GIF and up.nframes == 2
    hdr = up.frames[1]
    assert hdr[9] == 2 and int.from_bytes(hdr[14:16], "big") == 2 and hdr[16] == 80
    assert hdr[17] == P.ERASE_SECTOR, "4 KB sector erase, not GCC's flaky 64 KB path"
    body = b"".join(up.frames[2:-1])
    assert body[:len(up.payload)] == up.payload and not any(body[len(up.payload):])
    pos = 2 + 10 * 2
    for i in range(2):
        end = struct.unpack_from("<I", up.payload, 2 + 10 * i)[0]
        assert P.rle_decode_frame(up.payload[pos:end + 1]) == frames[i]
        pos = end + 1


def test_set_display_flags_and_interval():
    fr = P.set_display(["temp", "usage", "power"], 5)
    assert fr[0] == 0xE1 and list(fr[5:14]) == [1, 0, 1, 0, 0, 0, 0, 1, 5]
    assert not any(P.set_display([])[5:14])
    with pytest.raises(ValueError):
        P.set_display(["bogus"])


def test_set_template_layout():
    fr = P.set_template((255, 0, 16), (1, 2), (146, 64), True, P.TEMPLATE_IMAGE)
    assert fr[0] == 0xEA and list(fr[5:9]) == [2, 255, 0, 16]
    assert fr[9:17] == bytes([0, 1, 0, 2, 0, 146, 0, 64]) and fr[17] == 1


def test_sensor_feed_layout_and_clamping():
    fr = P.sensor_feed(P.Sample(temp=300, clock=2955, usage=97, fan=1450, vram_clock=14001,
                                vram=41, fps=0, power=465))
    assert fr[0] == 0xE3 and fr[5] == 255
    assert fr[6:8] == (2955).to_bytes(2, "big") and fr[8] == 97
    assert fr[9:11] == (1450).to_bytes(2, "big") and fr[11:13] == (14001).to_bytes(2, "big")
    assert fr[13] == 41 and fr[14:16] == bytes(2) and fr[16:18] == (465).to_bytes(2, "big")


def test_mode_frames_and_parsers():
    assert P.set_mode(3)[5] == 4 and P.set_mode(7)[5] == 10
    assert P.parse_mode(bytes([0xDE, 4, 1, 1])) == (3, True)
    assert P.parse_mode(bytes([0xDE, 10, 0, 1])) == (7, False)
    assert P.parse_display(bytes([0xDF, 0x87, 3, 1])) == (["temp", "clock", "usage", "power"], 3)
    assert P.parse_firmware(bytes([0xD6, 0x13, 1, 2])) == "1.3"


def test_encoder_matches_gcc_ground_truth():
    """Re-encode every blob of GCC's own animation.bin byte-identically.
    Skips unless $AORUS_ANIM_BIN points at it (not redistributable)."""
    path = os.environ.get("AORUS_ANIM_BIN")
    if not path or not os.path.exists(path):
        pytest.skip("set $AORUS_ANIM_BIN to GCC's Assets/animation.bin")
    anim = open(path, "rb").read()
    n = struct.unpack_from("<H", anim)[0]
    pos = 2 + 10 * n
    for i in range(n):
        end = struct.unpack_from("<I", anim, 2 + 10 * i)[0]
        blob = anim[pos:end + 1]
        assert P.rle_encode_frame(P.rle_decode_frame(blob)) == blob, f"frame {i}"
        pos = end + 1


def test_header_pause_covers_every_erase():
    assert P.erase_count(12 + P.FRAME_BYTES, P.ERASE_BLOCK) == 2        # GCC's static upload
    assert P.erase_count(127114, P.ERASE_SECTOR) == 32
    assert P.header_pause(1000, P.ERASE_SECTOR) == P.PACE_HEADER         # small uploads: GCC timing
    assert P.header_pause(127114, P.ERASE_SECTOR) >= 32 * 0.1
    assert P.still_upload(bytes(P.FRAME_BYTES)).frames[1][17] == P.ERASE_SECTOR


def test_stills_are_sent_as_single_frame_gifs():
    from aorus_lcd import config as C, content
    up = content.build(C.ContentConfig(type="text", text="hi"))
    assert up.kind == "gif" and up.mode == P.MODE_GIF and up.nframes == 1
    assert len(up.frames) < 20, "RLE keeps a mostly-black still tiny"


def test_text_wave_uses_the_panels_text_mode_like_gcc():
    from aorus_lcd import config as C, content
    up = content.build(C.ContentConfig(type="text", text="hi", effect="wave"))
    assert up.mode == P.MODE_TEXT and up.fb_addr == P.FB_TEXT and up.erase_mode == P.ERASE_BLOCK
    assert up.frames[1][17] == P.ERASE_BLOCK
