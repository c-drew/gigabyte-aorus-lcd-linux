"""Build an Upload from a ContentConfig. Add a content type by adding a builder."""
import hashlib
from dataclasses import replace

from . import protocol as P
from . import render as R


def _logo(c):
    return R.logo(c.source, c.color, c.scale, c.background, c.anchor, tuple(c.offset))


def still(im):
    """Stills go out as a one-frame GIF (two frames on the panel: gif_upload
    adds frame 0, which is never shown). GIF mode is the one path that draws
    reliably on this firmware (RLE, 4 KB sector erases); Gigabyte's raw
    image/text framebuffer uploads can complete without drawing anything. A
    GIF of one frame in all leaves the panel on a garbled "Loading" screen
    (tested on LCD firmware 1.3)."""
    return P.gif_upload([R.to_rgb565(im)], 255)


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


def ttfx_source(c):
    return c.ttfx_source or ("text" if c.ttfx_text.strip() else "logo")


def _ttfx_text(c):
    """What the screensaver animates. Only its own fields count: another content
    type's text or file lingers in the settings when switching types."""
    from . import ttfx, wordmark
    source = ttfx_source(c)
    if source == "mark":
        rows = min(20, (P.H - c.border[1] - c.border[3]) // 8 - 1)
        return wordmark.mark(rows)
    if source == "wordmark":
        return wordmark.wordmark(c.ttfx_text if c.ttfx_text.strip() else "omarchy")
    if source == "text" and c.ttfx_text.strip():
        return c.ttfx_text
    with open(ttfx.DEFAULT_TEXT, encoding="utf-8") as f:
        return f.read()


def loop_key(c):
    """What the look of each screensaver loop depends on, hashed."""
    from . import screensaver as S
    from . import ttfx
    binary = ttfx.find_binary(c.ttfx)
    parts = [S.RENDERER_VERSION, ttfx.version(binary), _ttfx_text(c), c.fps, c.hold_ms, c.seed,
             list(c.border), bool(c.rotate_minutes), c.text_scale, c.ttfx_font]
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def ttfx_key(c):
    """Everything the rendered screensaver depends on, hashed."""
    from . import screensaver as S
    parts = [loop_key(c), list(c.effects or S.DEFAULT_EFFECTS), c.shuffle, c.rotate_minutes]
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def playlist_order(c):
    from . import playlist, screensaver as S
    return playlist.effects_in_order(c, S.DEFAULT_EFFECTS)


_last_animation = {}


def animation(c, index=0):
    """The rendered screensaver for a ttfx config (the last one is kept): with
    rotation, the playlist loop at `index`; without, one loop of all effects."""
    from . import screensaver as S
    key = (ttfx_key(c), index if c.rotate_minutes else 0)
    if key not in _last_animation:
        inset = tuple(c.border)
        if c.rotate_minutes:
            order = playlist_order(c)
            # At most GIF_MAX_PAYLOAD, like any upload (the erase wait must end
            # before the panel decides the PC has gone).
            anim = S.build_loop(_ttfx_text(c), order[index % len(order)], c.seed + index, c.hold_ms,
                                inset, c.ttfx, fps=c.fps, budget=P.GIF_MAX_PAYLOAD,
                                scale=c.text_scale, font=c.ttfx_font)
        else:
            # As many of the effects as the panel takes (GIF_MAX_PLAYED, GIF_MAX_PAYLOAD).
            anim = S.build(_ttfx_text(c), tuple(c.effects) or S.DEFAULT_EFFECTS, c.fps, c.seed, c.hold_ms,
                           P.GIF_MAX_PAYLOAD, c.ttfx, c.shuffle, delay_ms=S.frame_delay(c.fps),
                           inset=inset, scale=c.text_scale, font=c.ttfx_font)
        _last_animation.clear()
        _last_animation[key] = anim
    return _last_animation[key]


def build_ttfx(c):
    return animation(c).upload()


BUILDERS = {"logo": build_logo, "image": build_image, "gif": build_gif, "text": build_text,
            "ttfx": build_ttfx}


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
    if c.type == "ttfx":
        return animation(c).images()[-1]
    raise ValueError(f"nothing to preview for content type {c.type!r}")


def preview_frames(c):
    """([PIL frames], frame ms) exactly as they will be sent."""
    if c.type == "logo" and c.animate == "pulse":
        return R.pulse(_logo(c), c.frames), c.frame_ms
    if c.type == "gif":
        return R.gif_frames(c.source, c.fit, c.background)
    if c.type == "ttfx":
        anim = animation(c)
        step = max(1, (len(anim.blobs) - 1) // 400 + 1)       # keep previews light
        return anim.images(step), anim.delay_ms * step
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


def quick_fingerprint(c):
    """(fingerprint, mode) without building, for content that is slow to build;
    None when the upload itself has to be fingerprinted."""
    if c.type == "ttfx" and c.rotate_minutes:
        # A playlist: which effects rotate, and how often, is not on the panel;
        # changing that takes effect at the next swap, without an upload.
        return "ttfx-loop:" + loop_key(c), P.MODE_GIF
    if c.type == "ttfx":
        return "ttfx:" + ttfx_key(c), P.MODE_GIF
    return None
