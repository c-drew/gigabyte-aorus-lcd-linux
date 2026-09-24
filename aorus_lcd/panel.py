"""High-level LCD operations on top of a Transport."""
import contextlib
import socket
import threading
import time

from . import protocol as P
from .transport import LCD_ADDRESS, TransportError


class BusLock:
    """Cross-process lock (daemon vs CLI) as an abstract Unix socket: works across
    users and private /tmp, and the kernel drops it if the holder dies. Also
    re-entrant and thread-safe within a process (web server + stats loop)."""

    def __init__(self, name="aorus-lcd", timeout=30.0):
        self.name, self.timeout = name, timeout
        self._sock = None
        self._depth = 0
        self._thread_lock = threading.RLock()

    def __enter__(self):
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise TransportError("the bus is busy")
        try:
            if self._depth == 0:
                self._bind()
        except BaseException:
            self._thread_lock.release()
            raise
        self._depth += 1
        return self

    def _bind(self):
        deadline = time.monotonic() + self.timeout
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind("\0" + self.name)
                self._sock = sock
                return
            except OSError:
                sock.close()
                if time.monotonic() > deadline:
                    raise TransportError("another aorus-lcd process holds the bus") from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            self._sock.close()
            self._sock = None
        self._thread_lock.release()


class Panel:
    def __init__(self, transport, lock=None, sleep=time.sleep):
        self.t = transport
        self.lock = lock or BusLock()
        self.sleep = sleep

    def close(self):
        self.t.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- primitives ------------------------------------------------------------
    def send(self, frame):
        with self.lock:
            self.t.write(LCD_ADDRESS, frame)

    def query(self, opcode, tail=b"", length=4):
        """Write a query frame and read the reply. Replies are 4 bytes; reading
        more than the firmware prepared makes it stretch the clock (timeout)."""
        with self.lock:
            self.t.write(LCD_ADDRESS, P.query(opcode, tail))
            return self.t.read(LCD_ADDRESS, length)

    def probe(self):
        """EB 03, the presence poll Gigabyte's software uses. Raises if silent."""
        return self.query(P.OP_GET_TEMPLATE, b"\x03")

    def wait_ready(self, timeout=60.0, interval=1.0):
        """Right after boot the controller can take a while to answer."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.probe()
            except TransportError:
                if time.monotonic() > deadline:
                    raise
                self.sleep(interval)

    def status(self):
        fw = P.parse_firmware(self.query(P.OP_GET_FW))
        mode, on = P.parse_mode(self.query(P.OP_GET_MODE))
        widgets, interval = P.parse_display(self.query(P.OP_GET_DISPLAY))
        return {"firmware": fw, "mode": mode, "on": on, "widgets": widgets, "interval": interval}

    # -- display state ------------------------------------------------------------
    def power(self, on):
        self.send(P.open_lcd(on))

    def set_mode(self, mode):
        self.send(P.set_mode(mode))

    def carousel(self, modes, interval=0):
        self.send(P.set_loop(modes, interval))

    def overlay(self, widgets, interval_s=3):
        self.send(P.set_display(widgets, interval_s))

    def template(self, color, image_pos=(0, 0), data_pos=(0, 0), enabled=True, kind=P.TEMPLATE_IMAGE):
        self.send(P.set_template(color, image_pos, data_pos, enabled, kind))

    def feed(self, sample):
        self.send(P.sensor_feed(sample))

    def save(self):
        self.send(P.save())

    # -- content ------------------------------------------------------------------
    def upload(self, upload, chunk_delay=P.PACE_CHUNK, show=True, progress=None):
        """Stream an Upload with GCC's pacing and (optionally) switch to it.
        The whole upload holds the bus lock so nothing interleaves."""
        frames = upload.frames
        with self.lock:
            if show and upload.mode_first:
                self.set_mode(upload.mode)
                self.sleep(0.2)
            for i, frame in enumerate(frames):
                self.t.write(LCD_ADDRESS, frame)
                self.sleep(P.PACE_BEGIN if i == 0 else upload.header_pause if i == 1 else chunk_delay)
                if progress:
                    progress(i + 1, len(frames))
            if show and not upload.mode_first:
                self.show(upload.mode)

    def show(self, mode):
        """Switch to `mode` so it repaints. Re-selecting the current mode does not
        repaint a freshly uploaded framebuffer, so nudge through another mode."""
        with self.lock:
            current, _ = P.parse_mode(self.query(P.OP_GET_MODE))
            if current == mode:
                self.set_mode(P.MODE_CHIBI if mode != P.MODE_CHIBI else P.MODE_IMAGE)
                self.sleep(0.3)
            self.set_mode(mode)


@contextlib.contextmanager
def open_panel(kind="nvrm", gpu=None, speed_khz=400):
    from .transport import open_transport
    with open_transport(kind, gpu, speed_khz) as t:
        yield Panel(t)
