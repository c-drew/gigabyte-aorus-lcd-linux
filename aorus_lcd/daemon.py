"""The boot service: put the configured content on the panel, then keep the
firmware's GPU-stats overlay fed.

Content is only re-uploaded when it changed (the panel keeps its image across
power cycles, and every upload rewrites its flash). Each overlay update is a
256-byte I2C write (~6 ms of driver time), so samples are only sent when a
value moves past `min_change`, plus a periodic refresh.
"""
import json
import logging
import os
import signal
import time
from dataclasses import asdict
from pathlib import Path

from . import config as C
from . import content
from . import protocol as P
from .panel import Panel
from .render import parse_color
from .transport import TransportError, find_gpu, open_transport

log = logging.getLogger("aorus-lcd")


def state_dir():
    return Path(os.environ.get("STATE_DIRECTORY") or
                Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "aorus-lcd")


def _load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def apply_content(panel, cfg, state_file=None, force=False, progress=None):
    """Show the configured content. Returns True if something was uploaded."""
    c = cfg.content
    if c.type == "none":
        return False
    if c.type == "builtin":
        panel.set_mode(c.builtin_mode)
        return False
    upload = content.build(c)
    digest = content.fingerprint(upload)
    state = _load_state(state_file) if state_file else {}
    mode, _ = P.parse_mode(panel.query(P.OP_GET_MODE))
    if not force and state.get("content") == digest and mode == upload.mode:
        log.info("panel already shows this content; not re-uploading")
        return False
    panel.carousel([])
    start = time.monotonic()
    panel.upload(upload, cfg.panel.chunk_delay, progress=progress)
    log.info("uploaded %s (%d bytes, %d frames) in %.1fs", c.type, len(upload.payload),
             len(upload.frames), time.monotonic() - start)
    if state_file:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({**state, "content": digest}))
    return True


def apply_overlay(panel, cfg):
    o = cfg.overlay
    if not o.enabled or not o.widgets:
        panel.overlay([])
        return
    # The style is stored per display mode; set image and GIF mode alike.
    for kind in (P.TEMPLATE_IMAGE, P.TEMPLATE_GIF):
        panel.template(parse_color(o.color), tuple(o.image_position), tuple(o.data_position), True, kind)
    panel.overlay(o.widgets, o.rotate_seconds)


class Feeder:
    """Decides when a new sample is worth a bus write."""

    def __init__(self, min_change, refresh_seconds, clock=time.monotonic):
        self.min_change, self.refresh, self.clock = min_change, refresh_seconds, clock
        self.last, self.sent_at = None, 0.0

    def should_send(self, sample):
        if self.last is None or self.clock() - self.sent_at >= self.refresh:
            return True
        prev, cur = asdict(self.last), asdict(sample)
        return any(abs(cur[k] - prev[k]) >= self.min_change.get(k, 1) for k in cur)

    def sent(self, sample):
        self.last, self.sent_at = sample, self.clock()


def run(config_path=C.DEFAULT_PATH, force=False):
    cfg = C.load(config_path)
    stop = {"now": False, "reload": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGHUP, lambda *_: stop.update(reload=True))

    gpu = find_gpu(cfg.panel.gpu or None)
    log.info("GPU: %s", gpu.describe())
    sensors = None

    while not stop["now"]:
        if cfg.overlay.enabled and sensors is None:
            from .sensors import NvmlSensors
            sensors = NvmlSensors(gpu.pci)
        try:
            with open_transport(cfg.panel.transport, gpu, cfg.panel.speed_khz) as t:
                panel = Panel(t)
                log.info("panel answers: %s", panel.wait_ready().hex(" "))
                panel.power(True)
                apply_content(panel, cfg, state_dir() / "state.json", force)
                force = False
                apply_overlay(panel, cfg)
                feeder = Feeder(cfg.overlay.min_change, cfg.overlay.refresh_seconds)
                reasserted = time.monotonic()
                while not stop["now"] and not stop["reload"]:
                    if sensors and cfg.overlay.enabled:
                        if time.monotonic() - reasserted >= cfg.overlay.refresh_seconds:
                            apply_overlay(panel, cfg)        # cheap insurance against resets
                            reasserted = time.monotonic()
                        sample = sensors.read()
                        if feeder.should_send(sample):
                            panel.feed(sample)
                            feeder.sent(sample)
                    time.sleep(cfg.overlay.update_seconds if cfg.overlay.enabled else 1.0)
                if stop["reload"]:
                    stop["reload"] = False
                    cfg = C.load(config_path)
                    log.info("config reloaded")
                    continue
                if cfg.overlay.enabled and cfg.overlay.clear_on_exit:
                    panel.overlay([])
        except TransportError as e:
            log.warning("bus error: %s; retrying in 5s", e)
            for _ in range(50):
                if stop["now"]:
                    break
                time.sleep(0.1)
    if sensors:
        sensors.close()
    log.info("stopped")
