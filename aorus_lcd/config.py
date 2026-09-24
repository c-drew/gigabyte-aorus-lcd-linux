"""TOML configuration for the daemon (and `aorus-lcd apply`)."""
from dataclasses import dataclass, field, fields
import tomllib

from .protocol import WIDGETS

DEFAULT_PATH = "/etc/aorus-lcd/config.toml"


@dataclass
class PanelConfig:
    transport: str = "nvrm"          # nvrm | i2c-dev
    speed_khz: int = 400
    gpu: str = ""                    # PCI address; empty = the only Gigabyte NVIDIA card
    chunk_delay: float = 0.01        # seconds between 256-byte upload chunks


@dataclass
class ContentConfig:
    type: str = "none"               # logo | image | gif | text | builtin | none
    source: str = ""                 # file for logo/image/gif
    text: str = ""
    color: str = "#ff0000"           # logo/text colour
    background: str = "#000000"
    scale: float = 0.8               # logo: fraction of the panel it may fill
    anchor: str = "center"           # center, left, right, top, bottom, top-left, ...
    offset: list = field(default_factory=lambda: [0, 0])
    fit: str = "contain"             # image/gif: contain | cover
    size: int = 32                   # text: font size
    font: str = ""                   # text: font file/name
    animate: str = "none"            # logo: none | pulse (uploads as a GIF)
    frames: int = 10                 # pulse: frame count
    frame_ms: int = 80               # pulse: delay per frame
    builtin_mode: int = 0            # builtin: 0-2 stat screens, 6 chibi clock


@dataclass
class OverlayConfig:
    enabled: bool = False
    widgets: list = field(default_factory=lambda: ["temp", "usage", "power"])
    rotate_seconds: int = 3          # firmware rotates through the widgets
    color: str = "#ff0000"
    image_position: list = field(default_factory=lambda: [0, 0])
    data_position: list = field(default_factory=lambda: [146, 64])   # widget block is ~100 px tall
    update_seconds: float = 1.0      # how often to sample the GPU
    refresh_seconds: float = 30.0    # resend even when nothing changed
    min_change: dict = field(default_factory=lambda: {
        "temp": 1, "clock": 15, "usage": 2, "fan": 50, "vram_clock": 15, "vram": 1, "power": 3})
    clear_on_exit: bool = True       # hide the widgets when the daemon stops


@dataclass
class Config:
    panel: PanelConfig = field(default_factory=PanelConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)


def _build(cls, data, section):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    return cls(**data)


def parse(data):
    unknown = set(data) - {"panel", "content", "overlay"}
    if unknown:
        raise ValueError(f"unknown sections: {', '.join(sorted(unknown))}")
    cfg = Config(_build(PanelConfig, data.get("panel", {}), "panel"),
                 _build(ContentConfig, data.get("content", {}), "content"),
                 _build(OverlayConfig, data.get("overlay", {}), "overlay"))
    validate(cfg)
    return cfg


def load(path=DEFAULT_PATH):
    with open(path, "rb") as f:
        return parse(tomllib.load(f))


def validate(cfg):
    if cfg.panel.transport not in ("nvrm", "i2c-dev"):
        raise ValueError("panel.transport must be nvrm or i2c-dev")
    if cfg.content.type not in ("logo", "image", "gif", "text", "builtin", "none"):
        raise ValueError(f"content.type {cfg.content.type!r} is not supported")
    if cfg.content.type in ("logo", "image", "gif") and not cfg.content.source:
        raise ValueError(f"content.type = {cfg.content.type!r} needs content.source")
    if cfg.content.animate not in ("none", "pulse"):
        raise ValueError("content.animate must be none or pulse")
    bad = set(cfg.overlay.widgets) - set(WIDGETS)
    if bad:
        raise ValueError(f"overlay.widgets: unknown {sorted(bad)}; choose from {', '.join(WIDGETS)}")
    if cfg.overlay.update_seconds < 0.25:
        raise ValueError("overlay.update_seconds must be >= 0.25")
