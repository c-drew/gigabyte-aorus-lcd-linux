"""Panel sequencing, config parsing and the daemon's decisions, on a fake bus."""
import pytest

from aorus_lcd import config as C
from aorus_lcd import controller as CTL
from aorus_lcd import protocol as P
from aorus_lcd.panel import BusLock, Panel
from aorus_lcd.transport import Transport


class FakeLcd(Transport):
    """Answers queries like the firmware: 4-byte replies, mode tracking."""

    def __init__(self):
        self.writes, self.mode = [], P.MODE_TEXT

    def write(self, address, data):
        self.check(address, len(data))
        assert len(data) == 256, "the firmware only parses full 256-byte frames"
        self.writes.append(bytes(data))
        if data[0] == P.OP_SET_MODE:
            self.mode = data[5] - 1

    def read(self, address, length):
        assert length == 4
        op = self.writes[-1][0]
        return bytes([op, self.mode + 1, 1, 1]) if op == P.OP_GET_MODE else bytes([op, 0, 0, 1])

    def ping(self, address):
        return True

    def opcodes(self):
        return [w[0] for w in self.writes]


def _panel(lcd):
    return Panel(lcd, lock=BusLock("aorus-lcd-test"), sleep=lambda s: None)


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
    assert ops[:2] == [0xF2, 0xF1] and ops[ops.index(0xF2, 1)] == 0xF2
    assert lcd.mode == P.MODE_IMAGE


def test_reselecting_the_same_mode_nudges_through_another():
    lcd = FakeLcd()
    lcd.mode = P.MODE_IMAGE
    _panel(lcd).show(P.MODE_IMAGE)
    modes = [w[5] - 1 for w in lcd.writes if w[0] == P.OP_SET_MODE]
    assert modes == [P.MODE_CHIBI, P.MODE_IMAGE]


def test_gif_sets_mode_before_streaming():
    lcd = FakeLcd()
    _panel(lcd).upload(P.gif_upload([bytes(P.FRAME_BYTES)] * 2, 100))
    assert lcd.opcodes()[0] == P.OP_SET_MODE and lcd.opcodes()[1] == 0xF2


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
