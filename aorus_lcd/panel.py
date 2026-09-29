"""High-level LCD operations on top of a Transport."""
import contextlib
import socket
import threading
import time

from . import protocol as P
from .transport import LCD_ADDRESS, TransportError

# While an upload is open (after F1, until F2 END) the firmware answers every
# command with this D6-style reply instead of running it (LCD firmware 1.3).
UPLOAD_BUSY_REPLY = bytes([P.OP_GET_FW, 0x13, 0x01, 0x02])


class PanelBusy(TransportError):
    """The panel is in the middle of an upload and ignores commands."""


class UploadCancelled(TransportError):
    """An upload was stopped on request; the panel kept its old content."""


class ChunksLost(TransportError):
    """The panel did not write every chunk; it kept its old content."""


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
    def __init__(self, transport, lock=None, sleep=time.sleep, clock=time.monotonic):
        self.t = transport
        self.lock = lock or BusLock()
        self.sleep, self.clock = sleep, clock

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
            reply = self.t.read(LCD_ADDRESS, length)
        if opcode != P.OP_GET_FW and reply[:4] == UPLOAD_BUSY_REPLY:
            raise PanelBusy("the panel is in the middle of an upload (another aorus-lcd process?)")
        return reply

    def probe(self):
        """EB 03, the presence poll Gigabyte's software uses. Raises if silent."""
        return self.query(P.OP_GET_TEMPLATE, b"\x03")

    def wait_ready(self, timeout=60.0, interval=1.0):
        """Right after boot the controller can take a while to answer."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.probe()
            except PanelBusy:
                raise                          # polling would only keep that upload open
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

    def leave_slideshow(self, mode, sample=None):
        """After a reset the panel cycles its own slideshow until a sensor feed
        arrives: send one, then select `mode` again. True if it holds."""
        with self.lock:
            self.feed(sample or P.Sample())
            self.sleep(0.5)
            self.set_mode(mode)
            self.sleep(0.5)
            got, _on = P.parse_mode(self.query(P.OP_GET_MODE))
        return got == mode

    # -- content ------------------------------------------------------------------
    # How uploads are kept safe (LCD firmware 1.3, disassembled and measured):
    # - Outside an upload the firmware takes the command from byte 0 of every
    #   256-byte frame without checking the CB 55 AC 38 magic, so a pixel chunk
    #   must never arrive when no upload is open: it would run as a command.
    # - F1 opens the upload and the panel erases flash. Data must start after
    #   the erase and within 30 s of its end (protocol.header_pause).
    # - During an upload every question is answered with UPLOAD_BUSY_REPLY and
    #   pushes the upload's inactivity timer to 60 s. The erase resets that
    #   timer when it ends, so a question is harmless while erasing; after it,
    #   the panel closes the upload as timed out.
    # - Each question gets exactly one read, once the reply is ready: an early
    #   or second read wedges the panel's I2C interface for a while.
    # - The upload closes by itself once the last chunk is written.
    # So: one question just after F1 (it is erasing) confirms the upload is
    # open; nothing is asked while data flows; one question after the last
    # chunk proves every chunk was written (a normal reply), or, if some were
    # lost (the busy reply), closes the upload so the panel keeps its old
    # animation. Only then does F2 END go out.
    REPLY_SECONDS = 0.4                  # a busy panel prepares its reply within this

    def _ask(self, opcode=P.OP_GET_MODE):
        """One question, one read, after the reply is ready. No busy-check."""
        self.t.write(LCD_ADDRESS, P.query(opcode))
        self.sleep(self.REPLY_SECONDS)
        return self.t.read(LCD_ADDRESS, 4)

    def _enter_mode(self, mode, settle=0.5):
        """Select `mode` and make sure it holds (the slideshow reverts it)."""
        self.set_mode(mode)
        self.sleep(settle)
        for _ in range(2):
            got, _on = P.parse_mode(self.query(P.OP_GET_MODE))
            if got != mode:
                raise TransportError(f"the panel does not stay in mode {mode} (it shows mode {got}); "
                                     "if it cycles on its own, power-cycle the PC fully and retry")
            self.sleep(0.2)

    def _close_upload(self, started):
        """End an open upload without committing it: a question once the erase
        is over times it out (the panel keeps its old animation). Harmless when
        no upload is open."""
        self.sleep(max(0.0, started + self._pause - self.clock()))
        try:
            self._ask()
        except TransportError:
            pass

    def upload(self, upload, chunk_delay=P.FAST_CHUNK_DELAY, show=True, progress=None, cancel=None):
        """Stream an Upload (see _upload_once). If the panel drops a chunk at a
        fast pace, the upload is repeated once at the safe pace."""
        self.erases = 0                                  # flash erases this upload cost (wear)
        try:
            return self._upload_once(upload, max(0.0, chunk_delay), show, progress, cancel)
        except ChunksLost:
            if chunk_delay >= P.PACE_CHUNK:
                raise
        return self._upload_once(upload, P.PACE_CHUNK, show, progress, cancel)

    def _upload_once(self, upload, chunk_delay, show, progress, cancel):
        """Stream an Upload and (optionally) switch to it. The whole upload
        holds the bus lock so nothing interleaves.

        GIF uploads hold playback (the screen keeps its last frame) and skip
        the BEGIN marker (it puts up Gigabyte's Loading screen). There is no
        way to write while the animation plays: see protocol.py. Setting the
        `cancel` event stops it between chunks."""
        frames = upload.frames
        begin, header, chunks, end = frames[0], frames[1], frames[2:-1], frames[-1]
        gif = upload.kind == "gif"
        self.erases += 1
        pause = self._pause = P.header_pause(len(upload.payload), upload.erase_mode)
        if pause > P.HOST_SILENCE_SECONDS - 4:
            raise ValueError(f"an upload this big ({len(upload.payload)} bytes) waits {pause:.0f} s for its "
                             f"erase; the panel gives up on the PC after {P.HOST_SILENCE_SECONDS} s")
        with self.lock:
            # A sensor feed first: the panel counts it as the PC being there,
            # and the erase wait that follows is silent (see GIF_MAX_PAYLOAD).
            self.feed(P.Sample())
            self.sleep(0.1)
            if gif:
                # GIF data lands on the GIF flash chip only while GIF mode is on
                # screen (each mode's draw routine picks its chip on every tick),
                # and the bank on screen must not be decoded meanwhile: it may be
                # damaged, and decoding makes each flash erase far slower.
                self.send(P.gif_hold())
                self.sleep(0.3)
                self._enter_mode(P.MODE_GIF)
            else:
                if show and upload.mode_first:
                    self.set_mode(upload.mode)
                    self.sleep(0.2)
                self._write_retrying(begin)
                self.sleep(P.PACE_BEGIN)
            self._write_retrying(header)
            started = self.clock()
            self.sleep(0.1)                  # it must take F1 before another frame replaces it
            try:
                opened = self._ask() == UPLOAD_BUSY_REPLY
            except TransportError:
                opened = False
            if not opened:
                self._close_upload(started)
                raise TransportError("the panel did not open the upload; no data was sent")
            self.sleep(max(0.0, started + pause - self.clock()))
            total = len(frames)
            for i, chunk in enumerate(chunks):
                if cancel is not None and cancel.is_set():
                    self._close_upload(started)
                    raise UploadCancelled("upload cancelled; the panel keeps its content")
                self._write_retrying(chunk)
                self.sleep(chunk_delay)
                if progress:
                    progress(i + 3, total)
            self.sleep(0.2)
            if self._ask() == UPLOAD_BUSY_REPLY:
                raise ChunksLost("the panel lost part of the upload; it keeps its old content")
            self._write_retrying(end)
            if progress:
                progress(total, total)
            if gif:
                self._switch_checked()
            elif show:
                self.show(upload.mode)

    def _switch_checked(self):
        """After a GIF upload's END: END restores the mode saved by the last
        BEGIN marker and flips to the new bank only if that mode is GIF. After
        the panel powers up nothing is saved (mode 0), so the first END goes
        wrong; then select GIF mode and send BEGIN + END, which saves GIF mode
        and flips (Gigabyte's Loading screen shows for about a frame)."""
        self.sleep(0.3)
        mode, _on = P.parse_mode(self.query(P.OP_GET_MODE))
        if mode == P.MODE_GIF:
            return
        self.set_mode(P.MODE_GIF)
        self.sleep(0.3)
        self._write_retrying(P.upload_marker(True))
        self.sleep(0.2)
        self._write_retrying(P.upload_marker(False))
        self.sleep(0.3)
        mode, _on = P.parse_mode(self.query(P.OP_GET_MODE))
        if mode != P.MODE_GIF:
            raise TransportError(f"the panel did not switch to the new animation (mode {mode})")

    def _write_retrying(self, frame, attempts=3):
        """The panel occasionally NACKs a write while it is busy; a long upload
        should not die of one. The retry resends the whole frame."""
        for attempt in range(attempts):
            try:
                self.t.write(LCD_ADDRESS, frame)
                return
            except TransportError:
                if attempt == attempts - 1:
                    raise
                self.sleep(0.05 * (attempt + 1))

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
