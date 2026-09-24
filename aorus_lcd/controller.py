"""The service's single owner of the panel, shared by the stats loop and the web UI.

Effective settings = config.toml + overrides chosen in the UI (saved in the
state directory). If config.toml changes, it wins and the overrides are
dropped. Presets are named snapshots of content + overlay + lighting.
"""
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import config as C
from . import content as CT
from . import protocol as P
from .lighting import GpuRgb, Lighting
from .render import parse_color
from .transport import TransportError

log = logging.getLogger("aorus-lcd")
MEDIA_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


class Busy(RuntimeError):
    pass


def _json_load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _json_save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def apply_overlay(panel, cfg):
    o = cfg.overlay
    if not o.enabled or not o.widgets:
        panel.overlay([])
        return
    # The style is stored per display mode; set image and GIF mode alike.
    for kind in (P.TEMPLATE_IMAGE, P.TEMPLATE_GIF):
        panel.template(parse_color(o.color), tuple(o.image_position), tuple(o.data_position), True, kind)
    panel.overlay(o.widgets, o.rotate_seconds)


def lighting_of(cfg):
    lc = cfg.lighting
    return Lighting(lc.mode, tuple(parse_color(c) for c in lc.colors), lc.brightness, lc.speed)


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


class Controller:
    def __init__(self, base_cfg, state_dir, config_digest=None, gpu=None, sensors_factory=None):
        self.base = base_cfg
        self.state_dir = Path(state_dir)
        self.media_dir = self.state_dir / "media"
        self.state_file = self.state_dir / "state.json"
        self.presets_file = self.state_dir / "presets.json"
        self.gpu = gpu
        self.sensors_factory = sensors_factory
        self.state = _json_load(self.state_file, {})
        if config_digest and self.state.get("config_digest") != config_digest:
            if self.state.pop("overrides", None):
                log.info("config file changed: it replaces the choices made in the web UI")
            self.state["config_digest"] = config_digest
            self._save_state()
        self.cfg = self._effective(self.state.get("overrides", {}))
        self.lock = threading.RLock()
        self.panel = self.rgb = None
        self.sensors = None
        self.sample = None
        self.panel_info = {"connected": False}
        self.job = {"state": "idle", "done": 0, "total": 0, "label": "", "error": None}
        self.feeder = Feeder(self.cfg.overlay.min_change, self.cfg.overlay.refresh_seconds)
        self._reasserted = self._info_at = 0.0

    # -- settings -----------------------------------------------------------------
    def _effective(self, overrides):
        try:
            return C.with_overrides(self.base, overrides)
        except ValueError as e:
            log.warning("ignoring saved web UI choices: %s", e)
            self.state.pop("overrides", None)
            return self.base

    def _save_state(self):
        _json_save(self.state_file, self.state)

    def _override(self, section, values):
        """Validate and adopt new values for one section; returns the new config."""
        overrides = {**self.state.get("overrides", {})}
        overrides[section] = {**overrides.get(section, {}), **values}
        cfg = C.with_overrides(self.base, overrides)          # raises ValueError if invalid
        return cfg, overrides

    def _commit(self, cfg, overrides):
        self.cfg = cfg
        self.state["overrides"] = overrides
        self._save_state()

    def reload(self, base_cfg, config_digest=None):
        self.base = base_cfg
        if config_digest and config_digest != self.state.get("config_digest"):
            self.state.pop("overrides", None)
            self.state["config_digest"] = config_digest
            self._save_state()
        self.cfg = self._effective(self.state.get("overrides", {}))

    # -- panel lifecycle ---------------------------------------------------------------
    def attach(self, panel):
        with self.lock:
            self.panel = panel
            self.rgb = GpuRgb(panel.t, panel.lock, panel.sleep)
            self._refresh_info()

    def detach(self, clear_overlay=False):
        with self.lock:
            if self.panel and clear_overlay:
                try:
                    self.panel.overlay([])
                except TransportError:
                    pass
            self.panel = self.rgb = None
            self.panel_info = {"connected": False}

    def apply_all(self, force=False):
        with self.lock:
            self._apply_content(force)
            apply_overlay(self.panel, self.cfg)
            self._apply_lighting(force)
            self._refresh_info()

    def _apply_content(self, force=False, progress=None):
        c = self.cfg.content
        if c.type == "none":
            return False
        if c.type == "builtin":
            self.panel.set_mode(c.builtin_mode)
            return False
        upload = CT.build(c)
        digest = CT.fingerprint(upload)
        mode, _ = P.parse_mode(self.panel.query(P.OP_GET_MODE))
        if not force and self.state.get("content") == digest and mode == upload.mode:
            log.info("panel already shows this content; not re-uploading")
            return False
        self.panel.carousel([])
        start = time.monotonic()
        self.panel.upload(upload, self.cfg.panel.chunk_delay, progress=progress)
        log.info("uploaded %s (%d bytes) in %.1fs", c.type, len(upload.payload), time.monotonic() - start)
        self.state["content"] = digest
        self._save_state()
        return True

    def _apply_lighting(self, force=False):
        lc = self.cfg.lighting
        if not lc.enabled or not self.rgb:
            return
        digest = hashlib.sha256(json.dumps(asdict(lc), sort_keys=True).encode()).hexdigest()
        if not force and self.state.get("lighting") == digest:
            return
        self.rgb.apply(lighting_of(self.cfg), save=lc.save_to_card)
        self.state["lighting"] = digest
        self._save_state()

    def _refresh_info(self):
        if not self.panel:
            self.panel_info = {"connected": False}
            return
        try:
            info = self.panel.status()
            self.panel_info = {"connected": True, **info}
        except TransportError as e:
            self.panel_info = {"connected": False, "error": str(e)}
        self._info_at = time.monotonic()

    def tick(self):
        """One stats-loop step; returns seconds until the next one."""
        o = self.cfg.overlay
        if o.enabled and self.sensors is None and self.sensors_factory:
            self.sensors = self.sensors_factory()
        if self.sensors:
            self.sample = self.sensors.read()
        if not self.panel or not self.lock.acquire(blocking=False):
            return 1.0                                   # an upload is running
        try:
            now = time.monotonic()
            if o.enabled and self.sample is not None:
                if now - self._reasserted >= o.refresh_seconds:
                    apply_overlay(self.panel, self.cfg)  # cheap insurance against resets
                    self._reasserted = now
                if self.feeder.should_send(self.sample):
                    self.panel.feed(self.sample)
                    self.feeder.sent(self.sample)
            if now - self._info_at > 5:
                self._refresh_info()
        finally:
            self.lock.release()
        return o.update_seconds if o.enabled else 1.0

    # -- web UI actions ------------------------------------------------------------------
    def status(self):
        return {
            "gpu": self.gpu.describe() if self.gpu else None,
            "panel": self.panel_info,
            "job": dict(self.job),
            "sample": asdict(self.sample) if self.sample else None,
            "settings": {k: asdict(getattr(self.cfg, k)) for k in C.OVERRIDABLE},
            "overridden": bool(self.state.get("overrides")),
            "presets": sorted(_json_load(self.presets_file, {})),
            "widgets": list(P.WIDGETS),
        }

    def preview(self, content_values):
        cfg, _ = self._override("content", content_values)
        data, mime = CT.preview_bytes(cfg.content)
        upload = CT.build(cfg.content)
        return data, mime, (upload.estimate_seconds(self.cfg.panel.chunk_delay) if upload else 0.0)

    def start_content(self, content_values):
        cfg, overrides = self._override("content", content_values)
        if self.job["state"] == "running":
            raise Busy("an upload is already running")
        if not self.panel:
            raise TransportError("the panel is not connected")
        self.job = {"state": "running", "done": 0, "total": 0, "label": cfg.content.type, "error": None}
        threading.Thread(target=self._content_job, args=(cfg, overrides), daemon=True).start()

    def _content_job(self, cfg, overrides):
        def progress(done, total):
            self.job.update(done=done, total=total)
        try:
            with self.lock:
                previous = self.cfg
                self.cfg = cfg
                try:
                    self._apply_content(force=True, progress=progress)
                except BaseException:
                    self.cfg = previous
                    raise
                self._commit(cfg, overrides)
                apply_overlay(self.panel, self.cfg)          # mode change: restate the overlay
                self._refresh_info()
            self.job["state"] = "done"
        except Exception as e:                               # reported to the UI
            log.warning("upload failed: %s", e)
            self.job.update(state="error", error=str(e))

    def set_overlay(self, values):
        cfg, overrides = self._override("overlay", values)
        with self.lock:
            if self.panel:
                apply_overlay(self.panel, cfg)
                self.feeder = Feeder(cfg.overlay.min_change, cfg.overlay.refresh_seconds)
                self._refresh_info()
            self._commit(cfg, overrides)

    def set_lighting(self, values):
        cfg, overrides = self._override("lighting", values)
        with self.lock:
            self._commit(cfg, overrides)
            if cfg.lighting.enabled:
                self._apply_lighting(force=True)

    def set_power(self, on):
        with self.lock:
            if not self.panel:
                raise TransportError("the panel is not connected")
            self.panel.power(on)
            self._refresh_info()

    def reset(self):
        with self.lock:
            self.state.pop("overrides", None)
            self._save_state()
            self.cfg = self.base
            if self.panel:
                self.apply_all(force=False)

    # -- media and presets ----------------------------------------------------------------
    def save_media(self, filename, data):
        ext = Path(filename or "").suffix.lower()
        if ext not in MEDIA_TYPES:
            raise ValueError(f"unsupported file type {ext or '(none)'}; use {', '.join(sorted(MEDIA_TYPES))}")
        path = self.media_dir / (hashlib.sha256(data).hexdigest()[:16] + ext)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        info = {"source": str(path), "name": re.sub(r"[^\w.\- ]", "_", filename)[:80], "frames": 1}
        try:
            from . import render as R
            if ext == ".svg":
                R.load(path, (320, 170))
            else:
                from PIL import Image
                with Image.open(path) as im:
                    info.update(width=im.width, height=im.height, frames=getattr(im, "n_frames", 1))
        except Exception as e:
            path.unlink(missing_ok=True)
            raise ValueError(f"cannot read that file as an image: {e}") from None
        return info

    def presets(self):
        return _json_load(self.presets_file, {})

    def save_preset(self, name):
        name = name.strip()[:40]
        if not name:
            raise ValueError("preset name is empty")
        presets = self.presets()
        presets[name] = {k: asdict(getattr(self.cfg, k)) for k in C.OVERRIDABLE}
        _json_save(self.presets_file, presets)

    def delete_preset(self, name):
        presets = self.presets()
        presets.pop(name, None)
        _json_save(self.presets_file, presets)

    def apply_preset(self, name):
        preset = self.presets().get(name)
        if preset is None:
            raise ValueError(f"no preset named {name!r}")
        cfg = C.with_overrides(self.base, preset)
        with self.lock:
            self.set_overlay(preset["overlay"])
            self.set_lighting(preset["lighting"])
        self.start_content(preset["content"])
        return cfg
