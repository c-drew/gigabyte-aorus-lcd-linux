"""Build an Upload from a ContentConfig. Add a content type by adding a builder."""
import hashlib
from dataclasses import replace

from . import protocol as P
from . import render as R


def _logo(c):
    return R.logo(c.source, c.color, c.scale, c.background, c.anchor, tuple(c.offset))


def still(im):
    """Stills go out as a single-frame GIF. GIF mode is the one path that draws
    reliably on this firmware (RLE, 4 KB sector erases); Gigabyte's raw
    image/text framebuffer uploads can complete without drawing anything."""
    return P.gif_upload([R.to_rgb565(im)], 100)


def build_logo(c):
    im = _logo(c)
    if c.animate == "pulse":
        frames = [R.to_rgb565(f) for f in R.pulse(im, c.frames)]
        return P.gif_upload(frames, c.frame_ms)
    return still(im)


def build_image(c):
    return still(R.image(c.source, c.fit, c.background))


def build_gif(c):
    frames, delay = R.gif_frames(c.source, c.fit, c.background)
    return P.gif_upload([R.to_rgb565(f) for f in frames], delay)


def build_text(c):
    im = R.text(c.text, c.size, c.color, c.background, c.font or None)
    if c.effect == "wave":
        # The panel's text mode animates text as a rainbow wave (brightness is the
        # mask). Uploaded exactly like Gigabyte's software: raw, 64 KB erases.
        return replace(P.still_upload(R.to_rgb565(im), "text"), erase_mode=P.ERASE_BLOCK)
    return still(im)


BUILDERS = {"logo": build_logo, "image": build_image, "gif": build_gif, "text": build_text}


def build(content_cfg):
    """Upload for the configured content, or None for builtin/none."""
    builder = BUILDERS.get(content_cfg.type)
    return builder(content_cfg) if builder else None


def preview(c):
    """What the content looks like (first frame), as a PIL image."""
    if c.type == "logo":
        return _logo(c)
    if c.type == "image":
        return R.image(c.source, c.fit, c.background)
    if c.type == "gif":
        return R.gif_frames(c.source, c.fit, c.background)[0][0]
    if c.type == "text":
        return R.text(c.text, c.size, c.color, c.background, c.font or None)
    raise ValueError(f"nothing to preview for content type {c.type!r}")


def preview_frames(c):
    """([PIL frames], frame ms) exactly as they will be sent."""
    if c.type == "logo" and c.animate == "pulse":
        return R.pulse(_logo(c), c.frames), c.frame_ms
    if c.type == "gif":
        return R.gif_frames(c.source, c.fit, c.background)
    return [preview(c)], 0


def preview_bytes(c):
    """(bytes, mime type): a PNG, or an animated GIF for animated content. Frames
    go through RGB565 so the preview shows the panel's colour depth."""
    import io
    frames, ms = preview_frames(c)
    frames = [R.from_rgb565(R.to_rgb565(f)) for f in frames]
    buf = io.BytesIO()
    if len(frames) > 1:
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=ms, loop=0)
        return buf.getvalue(), "image/gif"
    frames[0].save(buf, "PNG")
    return buf.getvalue(), "image/png"


def fingerprint(upload):
    """Identifies what is on the panel, so unchanged content is not re-uploaded."""
    return hashlib.sha256(bytes([upload.mode]) + upload.payload).hexdigest()
