"""Build an Upload from a ContentConfig. Add a content type by adding a builder."""
import hashlib

from . import protocol as P
from . import render as R


def _logo(c):
    return R.logo(c.source, c.color, c.scale, c.background, c.anchor, tuple(c.offset))


def build_logo(c):
    im = _logo(c)
    if c.animate == "pulse":
        frames = [R.to_rgb565(f) for f in R.pulse(im, c.frames)]
        return P.gif_upload(frames, c.frame_ms)
    return P.still_upload(R.to_rgb565(im), "image")


def build_image(c):
    return P.still_upload(R.to_rgb565(R.image(c.source, c.fit, c.background)), "image")


def build_gif(c):
    frames, delay = R.gif_frames(c.source, c.fit, c.background)
    return P.gif_upload([R.to_rgb565(f) for f in frames], delay)


def build_text(c):
    im = R.text(c.text, c.size, c.color, c.background, c.font or None)
    return P.still_upload(R.to_rgb565(im), "text")


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


def fingerprint(upload):
    """Identifies what is on the panel, so unchanged content is not re-uploaded."""
    return hashlib.sha256(bytes([upload.mode]) + upload.payload).hexdigest()
