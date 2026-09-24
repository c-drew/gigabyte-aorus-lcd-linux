"""Turn images, logos, text and GIFs into 320x170 LE-RGB565 frames (Pillow)."""
import shutil
import subprocess
import tempfile
from pathlib import Path

from .protocol import FRAME_BYTES, H, W

FONT_CANDIDATES = ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf")


def _pil():
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("rendering needs Pillow (pip install Pillow)") from e
    return Image


def parse_color(value):
    """'#ff0000', 'ff0000', 'f00' or (r, g, b) -> (r, g, b)."""
    if isinstance(value, (tuple, list)):
        return tuple(int(v) for v in value)
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"bad colour {value!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def to_rgb565(im):
    """PIL image (any mode, already 320x170) -> little-endian RGB565 bytes."""
    rgb = im.convert("RGB").tobytes()
    out = bytearray(FRAME_BYTES)
    out[0::2] = bytes(((g & 0x1C) << 3) | (b >> 3) for g, b in zip(rgb[1::3], rgb[2::3]))
    out[1::2] = bytes((r & 0xF8) | (g >> 5) for r, g in zip(rgb[0::3], rgb[1::3]))
    return bytes(out)


def from_rgb565(px):
    """LE-RGB565 bytes -> PIL RGB image (for previews)."""
    Image = _pil()
    rgb = bytearray()
    for i in range(0, len(px), 2):
        v = px[i] | (px[i + 1] << 8)
        rgb += bytes(((v >> 8) & 0xF8, (v >> 3) & 0xFC, (v << 3) & 0xF8))
    return Image.frombytes("RGB", (W, H), bytes(rgb))


def load(path, size_hint=None):
    """Open any Pillow image, or an SVG via rsvg-convert, as RGBA."""
    Image = _pil()
    path = Path(path)
    if path.suffix.lower() == ".svg":
        tool = shutil.which("rsvg-convert")
        if not tool:
            raise RuntimeError("SVG logos need rsvg-convert (librsvg); or use a PNG")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "logo.png"
            args = [tool, "-o", str(out)]
            if size_hint:
                args += ["-w", str(size_hint[0] * 4)]      # render large, downscale cleanly
            subprocess.run(args + [str(path)], check=True)
            return Image.open(out).convert("RGBA")
    return Image.open(path).convert("RGBA")


def fit(im, box=(W, H), mode="contain"):
    """Resize to fit inside (contain) or fill (cover) `box`, keeping aspect."""
    Image = _pil()
    scale = (min if mode == "contain" else max)(box[0] / im.width, box[1] / im.height)
    im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    if mode == "cover":
        left, top = (im.width - box[0]) // 2, (im.height - box[1]) // 2
        im = im.crop((left, top, left + box[0], top + box[1]))
    return im


def canvas(background=(0, 0, 0)):
    return _pil().new("RGB", (W, H), parse_color(background))


def place(base, im, anchor="center", offset=(0, 0)):
    """Paste `im` (RGBA honoured) onto `base` at an anchor + offset."""
    x = {"left": 0, "center": (W - im.width) // 2, "right": W - im.width}
    y = {"top": 0, "center": (H - im.height) // 2, "bottom": H - im.height}
    horiz = next((k for k in ("left", "right") if k in anchor), "center")
    vert = next((k for k in ("top", "bottom") if k in anchor), "center")
    pos = (x[horiz] + offset[0], y[vert] + offset[1])
    base.paste(im, pos, im if im.mode == "RGBA" else None)
    return base


def recolor(im, color):
    """Paint every visible pixel of a logo in `color`, keeping its shape.
    Coverage comes from alpha when the image has transparency, otherwise from
    luminance against black (dark = empty); an inverted logo is detected."""
    Image = _pil()
    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] == 255:                 # opaque art: coverage from brightness
        lum = im.convert("L")
        w, h = lum.size
        corners = [lum.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
        alpha = lum if sum(corners) / 4 < 128 else lum.point(lambda v: 255 - v)
    solid = Image.new("RGBA", im.size, parse_color(color) + (255,))
    solid.putalpha(alpha)
    return solid


def logo(path, color="#ff0000", scale=0.8, background="#000000", anchor="center", offset=(0, 0)):
    """A logo recoloured to `color`, fitted to `scale` of the panel, on `background`."""
    box = (int(W * scale), int(H * scale))
    art = recolor(fit(load(path, box), box), color)
    return place(canvas(background), art, anchor, offset)


def text(message, size=32, color="#ffffff", background="#000000", font=None):
    Image = _pil()
    from PIL import ImageDraw, ImageFont
    im = canvas(background)
    draw = ImageDraw.Draw(im)
    face = None
    for name in ((font,) if font else ()) + FONT_CANDIDATES:
        try:
            face = ImageFont.truetype(name, size)
            break
        except OSError:
            continue
    face = face or ImageFont.load_default()
    box = draw.multiline_textbbox((0, 0), message, font=face, align="center")
    pos = ((W - (box[2] - box[0])) / 2 - box[0], (H - (box[3] - box[1])) / 2 - box[1])
    draw.multiline_text(pos, message, font=face, fill=parse_color(color), align="center")
    return im


def image(path, mode="contain", background="#000000"):
    return place(canvas(background), fit(load(path), mode=mode))


def gif_frames(path, mode="contain", background="#000000"):
    """Decode an animated GIF -> ([PIL frames], average delay in ms)."""
    Image = _pil()
    frames, delays = [], []
    with Image.open(path) as im:
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            frames.append(place(canvas(background), fit(im.convert("RGBA"), mode=mode)))
            delays.append(im.info.get("duration", 100) or 100)
    return frames, round(sum(delays) / len(delays))


def pulse(im, frames=10, low=0.35):
    """Frames of `im` fading between `low` and full brightness (a breathing glow)."""
    import math
    Image = _pil()
    black = Image.new("RGB", im.size)
    out = []
    for i in range(frames):
        level = low + (1 - low) * (0.5 - 0.5 * math.cos(2 * math.pi * i / frames))
        out.append(Image.blend(black, im.convert("RGB"), level))
    return out
