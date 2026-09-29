"""Terminal characters as 4x8-pixel cell bitmaps, for drawing a text grid on the panel.

At 81 columns across 320 px a terminal cell is 4 px wide, so glyphs are drawn,
not rasterised from a font: block elements and box drawing geometrically,
ASCII from a 3x5 pixel font, accented letters as their base letter, and
anything else (katakana in `matrix`, stray symbols) as a stable pseudo-random
pattern, which reads as glyph noise at this size anyway.

A glyph is 8 row masks, 4 bits each (bit 3 = leftmost pixel). Shades
(░▒▓) are solid cells drawn dimmer: smoother than a dither on a 4 px cell,
and runs compress far better than alternating pixels.
"""
import functools
import hashlib
import operator
import unicodedata

CELL_W, CELL_H = 4, 8
FULL = (1 << CELL_W) - 1
BLANK = (0,) * CELL_H

# 3x5 pixel font; a sixth row is the descender. Drawn at x 0..2, y 1..6.
_FONT = {
    "!": ".#. .#. .#. ... .#.", '"': "#.# #.# ... ... ...", "#": "#.# ### #.# ### #.#",
    "$": ".## ##. .#. .## ##.", "%": "#.# ..# .#. #.. #.#", "&": ".#. #.# .#. #.# .##",
    "'": ".#. .#. ... ... ...", "(": "..# .#. .#. .#. ..#", ")": "#.. .#. .#. .#. #..",
    "*": "... #.# .#. #.# ...", "+": "... .#. ### .#. ...", ",": "... ... ... .#. #..",
    "-": "... ... ### ... ...", ".": "... ... ... ... .#.", "/": "..# ..# .#. #.. #..",
    "0": "### #.# #.# #.# ###", "1": ".#. ##. .#. .#. ###", "2": "##. ..# .#. #.. ###",
    "3": "##. ..# .#. ..# ##.", "4": "#.# #.# ### ..# ..#", "5": "### #.. ##. ..# ##.",
    "6": ".## #.. ### #.# ###", "7": "### ..# .#. .#. .#.", "8": "### #.# ### #.# ###",
    "9": "### #.# ### ..# ##.", ":": "... .#. ... .#. ...", ";": "... .#. ... .#. #..",
    "<": "..# .#. #.. .#. ..#", "=": "... ### ... ### ...", ">": "#.. .#. ..# .#. #..",
    "?": "##. ..# .#. ... .#.", "@": ".#. #.# ### #.. .##",
    "A": ".#. #.# ### #.# #.#", "B": "##. #.# ##. #.# ##.", "C": ".## #.. #.. #.. .##",
    "D": "##. #.# #.# #.# ##.", "E": "### #.. ### #.. ###", "F": "### #.. ### #.. #..",
    "G": ".## #.. #.# #.# .##", "H": "#.# #.# ### #.# #.#", "I": "### .#. .#. .#. ###",
    "J": "..# ..# ..# #.# .#.", "K": "#.# #.# ##. #.# #.#", "L": "#.. #.. #.. #.. ###",
    "M": "#.# ### ### #.# #.#", "N": "##. #.# #.# #.# #.#", "O": ".#. #.# #.# #.# .#.",
    "P": "##. #.# ##. #.. #..", "Q": ".#. #.# #.# ### .##", "R": "##. #.# ##. #.# #.#",
    "S": ".## #.. .#. ..# ##.", "T": "### .#. .#. .#. .#.", "U": "#.# #.# #.# #.# ###",
    "V": "#.# #.# #.# #.# .#.", "W": "#.# #.# ### ### #.#", "X": "#.# #.# .#. #.# #.#",
    "Y": "#.# #.# .#. .#. .#.", "Z": "### ..# .#. #.. ###",
    "[": "##. #.. #.. #.. ##.", "\\": "#.. #.. .#. ..# ..#", "]": ".## ..# ..# ..# .##",
    "^": ".#. #.# ... ... ...", "_": "... ... ... ... ###", "`": "#.. .#. ... ... ...",
    "a": "... ##. .## #.# ###", "b": "#.. #.. ##. #.# ##.", "c": "... ... .## #.. .##",
    "d": "..# ..# .## #.# .##", "e": "... .## ### #.. .##", "f": "..# .#. ### .#. .#.",
    "g": "... .## #.# .## ..# ##.", "h": "#.. #.. ##. #.# #.#", "i": ".#. ... .#. .#. .#.",
    "j": "..# ... ..# ..# #.# .#.", "k": "#.. #.# ##. ##. #.#", "l": "##. .#. .#. .#. ###",
    "m": "... ... ### ### #.#", "n": "... ... ##. #.# #.#", "o": "... ... ### #.# ###",
    "p": "... ... ##. #.# ##. #..", "q": "... ... .## #.# .## ..#", "r": "... ... .## #.. #..",
    "s": "... ... .## .#. ##.", "t": ".#. .#. ### .#. .##", "u": "... ... #.# #.# .##",
    "v": "... ... #.# #.# .#.", "w": "... ... #.# ### ###", "x": "... ... #.# .#. #.#",
    "y": "... ... #.# #.# .## ##.", "z": "... ... ##. .#. .##",
    "{": ".## .#. ##. .#. .##", "|": ".#. .#. .#. .#. .#.", "}": "##. .#. .## .#. ##.",
    "~": "... .## ##. ... ...",
    # symbols the effects use that have no ASCII base letter
    "·": "... ... .#. ... ...", "•": "... .## .## ... ...", "°": ".#. #.# .#. ... ...",
    "¤": "... #.# .#. #.# ...", "¦": ".#. .#. ... .#. .#.", "×": "... #.# .#. #.# ...",
    "÷": ".#. ... ### ... .#.", "±": ".#. ### .#. ### ...", "¬": "... ### ..# ... ...",
    "«": "... .## #.. .## ...", "»": "... ##. ..# ##. ...", "¿": ".#. ... .#. #.. .##",
    "¡": ".#. ... .#. .#. .#.", "ß": "##. #.# ##. #.# ##.", "Æ": ".## ##. ### #.. ###",
    "æ": "... ##. .## ##. .##", "Ð": "##. #.# ### #.# ##.", "ð": ".#. ..# .## #.# .#.",
    "Ø": ".## #.# ### #.# ##.", "ø": "... ..# .#. ### .#.", "Þ": "#.. ##. #.# ##. #..",
    "þ": "#.. #.. ##. #.# ##. #..", "µ": "... #.# #.# ### #..", "¶": ".## ### .## ..# ..#",
    "§": ".## #.. ### ..# ##.", "©": "### #.# #.# #.# ###", "®": "### ### ##. #.# ###",
    "¢": ".#. ### #.. ### .#.", "£": ".## .#. ### .#. ###", "¥": "#.# .#. ### .#. .#.",
    "●": ".#. ### ### ### .#.", "○": ".#. #.# #.# #.# .#.", "■": "... ### ### ### ...",
    "□": "... ### #.# ### ...", "◆": ".#. ### ### .#. ...", "◇": ".#. #.# #.# .#. ...",
    "▪": "... ... ##. ##. ...", "▫": "... ... ##. ##. ...", "◦": "... ... .#. #.# .#.",
    "∙": "... ... .#. ... ...", "…": "... ... ... ... #.#", "✦": ".#. .#. ### .#. .#.",
}

_SHADES = {"░": 0.35, "▒": 0.6, "▓": 0.85}


def _rows(pattern):
    """'.#. ###' -> row masks placed at x 0..2 (bit 3 = leftmost)."""
    return [sum(1 << (3 - i) for i, c in enumerate(row) if c == "#") for row in pattern.split()]


def _font_glyph(pattern):
    rows = _rows(pattern)
    out = [0] * CELL_H
    for y, mask in enumerate(rows[:6]):
        out[1 + y] = mask
    return tuple(out)


def _rect(x0, y0, x1, y1):
    """Filled rectangle, pixels x0..x1-1, y0..y1-1."""
    mask = sum(1 << (3 - x) for x in range(x0, x1))
    return tuple(mask if y0 <= y < y1 else 0 for y in range(CELL_H))


def _union(*glyphs):
    return tuple(functools.reduce(operator.or_, rows, 0) for rows in zip(*glyphs))


_QUAD = {"UL": _rect(0, 0, 2, 4), "UR": _rect(2, 0, 4, 4), "LL": _rect(0, 4, 2, 8), "LR": _rect(2, 4, 4, 8)}
_QUADRANTS = {"▖": ("LL",), "▗": ("LR",), "▘": ("UL",), "▝": ("UR",), "▙": ("UL", "LL", "LR"),
              "▚": ("UL", "LR"), "▛": ("UL", "UR", "LL"), "▜": ("UL", "UR", "LR"), "▞": ("UR", "LL"),
              "▟": ("UR", "LL", "LR")}


def _block(ch):
    """U+2580..U+259F block elements, exactly."""
    o = ord(ch)
    if ch == "▀":
        return _rect(0, 0, 4, 4)
    if ch == "▔":
        return _rect(0, 0, 4, 1)
    if 0x2581 <= o <= 0x2588:                       # lower n/8 blocks, ▄ and █ included
        n = o - 0x2580
        return _rect(0, CELL_H - n, 4, CELL_H)
    if 0x2589 <= o <= 0x258F:                       # left n/8 blocks
        n = 0x2590 - o
        return _rect(0, 0, max(1, n * CELL_W // 8), CELL_H)
    if ch == "▐":
        return _rect(2, 0, 4, CELL_H)
    if ch == "▕":
        return _rect(3, 0, 4, CELL_H)
    if ch in _SHADES:
        return _rect(0, 0, 4, CELL_H)
    if ch in _QUADRANTS:
        return _union(*(_QUAD[q] for q in _QUADRANTS[ch]))
    return None


def _box(ch):
    """U+2500..U+257F box drawing, from the character name: arms from the centre
    (up/down/left/right), 2 px thick when heavy or double, dashed or diagonal."""
    name = unicodedata.name(ch, "")
    if not name.startswith("BOX DRAWINGS "):
        return None
    spec = name[len("BOX DRAWINGS "):]
    if "DIAGONAL" in spec:
        a = tuple(1 << (3 - min(3, y // 2)) for y in range(CELL_H))     # upper left -> lower right
        b = tuple(1 << min(3, y // 2) for y in range(CELL_H))           # upper right -> lower left
        if "CROSS" in spec:
            return _union(a, b)
        return b if "UPPER RIGHT TO LOWER LEFT" in spec else a
    words = spec.split()
    arms = set()
    for w in words:
        if w == "HORIZONTAL":
            arms |= {"LEFT", "RIGHT"}
        elif w == "VERTICAL":
            arms |= {"UP", "DOWN"}
        elif w in ("UP", "DOWN", "LEFT", "RIGHT"):
            arms.add(w)
    thick = "HEAVY" in words or "DOUBLE" in words
    cx0, cx1 = (1, 3) if thick else (1, 2)
    cy0, cy1 = (3, 5) if thick else (3, 4)
    parts = []
    if "LEFT" in arms:
        parts.append(_rect(0, cy0, cx1, cy1))
    if "RIGHT" in arms:
        parts.append(_rect(cx0, cy0, CELL_W, cy1))
    if "UP" in arms:
        parts.append(_rect(cx0, 0, cx1, cy1))
    if "DOWN" in arms:
        parts.append(_rect(cx0, cy0, cx1, CELL_H))
    if not parts:
        return None
    glyph = _union(*parts)
    if "DASH" in spec:                                   # knock out every other pixel along the line
        if arms <= {"LEFT", "RIGHT"}:
            glyph = tuple(m & 0b1010 for m in glyph)
        else:
            glyph = tuple(m if y % 2 == 0 else 0 for y, m in enumerate(glyph))
    return glyph


def _noise(ch):
    """A stable 3x5 pattern for characters we have no drawing of."""
    h = hashlib.blake2s(ch.encode("utf-8", "surrogatepass"), digest_size=4).digest()
    bits = int.from_bytes(h, "little") | 0b010_000_010_000_010    # keep a spine so it reads as a glyph
    out = [0] * CELL_H
    for y in range(5):
        out[1 + y] = ((bits >> (3 * y)) & 0b111) << 1
    return tuple(out)


@functools.lru_cache(maxsize=4096)
def glyph(ch):
    """8 row masks for one terminal character."""
    if ch == " " or not ch:
        return BLANK
    if ch in _FONT:
        return _font_glyph(_FONT[ch])
    o = ord(ch)
    if 0x2580 <= o <= 0x259F:
        g = _block(ch)
        if g:
            return g
    if 0x2500 <= o <= 0x257F:
        g = _box(ch)
        if g:
            return g
    if 0x2800 <= o <= 0x28FF:                            # braille: 2x4 dots
        dots = o - 0x2800
        order = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3))
        out = [0] * CELL_H
        for bit, (x, y) in enumerate(order):
            if dots & (1 << bit):
                out[1 + 2 * y] |= 1 << (3 - (2 * x))
        return tuple(out)
    base = unicodedata.normalize("NFKD", ch)[:1]
    if base != ch and base in _FONT:
        return _font_glyph(_FONT[base])
    return _noise(ch)


def brightness(ch):
    """Colour scale for the character (shades are drawn as dimmer solid cells)."""
    return _SHADES.get(ch, 1.0)


# ---- big cells: real letterforms ------------------------------------------------------
# At 3x and up the 3x5 font is too crude, so letters, digits and symbols are
# drawn from a monospace TrueType font instead, 1-bit (crisp, and runs of
# solid colour compress far better than antialiased edges). Block elements,
# box drawing and braille stay geometric; characters the font lacks keep
# their pixel pattern.
FONT_QUERIES = ("JetBrainsMono Nerd Font:style=Bold", "JetBrains Mono:style=Bold",
                "DejaVu Sans Mono:style=Bold", "monospace:style=Bold")
_GEOMETRIC = ((0x2500, 0x259F), (0x2800, 0x28FF))


def monospace_fonts():
    """Installed monospace font families (for the web UI's font picker)."""
    import shutil
    import subprocess
    tool = shutil.which("fc-list")
    if not tool:
        return []
    try:
        out = subprocess.run([tool, ":spacing=100", "family"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = {line.split(",")[0].strip() for line in out.splitlines() if line.strip()}
    return sorted(n for n in names if n and not any(w in n.lower() for w in ("emoji", "signwriting", "omarchy")))


@functools.lru_cache(maxsize=16)
def _font_file(family=""):
    import shutil
    import subprocess
    tool = shutil.which("fc-match")
    queries = ((f"{family}:style=Bold", family) if family else ()) + FONT_QUERIES
    for query in queries if tool else ():
        try:
            path = subprocess.run([tool, "-f", "%{file}", query], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if path.lower().endswith((".ttf", ".otf")):
            return path
    return None


@functools.lru_cache(maxsize=16)
def _font(width, height, family=""):
    """The font at the biggest size whose monospace cell fits width x height."""
    from PIL import ImageFont
    path = _font_file(family)
    if not path:
        return None
    size = max(4, int(height * 0.9))
    while size > 4:
        font = ImageFont.truetype(path, size)
        ascent, descent = font.getmetrics()
        if font.getlength("M") <= width and ascent + descent <= height * 1.05:
            return font
        size -= 1
    return ImageFont.truetype(path, size)


def _scaled(rows4, width, height):
    """Nearest-neighbour scale of a 4x8 glyph to width x height masks."""
    out = []
    for y in range(height):
        mask = rows4[min(CELL_H - 1, y * CELL_H // height)]
        row = 0
        for x in range(width):
            row = (row << 1) | (mask >> (CELL_W - 1 - min(CELL_W - 1, x * CELL_W // width)) & 1)
        out.append(row)
    return tuple(out)


@functools.lru_cache(maxsize=8192)
def bitmap(ch, width, height, family=""):
    """Row masks (width bits, most significant = leftmost) of `ch` drawn in a
    width x height cell, from `family` (or the system monospace font) when
    the cell is big enough for real letterforms."""
    if ch == " " or not ch:
        return (0,) * height
    code = ord(ch[0])
    geometric = any(lo <= code <= hi for lo, hi in _GEOMETRIC)
    font = None if geometric or width < 3 * CELL_W else _font(width, height, family)
    if font is not None:
        rows = _rasterize(font, ch, width, height)
        if rows is not None:
            return rows
    return _scaled(glyph(ch), width, height)


def _rasterize(font, ch, width, height):
    from PIL import Image, ImageDraw
    ascent, descent = font.getmetrics()

    def draw(c):
        im = Image.new("1", (width, height), 0)
        d = ImageDraw.Draw(im)
        d.fontmode = "1"
        d.text(((width - font.getlength(c)) / 2, (height - ascent - descent) / 2), c, font=font, fill=1)
        return im.tobytes()

    pixels = draw(ch)
    if pixels == draw("￾"):         # the font has no such glyph (its .notdef box)
        return None
    stride = (width + 7) // 8
    rows = []
    for y in range(height):
        row = int.from_bytes(pixels[y * stride:(y + 1) * stride], "big") >> (stride * 8 - width)
        rows.append(row)
    return tuple(rows)
