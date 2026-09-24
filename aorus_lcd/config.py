"""TOML configuration for the daemon (and `aorus-lcd apply`)."""
from dataclasses import asdict, dataclass, field, fields
import hashlib
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
    effect: str = "none"             # text: "wave" = the panel's own rainbow wave text mode
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
class LightingConfig:
    enabled: bool = False            # leave the GPU's RGB alone unless asked (e.g. OpenRGB users)
    mode: str = "static"             # static, breathing, flashing, color-cycle, wave, color-shift, ... off
    colors: list = field(default_factory=lambda: ["#ff0000"])
    brightness: int = 5              # 1..10
    speed: int = 3                   # 1..6 for animated modes
    save_to_card: bool = True        # persist in the card, so it survives reboots without this tool


@dataclass
class WebConfig:
    enabled: bool = True             # browser UI served by the daemon
    bind: str = "127.0.0.1"          # keep it local: anything that can reach it can drive the panel
    port: int = 5090
    max_upload_mb: int = 32


SECTIONS = {"panel": PanelConfig, "content": ContentConfig, "overlay": OverlayConfig,
            "lighting": LightingConfig, "web": WebConfig}
OVERRIDABLE = ("content", "overlay", "lighting")


@dataclass
class Config:
    panel: PanelConfig = field(default_factory=PanelConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    lighting: LightingConfig = field(default_factory=LightingConfig)
    web: WebConfig = field(default_factory=WebConfig)


def _build(cls, data, section):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    return cls(**data)


def parse(data):
    unknown = set(data) - set(SECTIONS)
    if unknown:
        raise ValueError(f"unknown sections: {', '.join(sorted(unknown))}")
    cfg = Config(**{name: _build(cls, data.get(name, {}), name) for name, cls in SECTIONS.items()})
    validate(cfg)
    return cfg


def with_overrides(cfg, overrides):
    """A copy of `cfg` with sections partially replaced (e.g. choices made in the
    web UI). Validated like a config file."""
    data = asdict(cfg)
    for section, values in (overrides or {}).items():
        if section not in OVERRIDABLE:
            raise ValueError(f"cannot override [{section}]")
        data[section] = {**data[section], **values}
    return parse(data)


def file_digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


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
    if cfg.content.effect not in ("none", "wave"):
        raise ValueError("content.effect must be none or wave")
    if cfg.content.animate not in ("none", "pulse"):
        raise ValueError("content.animate must be none or pulse")
    bad = set(cfg.overlay.widgets) - set(WIDGETS)
    if bad:
        raise ValueError(f"overlay.widgets: unknown {sorted(bad)}; choose from {', '.join(WIDGETS)}")
    if cfg.overlay.update_seconds < 0.25:
        raise ValueError("overlay.update_seconds must be >= 0.25")
    if cfg.content.type == "builtin" and cfg.content.builtin_mode not in (0, 1, 2, 6):
        raise ValueError("content.builtin_mode must be 0, 1, 2 (stat screens) or 6 (chibi clock)")
    if not 0 < cfg.content.scale <= 1:
        raise ValueError("content.scale must be in (0, 1]")
    if len(cfg.content.text) > 200:
        raise ValueError("content.text is limited to 200 characters")
    from .lighting import MODES
    from .render import parse_color
    if cfg.lighting.mode not in MODES:
        raise ValueError(f"lighting.mode must be one of {', '.join(MODES)}")
    if not 1 <= len(cfg.lighting.colors) <= 17:
        raise ValueError("lighting.colors needs 1..17 colours")
    for c in (*cfg.lighting.colors, cfg.content.color, cfg.content.background, cfg.overlay.color):
        parse_color(c)
    if not 1 <= cfg.lighting.brightness <= 10 or not 1 <= cfg.lighting.speed <= 6:
        raise ValueError("lighting.brightness is 1..10 and lighting.speed 1..6")
    if not 1 <= cfg.web.port <= 65535:
        raise ValueError("web.port must be 1..65535")
