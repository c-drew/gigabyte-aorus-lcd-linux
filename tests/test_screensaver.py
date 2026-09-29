"""ttfx capture, cell glyphs, the grid renderer and the screensaver composer (no hardware, no ttfx)."""
import os

import pytest

from aorus_lcd import glyphs as G
from aorus_lcd import protocol as P
from aorus_lcd import screensaver as S
from aorus_lcd import ttfx

ESC = "\x1b"
LOGO = "  ##  \n ████ \n"


def _frame(rows):
    """A parity-dump frame: rows of (char, rgb or None)."""
    lines = []
    for row in rows:
        line = ""
        for ch, rgb in row:
            line += f"{ESC}[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{ch}{ESC}[0m" if rgb else ch
        lines.append(line)
    return "\n".join(lines).encode()


# ---- glyphs -------------------------------------------------------------------------

def test_block_elements_are_exact():
    assert G.glyph("█") == (0b1111,) * 8
    assert G.glyph("▀") == (0b1111,) * 4 + (0,) * 4
    assert G.glyph("▄") == (0,) * 4 + (0b1111,) * 4
    assert G.glyph("▌") == (0b1100,) * 8 and G.glyph("▐") == (0b0011,) * 8
    assert G.glyph("▚") == (0b1100,) * 4 + (0b0011,) * 4
    assert G.glyph(" ") == G.BLANK


def test_every_printable_ascii_char_is_drawn():
    for code in range(0x21, 0x7F):
        assert any(G.glyph(chr(code))), chr(code)


def test_box_drawing_and_fallbacks():
    assert G.glyph("─")[3] == 0b1111 and not any(G.glyph("─")[:3])
    assert all(row == 0b0100 for row in G.glyph("│"))
    assert G.glyph("┼")[3] == 0b1111
    assert G.glyph("é") == G.glyph("e")                  # accented letters use their base
    assert G.glyph("ｱ") == G.glyph("ｱ") and any(G.glyph("ｱ"))   # stable pattern for the rest
    assert G.brightness("▒") < 1 and G.brightness("A") == 1


# ---- ttfx capture -------------------------------------------------------------------

def test_parse_frame_colours_and_columns():
    raw = _frame([[(" ", None), ("x", (1, 2, 3)), ("y", None)]])
    assert ttfx.parse_frame(raw, default_rgb=(9, 9, 9)) == [[(1, "x", (1, 2, 3)), (2, "y", (9, 9, 9))]]


def test_dump_reads_frames_and_caps_them(fake_ttfx):
    assert ttfx.effects(fake_ttfx) == ("fx", "big", "huge", "slow")
    assert ttfx.version(fake_ttfx) == "9.9.9"
    assert len(list(ttfx.dump("x", "fx", 6, 3, binary=fake_ttfx))) == 5
    assert len(list(ttfx.dump("x", "big", 6, 3, binary=fake_ttfx, max_frames=7))) == 7


def test_dump_rejects_oversized_frames_and_times_out(fake_ttfx):
    with pytest.raises(ttfx.TtfxError, match="frame"):
        list(ttfx.dump("x", "huge", 6, 3, binary=fake_ttfx))
    with pytest.raises(ttfx.TtfxError, match="timed out"):
        list(ttfx.dump("x", "slow", 6, 3, binary=fake_ttfx, timeout=0.5))


# ---- grid and encoder -----------------------------------------------------------------

def test_grid_hides_narrow_columns_in_blank_text_columns():
    text = open(ttfx.DEFAULT_TEXT).read() if os.path.exists(ttfx.DEFAULT_TEXT) else "x" * 40 + "  " + "x" * 39
    grid = S.Grid(81, 21, S.blank_columns(text, 81))
    narrow = [c for c, w in enumerate(grid.ws) if w == G.CELL_W - 1]
    assert len(narrow) == 4 and sum(grid.ws) == P.W
    assert set(narrow) <= set(S.blank_columns(text, 81))
    assert grid.top == 1


def test_encoded_frames_decode_to_the_rendered_pixels():
    grid = S.Grid(81, 21)
    frame = [[(c, ch, (c * 3 % 256, 200, 90)) for c, ch in enumerate("█▀▄ab ▒─┼ｱé@" * 6)]] * 21
    px = grid.pixels(frame)
    assert len(px) == P.FRAME_BYTES
    assert P.rle_decode_frame(grid.encode(frame)) == px
    assert P.rle_decode_frame(grid.encode([])) == bytes(P.FRAME_BYTES)


def test_encode_runs_splits_long_runs_and_literals():
    colors = [1, 2] * 20000 + [3]
    counts = [1] * 40000 + [P.FRAME_PIXELS - 40000]
    blob = S.encode_runs(colors, counts)
    decoded = P.rle_decode_frame(blob)
    assert decoded[:4] == b"\x01\x00\x02\x00" and decoded[-2:] == b"\x03\x00"
    assert S.BLACK_FRAME == (0x8000 | 0x7FFF).to_bytes(2, "little") + b"\0\0" \
        + (0x8000 | (P.FRAME_PIXELS - 0x7FFF)).to_bytes(2, "little") + b"\0\0"


def test_resampling_keeps_every_nth_frame():
    kept = [i for i in range(12) if S.shown(i, 30)]
    assert kept == [0, 4, 8]


# ---- composer -------------------------------------------------------------------------

def test_build_packs_effects_and_holds(fake_ttfx):
    anim = S.build(LOGO, ("fx", "fx"), fps=60, hold_ms=100, binary=fake_ttfx, cols=6, rows=3)
    # 5 source frames at 120 fps -> frames 0, 2, 4 at 60 fps, then a 6-frame hold, per effect
    assert [len(c.blobs) for c in anim.clips] == [9, 9]
    assert anim.frames[8] == anim.frames[3] and anim.blobs[0] == S.BLACK_FRAME
    upload = anim.upload()
    assert upload.nframes == 19 and upload.delay_ms == 17


def test_build_respects_the_frame_limit(fake_ttfx):
    anim = S.build(LOGO, ("big", "fx"), fps=120, hold_ms=0, binary=fake_ttfx, cols=6, rows=3)
    assert [c.effect for c in anim.clips] == ["fx"] and anim.skipped == [("big", 400)]
    with pytest.raises(ValueError, match="frames"):
        S.build(LOGO, ("big",), fps=120, hold_ms=0, binary=fake_ttfx, cols=6, rows=3)


def test_unknown_effects_and_oversized_text_are_refused(fake_ttfx):
    with pytest.raises(ValueError, match="unknown"):
        S.build(LOGO, ("nope",), binary=fake_ttfx, cols=6, rows=3)
    with pytest.raises(ValueError, match="characters"):
        S.build("x" * 100, ("fx",), binary=fake_ttfx, cols=6, rows=3)


def test_gif_uploads_are_capped():
    with pytest.raises(ValueError, match="frames"):
        P.gif_upload_encoded([S.BLACK_FRAME] * (P.GIF_MAX_FRAMES + 1), 33)
    with pytest.raises(ValueError, match="at most"):
        P.gif_upload_encoded([S.BLACK_FRAME, b"\0" * P.GIF_MAX_PAYLOAD], 33)


def _play(blobs_in_order):
    """What the panel shows for each frame: a frame paints its pixels from the
    top-left and the rest of the screen keeps the previous picture."""
    screen, shown = bytearray(P.FRAME_BYTES), []
    for blob in blobs_in_order:
        out, i = bytearray(), 0
        while i < len(blob):
            head = int.from_bytes(blob[i:i + 2], "little")
            count = head & 0x7FFF
            if head & 0x8000:
                out += blob[i + 2:i + 4] * count
                i += 4
            else:
                out += blob[i + 2:i + 2 + 2 * count]
                i += 2 + 2 * count
        screen[:len(out)] = out
        shown.append(bytes(screen))
    return shown


def test_delta_frames_show_exactly_the_full_frames():
    grid = S.Grid(81, 21)
    def frame(cells):
        return grid.encode([[(c, ch, (255, 0, 0)) for c, ch in cells]] + [[]] * 20)
    full = [frame([(1, "█")]), frame([(1, "█")]), frame([(1, "█"), (40, "a")]), frame([(2, "▀")]),
            BLACK_WITH_BOTTOM := grid.encode([[]] * 20 + [[(80, "█", (0, 255, 0))]]), BLACK_WITH_BOTTOM]
    delta = S.delta_frames(full)
    assert _play(delta) == [P.rle_decode_frame(b) for b in full]
    assert delta[0] == full[0] and len(delta[1]) == 4               # a repeat costs one 1-pixel run
    assert len(delta[3]) < len(full[3]) and len(delta[5]) == 4


def test_loops_wrap_and_start_on_whole_frames():
    """Played order after an upload: frame 1 first (on top of anything), then
    around the loop back to frame 1; frame 0 is never shown (its start is
    computed wrongly by the firmware: see protocol.GIF_MAX_FRAMES)."""
    grid = S.Grid(81, 21)
    frames = [grid.encode([[(k % 81, "█", (k, 0, 0))]]) for k in range(P.GIF_MAX_PLAYED)]
    anim = S.Animation([], [], 18, frames, S.frame_delay(18))
    blobs = anim.blobs
    assert len(blobs) == P.GIF_MAX_FRAMES and blobs[0] == S.BLACK_FRAME
    order = blobs[1:] + blobs[1:3]                                   # two passes of the start
    garbage = [S.encode_runs([0xFFFF], [P.FRAME_PIXELS])]            # whatever was on screen
    shown = _play(garbage + order)[1:]
    expect = [P.rle_decode_frame(f) for f in frames] + [P.rle_decode_frame(f) for f in frames[:2]]
    assert shown == expect


def test_frame_delays_match_whole_draws():
    """A whole frame takes ~55 ms to draw and shows for the whole draws that reach
    the delay; delta frames show for about the delay. Both should agree."""
    draw = 55
    for fps in S.FPS_CHOICES:
        delay = S.frame_delay(fps)
        whole = -(-delay // draw) * draw
        assert abs(whole - 1000 / fps) < 8 and delay < 1000 / fps


def test_text_size_fits_the_text():
    logo = "x" * 81 + "\n" + "x" * 81
    assert S.fitting_scale(logo) == 1 and S.fitting_scale("hi") == 8
    assert S.fitting_scale("hello world") == 6                     # 11 x 24 px <= 92% of 320
    assert S.fitting_scale("hello world, from linux!") == 3        # 24 x 12 px
    grid = S.make_grid("hello world", scale=0)
    assert grid.scale == 6 and grid.cw == 24 and grid.ch == 48 and grid.cols >= 11
    one, two = S.Grid(1, 1, scale=1), S.Grid(1, 1, scale=2)
    a = one.pixels([[(0, "A", (255, 255, 255))]])
    b = two.pixels([[(0, "A", (255, 255, 255))]])
    def lit(px, x, y):
        return px[2 * (y * P.W + x): 2 * (y * P.W + x) + 2] != b"\0\0"
    for y in range(8):                                             # 2x: the pixel font, doubled
        for x in range(4):
            assert lit(a, one.xs[0] + x, one.top + y) == lit(b, two.xs[0] + 2 * x + 1, two.top + 2 * y + 1)
    with pytest.raises(ValueError, match="at 4x"):
        S.make_grid("x" * 30, scale=4)


def test_big_text_uses_real_letterforms_and_geometric_blocks():
    from aorus_lcd import glyphs as G
    if G._font_file() is None:
        pytest.skip("no TrueType monospace font")
    a = G.bitmap("A", 24, 48)
    assert a != G._scaled(G.glyph("A"), 24, 48)                  # drawn from the font
    assert G.bitmap("█", 24, 48) == ((1 << 24) - 1,) * 48          # blocks stay exact
    assert G.bitmap("ｱ", 24, 48) == G._scaled(G.glyph("ｱ"), 24, 48)   # not in the font


def test_wordmark_is_the_omarchy_logo_lettering():
    from aorus_lcd import wordmark as W
    if os.path.exists("/usr/share/omarchy/logo.txt"):
        with open("/usr/share/omarchy/logo.txt", encoding="utf-8") as f:
            assert W.wordmark("OMARCHY").rstrip("\n") == f.read().rstrip("\n")
    art = W.wordmark("hi\nyo")
    assert art == W.wordmark("HI\nYO") and set(art) <= set("█▀▄ \n")
    assert 16 <= len(art.rstrip("\n").split("\n")) <= 20            # two lines of up to 10 rows
    # H's crossbar reaches a pixel left of its stem, even at the start of a line
    assert "\n▀▀███▀▀▀███" in "\n" + W.wordmark("H")


def test_screensaver_sources(tmp_path, monkeypatch):
    from aorus_lcd import config as C, content as CT, wordmark as W
    logo = tmp_path / "logo.txt"
    logo.write_text("LOGO\n")
    monkeypatch.setattr(ttfx, "DEFAULT_TEXT", str(logo))
    def text(**c):
        return CT._ttfx_text(C.parse({"content": {"type": "ttfx", **c}}).content)
    assert text() == "LOGO\n" and text(ttfx_text="hi") == "hi"      # the old fields keep working
    assert text(ttfx_source="logo", ttfx_text="hi") == "LOGO\n"
    assert text(ttfx_source="wordmark", ttfx_text="hi") == W.wordmark("hi")
    if W._mark_font():
        mark = text(ttfx_source="mark")
        assert len(mark.split("\n")[0]) == 40 and mark == W.mark(20)
        assert len(text(ttfx_source="mark", border=[0, 20, 0, 20]).rstrip("\n").split("\n")) == 15


def test_a_playlist_loop_is_its_runs_and_pauses_not_padded(fake_ttfx):
    """The panel goes straight back to the effect after each pause on the text:
    a loop is its runs and their holds, however short (it used to be padded to
    a fixed length, which left the finished text up for ~10 s)."""
    anim = S.build_loop("hi\n", "fx", hold_ms=500, binary=fake_ttfx, max_runs=2)
    hold = round(500 * S.PANEL_FPS / 1000)
    run = len(anim.clips[0].blobs)
    assert len(anim.clips) == 2 and len(anim.frames) == 2 * (run + hold) < S.LOOP_FRAMES
    assert anim.frames[run + hold - 1] == anim.frames[-1] == anim.clips[0].blobs[-1]   # each run ends held
    assert len(anim.blobs) == len(anim.frames) + 1                                   # + frame 0, never shown


def test_a_ttfx_installed_alongside_wins_over_the_systems(tmp_path, monkeypatch, fake_ttfx):
    import sys
    from aorus_lcd import ttfx
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert ttfx.find_binary() != str(tmp_path / "bin" / "ttfx")      # none there: PATH's
    (tmp_path / "bin").mkdir()
    bundled = tmp_path / "bin" / "ttfx"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    assert ttfx.find_binary() == str(bundled)
    assert ttfx.find_binary(fake_ttfx) == fake_ttfx                 # an explicit path still wins
