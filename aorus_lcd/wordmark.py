"""Text in the style of the Omarchy logo, and the Omarchy mark, as block art.

The Omarchy logo that the screensaver animates (/usr/share/omarchy/logo.txt)
is drawn in half-block characters. Omarchy Font (github.com/markcuda/Omarchy-Font,
MIT, bundled in fonts/) is that wordmark as a typeface, built on the same grid:
a cell is 50 font units wide and 100 tall, and corners are half cells. Drawn
at 20 px per em, one pixel is one half cell, so pairs of pixel rows become
exactly the characters of logo.txt; "OMARCHY" reproduces it.

The Omarchy mark (the logo symbol) is a ligature of the word "omarchy" in
Omarchy's own icon font, omarchy.ttf.
"""
import functools
import shutil
import subprocess
from importlib import resources

WORDMARK_FONT = "fonts/OmarchyFont.ttf"
MARK_FONT_QUERY = "omarchy"


def _half_blocks(image):
    """A 1-bit PIL image -> rows of block characters, two pixel rows each."""
    width, height = image.size
    px = image.load()
    rows = []
    for y in range(0, height - 1, 2):
        rows.append("".join(
            "█" if px[x, y] and px[x, y + 1] else "▀" if px[x, y] else "▄" if px[x, y + 1] else " "
            for x in range(width)).rstrip())
    return rows


def _tidy(rows):
    while rows and not rows[0].strip():
        rows.pop(0)
    while rows and not rows[-1].strip():
        rows.pop()
    indent = min((len(r) - len(r.lstrip()) for r in rows if r.strip()), default=0)
    return "\n".join(r[indent:] for r in rows) + "\n"


def wordmark(text):
    """`text` in the Omarchy logo's block letters (lines stay lines; unicase)."""
    from PIL import Image, ImageDraw, ImageFont
    with resources.as_file(resources.files("aorus_lcd").joinpath(WORDMARK_FONT)) as path:
        font = ImageFont.truetype(str(path), 20)
    rows = []
    for line in text.upper().split("\n"):
        if not line.strip():
            rows += [""] * 5                          # a blank line: half the letter height
            continue
        # Some letters (B, E, H, ...) reach a pixel left of their origin.
        image = Image.new("1", (int(font.getlength(line)) + 4, 20), 0)
        draw = ImageDraw.Draw(image)
        draw.fontmode = "1"
        draw.text((2, 0), line, font=font, fill=1)
        rows += _half_blocks(image)
    return _tidy(rows)


@functools.lru_cache(maxsize=1)
def _mark_font():
    tool = shutil.which("fc-match")
    if tool:
        try:
            path = subprocess.run([tool, "-f", "%{file}", MARK_FONT_QUERY], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
            if path.endswith("omarchy.ttf"):
                return path
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def mark(rows=20):
    """The Omarchy mark, `rows` characters tall (and twice as many wide: square)."""
    from PIL import Image, ImageDraw, ImageFont
    path = _mark_font()
    if not path:
        raise ValueError("the Omarchy icon font (omarchy.ttf) is not installed")
    size = rows * 2
    font = ImageFont.truetype(path, size)
    image = Image.new("1", (size, size), 0)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    draw.text((0, 0), "omarchy", font=font, fill=1)     # the ligature: the logo symbol
    box = image.getbbox()
    if box is None:
        raise ValueError("omarchy.ttf did not draw the Omarchy mark")
    return _tidy(_half_blocks(image.crop(box).resize((2 * rows, 2 * rows), Image.NEAREST)))
