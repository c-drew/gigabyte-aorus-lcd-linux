"""The boot service: put the configured content on the panel, keep the
firmware's GPU-stats overlay fed, and serve the web UI.

Content is only re-uploaded when it changed (the panel keeps its image across
power cycles, and every upload rewrites its flash). Each overlay update is a
256-byte I2C write (~6 ms of driver time), so samples are only sent when a
value moves past `min_change`, plus a periodic refresh.
"""
import logging
import os
import signal
import time
from pathlib import Path

from . import config as C
from .controller import Controller
from .panel import Panel
from .transport import TransportError, find_gpu, open_transport

log = logging.getLogger("aorus-lcd")


def state_dir():
    return Path(os.environ.get("STATE_DIRECTORY") or
                Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "aorus-lcd")


def load_config(path):
    if Path(path).exists():
        return C.load(path)
    log.info("no %s; using defaults (set things up in the web UI)", path)
    return C.Config()


def run(config_path=C.DEFAULT_PATH, force=False, web=True, on_ready=None):
    base = load_config(config_path)
    stop = {"now": False, "reload": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGHUP, lambda *_: stop.update(reload=True))

    gpu = find_gpu(base.panel.gpu or None)
    log.info("GPU: %s", gpu.describe())

    def sensors():
        from .sensors import NvmlSensors
        return NvmlSensors(gpu.pci)

    ctl = Controller(base, state_dir(), C.file_digest(config_path), gpu, sensors)
    server = None
    if web and ctl.cfg.web.enabled:
        from .web import serve
        try:
            server = serve(ctl, ctl.cfg.web.bind, ctl.cfg.web.port, ctl.cfg.web.max_upload_mb)
            log.info("web UI: http://%s:%d/", ctl.cfg.web.bind, ctl.cfg.web.port)
        except OSError as e:
            log.warning("web UI disabled: cannot listen on %s:%d (%s)", ctl.cfg.web.bind, ctl.cfg.web.port, e)
    if on_ready:
        on_ready(ctl, server)

    while not stop["now"]:
        try:
            with open_transport(ctl.cfg.panel.transport, gpu, ctl.cfg.panel.speed_khz) as t:
                panel = Panel(t)
                log.info("panel answers: %s", panel.wait_ready().hex(" "))
                panel.power(True)
                ctl.attach(panel)
                ctl.apply_all(force)
                force = False
                while not stop["now"] and not stop["reload"]:
                    wait = ctl.tick()
                    end = time.monotonic() + wait
                    while time.monotonic() < end and not stop["now"] and not stop["reload"]:
                        time.sleep(0.1)
                clear = ctl.cfg.overlay.enabled and ctl.cfg.overlay.clear_on_exit and not stop["reload"]
                ctl.detach(clear_overlay=clear)
                if stop["reload"]:
                    stop["reload"] = False
                    ctl.reload(load_config(config_path), C.file_digest(config_path))
                    log.info("config reloaded")
        except TransportError as e:
            ctl.detach()
            log.warning("bus error: %s; retrying in 5s", e)
            for _ in range(50):
                if stop["now"]:
                    break
                time.sleep(0.1)
    if server:
        server.shutdown()
    if ctl.sensors:
        ctl.sensors.close()
    log.info("stopped")
