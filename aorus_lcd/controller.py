"""The service's single owner of the panel, shared by the stats loop and the web UI.

Effective settings = config.toml + overrides chosen in the UI (saved in the
state directory). If config.toml changes, it wins and the overrides are
dropped. Presets are named snapshots of content + overlay + lighting.
"""
import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import config as C
from . import content as CT
from . import playlist
from . import protocol as P
from .lighting import GpuRgb, Lighting
from .panel import UploadCancelled
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
        self._reasserted = self._info_at = self._recovered_at = self._fed_at = 0.0
        # Screensaver rotation. While a background upload streams, nothing else
        # may reach the panel (a command would end the upload): changes made
        # meanwhile are kept in _pending and applied when it is over.
        self.rotation = playlist.Rotation(self.state)
        self.bg = {"phase": "idle", "effect": "", "done": 0, "total": 0, "error": None, "starts_at": 0}
        self._bg_thread, self._bg_cancel, self._pending = None, threading.Event(), {}
        self._job_thread, self._job_cancel = None, threading.Event()
        self._quiet_until = 0.0

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

    STOP_WAIT = 80      # an upload still erasing closes only once the erase is over
    SETTLE_SECONDS = 40  # after a failed upload: the panel closes it 30 s after the last data

    def _settle(self):
        """An upload failed part way: leave the panel alone until it has
        closed the upload by itself, so nothing lands in the middle of it."""
        self._quiet_until = time.monotonic() + self.SETTLE_SECONDS

    def settling_seconds(self):
        return max(0, round(self._quiet_until - time.monotonic()))

    def detach(self, clear_overlay=False, reason=""):
        """Let go of the panel. Uploads stop at the next chunk and close, so
        the panel is not left waiting inside one (a restart would then have to
        stay off the bus until it gives up)."""
        self._bg_cancel.set()
        self._job_cancel.set()
        for thread in (self._bg_thread, self._job_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(self.STOP_WAIT)
        with self.lock:
            if self.panel and clear_overlay:
                try:
                    self.panel.overlay([])
                except TransportError:
                    pass
            self.panel = self.rgb = None
            self.panel_info = {"connected": False, **({"error": reason} if reason else {})}

    def apply_all(self, force=False):
        with self.lock:
            self._apply_content(force)
            apply_overlay(self.panel, self.cfg)
            self._apply_lighting(force)
            self._refresh_info()

    def _apply_content(self, force=False, progress=None, cancel=None):
        c = self.cfg.content
        if c.type == "none":
            return False
        if c.type == "builtin":
            self.panel.set_mode(c.builtin_mode)
            return False
        quick = CT.quick_fingerprint(c)
        upload = None if quick else CT.build(c)
        digest, target_mode = quick or (CT.fingerprint(upload), upload.mode)
        unsure = self.state.get("flash_unsure")
        mode, _ = P.parse_mode(self.panel.query(P.OP_GET_MODE))
        if mode == P.MODE_SLIDESHOW and self.panel.leave_slideshow(target_mode, self.sample) and not unsure:
            mode = target_mode                         # it had reset; the flash kept the content
        if unsure:
            force = True       # an upload failed or was cut off: what the panel shows is unknown
        if not force and self.state.get("content") == digest and mode == target_mode:
            log.info("panel already shows this content; not re-uploading")
            return False
        index = self.rotation.state["index"] if c.type == "ttfx" and c.rotate_minutes else 0
        if upload is None:
            self.job["label"] = f"{c.type}: rendering"
            upload = CT.animation(c, index).upload() if c.type == "ttfx" else CT.build(c)
            self.job["label"] = c.type
        self.panel.carousel([])
        start = time.monotonic()
        bank = self.panel.query(P.OP_GET_BANK)[1] if upload.kind == "gif" else None
        self._flash_unsure("foreground")
        try:
            self.panel.upload(upload, self.cfg.panel.chunk_delay, progress=progress, cancel=cancel)
        finally:
            if bank in (1, 2):
                self.rotation.erased(3 - bank)
                self._save_state()
        self._flash_unsure(None)
        log.info("uploaded %s (%d bytes) in %.1fs", c.type, len(upload.payload), time.monotonic() - start)
        self.state["content"] = digest
        self.state["content_mode"] = target_mode
        if c.type == "ttfx" and c.rotate_minutes:
            self.rotation.switched(index, CT.playlist_order(c)[index % len(CT.playlist_order(c))])
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
        if self.streaming or self.settling_seconds():
            return 1.0                                   # an upload owns the panel, or just failed
        self._maybe_rotate()
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
                    self._fed_at = now
            if now - self._fed_at >= self.HEARTBEAT_SECONDS:
                # The panel takes a sensor feed as a sign of life from the PC:
                # without one for ~25 s it goes back to its own slideshow. With
                # the overlay off it draws nothing.
                self.panel.feed(self.sample or P.Sample())
                self._fed_at = now
            if now - self._info_at > 5:
                self._refresh_info()
                self._recover_from_reset()
            if self._pending:
                self._apply_pending()
        finally:
            self.lock.release()
        return o.update_seconds if o.enabled else 1.0

    RECOVER_EVERY = 60
    HEARTBEAT_SECONDS = 5

    def _content_mode(self):
        c = self.cfg.content
        if c.type == "none":
            return None
        if c.type == "builtin":
            return c.builtin_mode
        quick = CT.quick_fingerprint(c)
        return quick[1] if quick else self.state.get("content_mode")

    def _flash_unsure(self, kind):
        """Which upload ("foreground") is running, kept until it
        succeeds. If the panel resets meanwhile (it crashed), what its flash
        holds is unknown; a foreground upload cut off leaves playback held. In
        both cases the content is uploaded again."""
        if kind:
            self.state["flash_unsure"] = kind
        else:
            self.state.pop("flash_unsure", None)
        self._save_state()

    def _recover_from_reset(self):
        """A panel that reset (a power blip, a crash) plays its own slideshow;
        bring back what the service put on it: the flash keeps it, unless the
        panel reset during an upload."""
        if self.panel_info.get("mode") != P.MODE_SLIDESHOW:
            return
        mode = self._content_mode()
        if mode is None or time.monotonic() - self._recovered_at < self.RECOVER_EVERY:
            return
        self._recovered_at = time.monotonic()
        held = self.panel.leave_slideshow(mode, self.sample)
        log.warning("the panel had reset to its slideshow; %s",
                    f"mode {mode} is back" if held else f"it did not stay in mode {mode}")
        self._refresh_info()
        if held and self.state.get("flash_unsure") and self.job["state"] != "running":
            log.warning("it reset during an upload; uploading the content again")
            self._start_upload(self.cfg, self.state.get("overrides", {}))

    # -- screensaver rotation ----------------------------------------------------------------
    ROTATE_LEAD = 30 * 60        # start rendering this early; the upload itself starts on time

    @property
    def streaming(self):
        return self.bg["phase"] == "uploading"

    def _rotation_active(self, c=None):
        c = c or self.cfg.content
        quick = CT.quick_fingerprint(c) if c.type == "ttfx" else None
        return bool(c.type == "ttfx" and c.rotate_minutes and quick
                    and self.state.get("content") == quick[0])

    def _maybe_rotate(self, now=None):
        c = self.cfg.content
        if not self.panel or not self._rotation_active(c) or self.job["state"] == "running":
            return
        if self._bg_thread and self._bg_thread.is_alive():
            return
        now = now or time.time()
        if now < self.rotation.state.get("retry_at", 0):
            return
        if self.state.get("flash_unsure"):
            # The last upload failed part way: write the playlist again.
            log.warning("the last upload did not finish; uploading the playlist again")
            self._start_upload(self.cfg, self.state.get("overrides", {}))
            return
        if now < self.rotation.due_at(c) - self.ROTATE_LEAD:
            return
        self._bg_cancel.clear()
        self._bg_thread = threading.Thread(target=self._rotate_job, args=(c,), daemon=True)
        self._bg_thread.start()

    def rotate_now(self):
        """Swap in the next loop as soon as it is written (no waiting for the timer)."""
        if not self._rotation_active():
            raise ValueError("the screensaver playlist is not on the panel")
        self.rotation.state["switched_at"] = 0.0
        self.rotation.state.pop("retry_at", None)
        if self._bg_thread and self._bg_thread.is_alive() and self.bg["phase"] == "waiting":
            self.bg["starts_at"] = 0
        self._maybe_rotate()

    def _rotate_job(self, c, clock=time.time):
        order = CT.playlist_order(c)
        index = self.rotation.next_index(order)
        effect = order[index]
        self.bg = {"phase": "rendering", "effect": effect, "done": 0, "total": 0, "error": None, "starts_at": 0}
        bank = None
        try:
            upload = CT.animation(c, index).upload()
            self.bg.update(phase="waiting", starts_at=self.rotation.due_at(c) - playlist.estimate_seconds(upload))
            while clock() < self.bg["starts_at"]:
                if self._bg_cancel.wait(min(5.0, max(0.1, self.bg["starts_at"] - clock()))):
                    raise UploadCancelled("rotation cancelled")
            with self.lock:
                if self._bg_cancel.is_set() or self.cfg.content != c or not self.panel:
                    raise UploadCancelled("rotation cancelled")
                bank = self.panel.query(P.OP_GET_BANK)[1]
                self.bg["phase"] = "uploading"
            # Written with playback held (the screen keeps its last frame for
            # a minute or two), not while it plays: see protocol.py.
            self._flash_unsure("foreground")
            self.panel.upload(upload, self.cfg.panel.chunk_delay, cancel=self._bg_cancel,
                              progress=lambda done, total: _progress(self.bg, done, total))
            self._flash_unsure(None)
            self.rotation.switched(index, effect)
            self.rotation.state.pop("retry_at", None)
            self.bg["phase"] = "idle"
            log.info("screensaver: now showing %s", effect)
        except UploadCancelled:
            # Cut off after playback was held, it is written again (flash_unsure).
            self.bg["phase"] = "idle"
        except (TransportError, ValueError, RuntimeError, OSError) as e:
            if self.bg["phase"] == "uploading":
                # Once the panel has closed the failed upload, write it again.
                self._settle()
                log.warning("screensaver rotation failed: %s; uploading again shortly", e)
                self.rotation.state["retry_at"] = clock() + self.SETTLE_SECONDS + 5
            else:
                log.warning("screensaver rotation failed: %s; retrying in 10 minutes", e)
                self.rotation.state["retry_at"] = clock() + 600
            self.bg.update(phase="error", error=str(e))
        finally:
            if bank in (1, 2) and self.bg["phase"] != "rendering":
                self.rotation.erased(3 - bank, getattr(self.panel, "erases", 1) if self.panel else 1)
            self._save_state()

    def _cancel_background(self, wait=True):
        self._bg_cancel.set()
        if wait and self._bg_thread and self._bg_thread.is_alive():
            self._bg_thread.join()

    def _apply_pending(self):
        pending, self._pending = self._pending, {}
        if "overlay" in pending:
            apply_overlay(self.panel, self.cfg)
        if "lighting" in pending and self.cfg.lighting.enabled:
            self._apply_lighting(force=True)
        if "power" in pending:
            self.panel.power(pending["power"])
        self._refresh_info()

    # -- web UI actions ------------------------------------------------------------------
    def status(self):
        return {
            "gpu": self.gpu.describe() if self.gpu else None,
            "panel": self.panel_info,
            "job": _with_eta(self.job),
            "settling_seconds": self.settling_seconds(),
            "sample": asdict(self.sample) if self.sample else None,
            "settings": {k: asdict(getattr(self.cfg, k)) for k in C.OVERRIDABLE},
            "overridden": bool(self.state.get("overrides")),
            "presets": sorted(_json_load(self.presets_file, {})),
            "widgets": list(P.WIDGETS),
            "ttfx": self._ttfx_info(),
            "screensaver": self._screensaver_status(),
        }

    def _screensaver_status(self):
        c = self.cfg.content
        out = {"rotating": self._rotation_active(c), "background": _with_eta(self.bg),
               "wear": self.rotation.report(c if c.type == "ttfx" else None),
               "pending": sorted(self._pending)}
        if c.type == "ttfx" and c.rotate_minutes:
            order = CT.playlist_order(c)
            out.update(order=order, effect=self.rotation.state.get("effect", ""),
                       next=order[self.rotation.next_index(order)], due_at=self.rotation.due_at(c))
        return out

    def _ttfx_info(self):
        """Effects the installed ttfx offers (asked once, for the web UI)."""
        if getattr(self, "_ttfx", None) is None:
            from . import screensaver, ttfx
            try:
                binary = ttfx.find_binary(self.cfg.content.ttfx)
                from .glyphs import monospace_fonts
                self._ttfx = {"version": ttfx.version(binary), "effects": list(ttfx.effects(binary)),
                              "default": list(screensaver.DEFAULT_EFFECTS),
                              "max_mb": round(P.GIF_MAX_PAYLOAD / 1e6, 1), "fonts": monospace_fonts()}
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                self._ttfx = {"error": str(e), "effects": [], "default": []}
        return self._ttfx

    def preview(self, content_values):
        """(image bytes, mime, upload seconds, info): info describes a screensaver
        render (which effects made it in, size, loop length) for the web UI."""
        cfg, _ = self._override("content", content_values)
        c = cfg.content
        data, mime = CT.preview_bytes(c)
        upload = CT.build(c)
        info = {}
        if c.type == "ttfx":
            anim = CT.animation(c)
            info = {"included": [clip.effect for clip in anim.clips],
                    "skipped": [effect for effect, _ in anim.skipped],
                    "bytes": len(upload.payload), "seconds": round(len(anim.frames) / c.fps, 1),
                    "frames": len(anim.frames)}
            if c.rotate_minutes:
                info["background_seconds"] = round(playlist.estimate_seconds(upload))
        return data, mime, (upload.estimate_seconds(self.cfg.panel.chunk_delay) if upload else 0.0), info

    def start_content(self, content_values):
        cfg, overrides = self._override("content", content_values)
        if self.job["state"] == "running":
            raise Busy("an upload is already running")
        if self.settling_seconds():
            raise Busy(f"the panel is closing a failed upload; try again in {self.settling_seconds()} s")
        unchanged = cfg.content == self.cfg.content      # sent again: upload it again
        if not unchanged and self._rotation_active() and self._rotation_active(cfg.content):
            # Only the playlist changed (effects, order, interval): no upload.
            with self.lock:
                self._commit(cfg, overrides)
            self.job = {"state": "done", "done": 0, "total": 0, "label": "playlist saved", "error": None}
            return
        if not self.panel:
            raise TransportError(self.panel_info.get("error") or "the panel is not connected")
        self._start_upload(cfg, overrides)

    def _start_upload(self, cfg, overrides):
        self.job = {"state": "running", "done": 0, "total": 0, "label": cfg.content.type, "error": None}
        self._job_cancel.clear()
        self._job_thread = threading.Thread(target=self._content_job, args=(cfg, overrides), daemon=True)
        self._job_thread.start()

    def _content_job(self, cfg, overrides):
        def progress(done, total):
            _progress(self.job, done, total)
        try:
            if self._bg_thread and self._bg_thread.is_alive():
                self.job["label"] = "stopping the screensaver upload"
                self._cancel_background()
                self.job["label"] = cfg.content.type
            if cfg.content.type == "ttfx" and cfg.content.rotate_minutes:
                self.rotation.state["index"] = 0
            with self.lock:
                previous = self.cfg
                self.cfg = cfg
                try:
                    self._apply_content(force=True, progress=progress, cancel=self._job_cancel)
                except BaseException:
                    self.cfg = previous
                    raise
                self._commit(cfg, overrides)
                apply_overlay(self.panel, self.cfg)          # mode change: restate the overlay
                self._refresh_info()
            self.job["state"] = "done"
        except UploadCancelled as e:
            self.job.update(state="error", error=str(e))
        except Exception as e:                               # reported to the UI
            log.warning("upload failed: %s", e)
            self.job.update(state="error", error=str(e))
            if isinstance(e, TransportError):
                self._settle()
            self.rotation.state["retry_at"] = time.time() + 600

    def set_overlay(self, values):
        cfg, overrides = self._override("overlay", values)
        with self.lock:
            self._commit(cfg, overrides)
            self.feeder = Feeder(cfg.overlay.min_change, cfg.overlay.refresh_seconds)
            if self.streaming:
                self._pending["overlay"] = True          # applied when the upload is over
            elif self.panel:
                apply_overlay(self.panel, cfg)
                self._refresh_info()

    def set_lighting(self, values):
        cfg, overrides = self._override("lighting", values)
        with self.lock:
            self._commit(cfg, overrides)
            if self.streaming:
                self._pending["lighting"] = True
            elif cfg.lighting.enabled:
                self._apply_lighting(force=True)

    def set_power(self, on):
        with self.lock:
            if not self.panel:
                raise TransportError(self.panel_info.get("error") or "the panel is not connected")
            if self.streaming:
                self._pending["power"] = bool(on)
                if not on:
                    self._bg_cancel.set()                # no point finishing it for a dark panel
                return
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


def _progress(record, done, total):
    """Chunk progress, and when the chunks started (for the time left)."""
    if not record.get("since") or done < record.get("done", 0):
        record["since"] = time.monotonic()
    record.update(done=done, total=total)


def _with_eta(record):
    """A copy of a job/upload record with "eta_seconds" (None until measurable)."""
    out = {k: v for k, v in record.items() if k != "since"}
    since, done, total = record.get("since"), record.get("done", 0), record.get("total", 0)
    running = record.get("state") == "running" or record.get("phase") == "uploading"
    out["eta_seconds"] = None
    if running and since and done > 20 and total:
        out["eta_seconds"] = round((time.monotonic() - since) / done * (total - done))
    return out
