"""Panel sequencing, config parsing and the daemon's decisions, on a fake bus."""
import pytest

from aorus_lcd import config as C
from aorus_lcd import controller as CTL
from aorus_lcd import protocol as P
from aorus_lcd.panel import BusLock, Panel
from aorus_lcd.transport import Transport, TransportError


BUSY = bytes([P.OP_GET_FW, 0x13, 0x01, 0x02])


class FakeLcd(Transport):
    """LCD firmware 1.3 on a virtual clock (advanced by the panel's sleeps),
    with the rules measured on the real panel:
    - F1 opens an upload and erases for 0.5 s + 30 ms per sector (`erase_rate`);
      a chunk during the erase lands in the wrong place (`misplaced`);
    - after the erase, 30 s without data closes the upload, and so does any
      question; questions during the erase are harmless; every question during
      an upload is answered with the busy reply;
    - the upload closes itself after its last chunk;
    - each question may be read once, no sooner than 0.2 s later, or the I2C
      interface wedges (`wedged`);
    - a chunk that arrives with no upload open runs as a command (`stray`);
    - F2 END restores the mode saved by the last BEGIN and flips the GIF bank
      only if that is GIF mode; at power-up nothing is saved (mode byte 0)."""

    def __init__(self, ignore_header=False, ignore_modes=False, lose_chunk=False, erase_rate=0.03,
                 saved_mode=P.MODE_GIF, mode=P.MODE_TEXT):
        self.writes, self.mode, self.bank, self.now = [], mode, 1, 0.0
        self.saved_mode, self.begins, self.chunk_times = saved_mode, 0, []
        self.open, self.erase_end, self.last_data, self.expected, self.data = False, 0.0, 0.0, 0, 0
        self.stray = self.misplaced = self.wedged = 0
        self.reply, self.asked_at = None, None
        self.ignore_header, self.ignore_modes = ignore_header, ignore_modes
        self.lose_chunk, self.erase_rate = lose_chunk, erase_rate

    def sleep(self, seconds):
        self.now += seconds

    def _expire(self):
        if self.open and self.now > max(self.erase_end, self.last_data) + P.DATA_TIMEOUT_SECONDS:
            self.open = False

    def write(self, address, data):
        self.check(address, len(data))
        assert len(data) == 256, "the firmware only parses full 256-byte frames"
        self.now += 0.007
        self.writes.append(bytes(data))
        self._expire()
        magic = data[1:5] == P.MAGIC
        if self.open:
            if not magic:
                if self.now < self.erase_end:
                    self.misplaced += 1
                if self.lose_chunk:
                    self.lose_chunk = False                # this one never arrives
                    return
                self.data, self.last_data = self.data + 1, self.now
                self.chunk_times.append(self.now)
                if self.data >= self.expected:
                    self.open = False                      # every chunk written
            else:
                self.reply, self.asked_at = BUSY, self.now
                if self.now >= self.erase_end:
                    self.open = False                      # a question after the erase: timed out
            return
        if not magic:
            self.stray += 1
            return
        self.reply, self.asked_at = None, None
        op = data[0]
        if op == P.OP_UPLOAD_HEADER and not self.ignore_header:
            size = int.from_bytes(data[10:14], "big") * 256
            self.open, self.data, self.expected = True, 0, int.from_bytes(data[10:14], "big")
            self.erase_end = self.now + 0.5 + self.erase_rate * P.erase_count(size, P.ERASE_SECTOR)
        elif op == P.OP_UPLOAD_MARK and data[5] == 1:
            self.saved_mode, self.begins = self.mode, self.begins + 1
        elif op == P.OP_UPLOAD_MARK and data[5] == 2:
            self.mode = self.saved_mode
            if self.mode == P.MODE_GIF:
                self.bank = 3 - self.bank
        elif op == P.OP_SET_MODE and not self.ignore_modes:
            self.mode = data[5] - 1
        elif op == P.OP_GET_MODE:
            self.reply, self.asked_at = bytes([op, self.mode + 1, 1, 1]), self.now
        elif op in (P.OP_GET_BANK, P.OP_GET_FW, P.OP_GET_DISPLAY, P.OP_GET_TEMPLATE):
            self.reply, self.asked_at = bytes([op, self.bank, 1, 2]), self.now

    def read(self, address, length):
        assert length == 4
        busy = self.reply == BUSY                          # a busy panel is slow to answer
        if self.reply is None or (busy and self.now - self.asked_at < 0.2):
            self.wedged += 1
            raise TransportError("read timed out")
        reply, self.reply = self.reply, None
        return reply

    def ping(self, address):
        return True

    def opcodes(self):
        return [w[0] for w in self.writes]


def _panel(lcd):
    return Panel(lcd, lock=BusLock("aorus-lcd-test"), sleep=lcd.sleep, clock=lambda: lcd.now)


def test_rgb_controller_is_write_only():
    Transport.check(0x75, 64)                      # writes are how it is driven
    with pytest.raises(ValueError, match="write-only"):
        Transport.check(0x75, 4, write=False)      # a read wedges the bus
    with pytest.raises(ValueError):
        Transport.check(0x50, 1)                   # nothing else on the port


def test_still_upload_then_mode():
    lcd = FakeLcd()
    _panel(lcd).upload(P.still_upload(bytes(P.FRAME_BYTES)))
    ops = lcd.opcodes()
    assert ops[:2] == [P.OP_SENSOR_FEED, 0xF2]                   # a sign of life, then BEGIN
    assert ops[ops.index(0xF1) + 1] == P.OP_GET_MODE
    assert ops[-3:] == [0xF2, P.OP_GET_MODE, P.OP_SET_MODE]      # END, then show the image
    assert lcd.mode == P.MODE_IMAGE


def test_reselecting_the_same_mode_nudges_through_another():
    lcd = FakeLcd()
    lcd.mode = P.MODE_IMAGE
    _panel(lcd).show(P.MODE_IMAGE)
    modes = [w[5] - 1 for w in lcd.writes if w[0] == P.OP_SET_MODE]
    assert modes == [P.MODE_CHIBI, P.MODE_IMAGE]


def _clean(lcd):
    return lcd.stray == 0 and lcd.misplaced == 0 and lcd.wedged == 0


def test_gif_holds_playback_and_sets_mode_before_streaming():
    lcd = FakeLcd()
    upload = P.gif_upload([bytes(P.FRAME_BYTES)] * 2, 100)
    _panel(lcd).upload(upload)
    ops = lcd.opcodes()
    assert ops[:3] == [P.OP_SENSOR_FEED, P.OP_GIF_HOLD, P.OP_SET_MODE]
    header = ops.index(P.OP_UPLOAD_HEADER)
    assert ops[header + 1] == P.OP_GET_MODE                      # the question, while erasing
    data = lcd.writes[header + 2: header + 2 + len(upload.frames) - 3]
    assert all(w[1:5] != P.MAGIC for w in data)                  # nothing asked while data flows
    assert lcd.data == len(upload.frames) - 3 and _clean(lcd)
    assert ops[-3:] == [P.OP_GET_MODE, P.OP_UPLOAD_MARK, P.OP_GET_MODE] and lcd.bank == 2
    assert lcd.begins == 0, "the BEGIN marker puts up Gigabyte's Loading screen"
    assert len(upload.payload) >= P.GIF_MIN_PAYLOAD


def test_first_upload_after_power_up_repairs_the_switch():
    """At power-up END has no saved mode: it would leave GIF mode without flipping."""
    lcd = FakeLcd(saved_mode=-1)
    _panel(lcd).upload(P.gif_upload([bytes(P.FRAME_BYTES)] * 2, 100))
    assert lcd.mode == P.MODE_GIF and lcd.bank == 2 and lcd.begins == 1 and _clean(lcd)
    _panel(lcd).upload(P.gif_upload([bytes(P.FRAME_BYTES)] * 2, 100))
    assert lcd.bank == 1 and lcd.begins == 1                     # saved now: no more Loading


def test_dropped_chunk_at_full_speed_retries_at_the_safe_pace():
    lcd = FakeLcd(lose_chunk=True, mode=P.MODE_GIF)
    panel = _panel(lcd)
    upload = P.gif_upload([bytes(range(256)) * (P.FRAME_BYTES // 256)] * 3, 100)
    panel.upload(upload)                                            # fast first, then GCC's pace
    headers = [w for w in lcd.writes if w[0] == P.OP_UPLOAD_HEADER]
    gaps = [b - a for a, b in zip(lcd.chunk_times, lcd.chunk_times[1:])]
    assert len(headers) == 2 and panel.erases == 2 and lcd.bank == 2 and _clean(lcd)
    assert max(gaps) >= P.PACE_CHUNK and min(gaps) < P.PACE_CHUNK


@pytest.mark.parametrize("size", [P.GIF_MIN_PAYLOAD, 700_000, P.GIF_MAX_PAYLOAD])
@pytest.mark.parametrize("rate", [0.02, 0.027, 0.04])
def test_data_starts_after_the_erase_and_before_the_panel_gives_up(size, rate):
    """Measured: ~30 ms per sector. The wait must suit faster and slower panels too."""
    upload = P.Upload("gif", P.FB_GIF, P.MODE_GIF, bytes(size), flag=2, nframes=2, delay_ms=33)
    lcd = FakeLcd(erase_rate=rate)
    _panel(lcd).upload(upload)
    assert lcd.data == len(upload.frames) - 3 and _clean(lcd) and lcd.bank == 2


def test_upload_never_streams_unless_an_upload_is_open():
    """A chunk with no upload open would run as a command."""
    upload = P.gif_upload([bytes(P.FRAME_BYTES)] * 2, 100)
    for lcd, error in ((FakeLcd(ignore_header=True), "did not open"),
                       (FakeLcd(ignore_modes=True), "does not stay in mode")):
        with pytest.raises(OSError, match=error):
            _panel(lcd).upload(upload)
        assert lcd.data == 0 and _clean(lcd), error
    lcd = FakeLcd(lose_chunk=True)
    with pytest.raises(OSError, match="lost part"):                 # already at the safe pace
        _panel(lcd).upload(upload, chunk_delay=P.PACE_CHUNK)
    assert P.OP_UPLOAD_MARK not in lcd.opcodes()[-2:] and lcd.bank == 1 and _clean(lcd)


def test_bus_lock_is_exclusive_and_reentrant():
    a, b = BusLock("aorus-lcd-test-lock", timeout=0.1), BusLock("aorus-lcd-test-lock", timeout=0.1)
    with a, a:
        with pytest.raises(OSError):
            b.__enter__()
    with b:
        pass


def test_config_parses_and_rejects_typos(tmp_path):
    cfg = C.parse({"content": {"type": "logo", "source": "x.svg", "color": "#f00"},
                   "overlay": {"enabled": True, "widgets": ["temp", "power"]}})
    assert cfg.content.scale == 0.8 and cfg.overlay.widgets == ["temp", "power"]
    with pytest.raises(ValueError, match="unknown keys"):
        C.parse({"overlay": {"widget": ["temp"]}})
    with pytest.raises(ValueError, match="needs content.source"):
        C.parse({"content": {"type": "logo"}})
    with pytest.raises(ValueError, match="unknown"):
        C.parse({"overlay": {"widgets": ["gpu_fps"]}})


def test_example_config_is_valid():
    import pathlib
    C.load(pathlib.Path(__file__).parent.parent / "examples" / "config.toml")


def test_feeder_deadband_and_refresh():
    now = [0.0]
    f = CTL.Feeder({"temp": 2, "power": 5}, refresh_seconds=30, clock=lambda: now[0])
    s = P.Sample(temp=40, power=100)
    assert f.should_send(s)
    f.sent(s)
    assert not f.should_send(P.Sample(temp=41, power=103))
    assert f.should_send(P.Sample(temp=42, power=100))
    now[0] = 31
    assert f.should_send(s)


def _ctl(tmp_path, data):
    ctl = CTL.Controller(C.parse(data), tmp_path)
    lcd = FakeLcd()
    ctl.attach(_panel(lcd))
    return ctl, lcd


def test_apply_content_skips_unchanged(tmp_path):
    ctl, lcd = _ctl(tmp_path, {"content": {"type": "text", "text": "hi"}})
    assert ctl._apply_content()
    uploads = lcd.opcodes().count(0xF1)
    assert not ctl._apply_content()
    assert lcd.opcodes().count(0xF1) == uploads
    assert ctl._apply_content(force=True)


def test_apply_overlay_sends_template_then_widgets():
    lcd = FakeLcd()
    cfg = C.parse({"overlay": {"enabled": True, "widgets": ["temp"], "color": "#ff0000"}})
    CTL.apply_overlay(_panel(lcd), cfg)
    assert lcd.opcodes() == [P.OP_SET_TEMPLATE, P.OP_SET_TEMPLATE, P.OP_SET_DISPLAY]
    assert {w[5] for w in lcd.writes[:2]} == {P.TEMPLATE_IMAGE, P.TEMPLATE_GIF}
    assert list(lcd.writes[0][6:9]) == [255, 0, 0]




def test_uploads_whose_erase_wait_outlasts_the_panels_patience_are_refused():
    """After ~25 s without a sensor feed the panel goes back to its own
    slideshow, even while it erases for an upload; that hangs it."""
    assert P.header_pause(P.GIF_MAX_PAYLOAD, P.ERASE_SECTOR) <= P.HOST_SILENCE_SECONDS - 4
    big = P.Upload("gif", P.FB_GIF, P.MODE_GIF, bytes(3_000_000), flag=2, nframes=2, delay_ms=33)
    lcd = FakeLcd()
    with pytest.raises(ValueError, match="gives up on the PC"):
        _panel(lcd).upload(big)
    assert lcd.writes == []
    lcd = FakeLcd()
    _panel(lcd).upload(P.gif_upload([bytes(P.FRAME_BYTES)] * 2, 100))
    assert lcd.opcodes()[0] == P.OP_SENSOR_FEED                   # a sign of life before the silence
