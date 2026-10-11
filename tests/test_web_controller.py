"""Controller (overrides, presets, lighting) and the local web UI, on a fake bus."""
import http.client
import io
import json
import time

import pytest

from aorus_lcd import config as C
from aorus_lcd import lighting as L
from aorus_lcd import protocol as P
from aorus_lcd.controller import Controller
from aorus_lcd.panel import BusLock, Panel
from aorus_lcd.transport import RGB_ADDRESS, Transport, TransportError
from aorus_lcd.web import serve


class FakeBus(Transport):
    def __init__(self):
        self.lcd, self.rgb, self.mode, self.uploading, self.bank = [], [], P.MODE_IMAGE, False, 1
        self.left, self.slideshow, self.questions, self.delay = 0, False, 0, 0.0
        self.fail_after = None

    def write(self, address, data):
        self.check(address, len(data))
        if address == RGB_ADDRESS:
            self.rgb.append(bytes(data))
            return
        assert len(data) == 256
        if self.delay:
            time.sleep(self.delay)
        if self.fail_after is not None and self.uploading and data[1:5] != P.MAGIC:
            self.fail_after -= 1
            if self.fail_after < 0:
                raise TransportError("write 256 bytes to 0x61: I2C error (NACK or stalled bus)")
        self.lcd.append(bytes(data))
        if self.uploading:
            if data[1:5] != P.MAGIC:
                self.left -= 1
                self.uploading = self.left > 0         # closes itself after the last chunk
            else:
                self.questions += 1                    # the first comes during the erase;
                self.uploading = self.questions < 2    # one after it closes the upload
            return
        if data[0] == P.OP_UPLOAD_HEADER:
            self.uploading, self.left, self.questions = True, int.from_bytes(data[10:14], "big"), 0
        elif data[0] == P.OP_UPLOAD_MARK and data[5] == 2 and self.mode == P.MODE_GIF:
            self.bank = 3 - self.bank
        elif data[0] == P.OP_SENSOR_FEED:
            self.slideshow = False
        elif data[0] == P.OP_SET_MODE and not self.slideshow:   # the slideshow reverts it
            self.mode = data[5] - 1

    def read(self, address, length):
        self.check(address, length, write=False)
        if self.uploading:
            return bytes([P.OP_GET_FW, 0x13, 0x01, 0x02])
        op = self.lcd[-1][0]
        if op == P.OP_GET_BANK:
            return bytes([op, self.bank, 1, 2])
        if op == P.OP_GET_MODE:
            return bytes([op, (P.MODE_SLIDESHOW if self.slideshow else self.mode) + 1, 1, 1])
        if op == P.OP_GET_FW:
            return bytes([op, 0x13, 1, 2])
        return bytes([op, 0, 0, 1])

    def ping(self, address):
        return True


def make(tmp_path, data=None, digest=None):
    ctl = Controller(C.parse(data or {}), tmp_path, digest)
    bus = FakeBus()
    ctl.attach(Panel(bus, lock=BusLock("aorus-lcd-test-web"), sleep=lambda s: None))
    return ctl, bus


def test_overrides_persist_and_config_changes_win(tmp_path):
    ctl, _ = make(tmp_path, digest="v1")
    ctl.set_overlay({"enabled": True, "widgets": ["temp"]})
    assert Controller(C.parse({}), tmp_path, "v1").cfg.overlay.widgets == ["temp"]
    assert Controller(C.parse({}), tmp_path, "v2").cfg.overlay.enabled is False


def test_invalid_override_is_rejected_and_nothing_saved(tmp_path):
    ctl, _ = make(tmp_path)
    with pytest.raises(ValueError):
        ctl.set_overlay({"widgets": ["nope"]})
    assert "overrides" not in json.loads((tmp_path / "state.json").read_text() if (tmp_path / "state.json").exists() else "{}")


def test_lighting_writes_zones_then_save_and_is_not_repeated(tmp_path):
    ctl, bus = make(tmp_path)
    ctl.set_lighting({"enabled": True, "mode": "static", "colors": ["#ff0000"], "brightness": 5})
    assert bus.rgb == L.packets(L.Lighting("static", ((255, 0, 0),), 5, 3))
    assert bus.rgb[-1][:2] == bytes([0x13, 0x01])
    ctl.apply_all()
    assert len(bus.rgb) == 7, "unchanged lighting is not rewritten at boot"


def test_lighting_disabled_by_default_never_touches_rgb(tmp_path):
    ctl, bus = make(tmp_path, {"content": {"type": "text", "text": "hi"}})
    ctl.apply_all()
    assert bus.rgb == []


def test_presets_round_trip(tmp_path):
    ctl, bus = make(tmp_path)
    ctl.set_overlay({"enabled": True, "widgets": ["power"]})
    ctl.save_preset("gaming")
    ctl.set_overlay({"widgets": ["temp"]})
    ctl.apply_preset("gaming")
    assert ctl.cfg.overlay.widgets == ["power"]
    ctl.delete_preset("gaming")
    assert ctl.presets() == {}


def _wait_job(ctl):
    for _ in range(200):
        if ctl.job["state"] != "running":
            return ctl.job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.fixture
def web(tmp_path):
    ctl, bus = make(tmp_path)
    server = serve(ctl, "127.0.0.1", 0)
    port = server.server_address[1]

    def request(method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body, h)
        r = conn.getresponse()
        return r.status, r.getheader("Content-Type"), r.read()

    yield ctl, bus, request
    server.shutdown()


API = {"X-Aorus-Lcd": "1"}


def test_web_serves_page_and_status(web):
    ctl, bus, req = web
    code, mime, body = req("GET", "/")
    assert code == 200 and mime.startswith("text/html") and b"AORUS" in body
    code, _, body = req("GET", "/api/status")
    assert code == 200 and json.loads(body)["panel"]["firmware"] == "1.3"


def test_web_rejects_foreign_hosts_and_missing_header(web):
    ctl, bus, req = web
    assert req("GET", "/api/status", host="evil.example:80")[0] == 403
    assert req("POST", "/api/power", {"on": False})[0] == 403
    assert req("POST", "/api/power", {"on": False}, API, host="evil.example")[0] == 403
    assert not any(w[0] == P.OP_OPEN_LCD for w in bus.lcd)


def test_web_preview_upload_and_errors(web, tmp_path):
    ctl, bus, req = web
    code, mime, body = req("POST", "/api/preview", {"content": {"type": "text", "text": "hi"}}, API)
    assert code == 200 and mime == "image/png" and body[:4] == b"\x89PNG"
    assert req("POST", "/api/overlay", {"overlay": {"widgets": ["bogus"]}}, API)[0] == 400
    code, _, _ = req("POST", "/api/content", {"content": {"type": "text", "text": "hi"}}, API)
    assert code == 202 and _wait_job(ctl)["state"] == "done"
    assert any(w[0] == P.OP_UPLOAD_HEADER for w in bus.lcd)


def test_web_media_upload(web):
    from PIL import Image
    ctl, bus, req = web
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (255, 0, 0)).save(buf, "PNG")
    code, _, body = req("POST", "/api/media", buf.getvalue(), {**API, "X-Filename": "red.png"})
    info = json.loads(body)
    assert code == 200 and info["width"] == 40 and info["source"].endswith(".png")
    assert req("POST", "/api/media", b"not an image", {**API, "X-Filename": "x.png"})[0] == 400
    assert req("POST", "/api/media", b"#!/bin/sh", {**API, "X-Filename": "x.sh"})[0] == 400


def _playlist(tmp_path, fake_ttfx, **extra):
    return {"type": "ttfx", "ttfx_text": "  ##  \n ████ \n", "effects": ["fx"], "rotate_minutes": 15,
            "ttfx": fake_ttfx, "hold_ms": 100, **extra}


def test_screensaver_ignores_other_types_text_and_file(tmp_path, monkeypatch):
    """Switching from text or logo content leaves `text`/`source` behind."""
    from aorus_lcd import content as CT, ttfx
    logo = tmp_path / "logo.txt"
    logo.write_text("OMARCHY\n")
    monkeypatch.setattr(ttfx, "DEFAULT_TEXT", str(logo))
    c = C.parse({"content": {"type": "ttfx", "text": "BVILT\n4\nBWC", "source": "/etc/x.png"}}).content
    assert CT._ttfx_text(c) == "OMARCHY\n"
    c = C.parse({"content": {"type": "ttfx", "ttfx_text": "hi"}}).content
    assert CT._ttfx_text(c) == "hi"


def _wait_done(ctl):
    deadline = time.monotonic() + 20
    while ctl.job["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ctl.job["state"] == "done", ctl.job


def test_playlist_rotates_with_playback_held(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    assert ctl._rotation_active() and bus.mode == P.MODE_GIF
    bank, uploads, writes = bus.bank, ctl.rotation.wear["uploads"], len(bus.lcd)
    ctl.rotation.state["switched_at"] = 0                          # due now
    ctl._maybe_rotate()
    ctl._bg_thread.join(20)
    assert ctl.bg["phase"] == "idle", ctl.bg
    assert bus.bank == 3 - bank and ctl.rotation.wear["uploads"] == uploads + 1
    ops = [w[0] for w in bus.lcd[writes:] if w[1:5] == P.MAGIC]
    assert P.OP_GIF_HOLD in ops                                     # held while it is written
    assert P.OP_UPLOAD_MARK in ops and "flash_unsure" not in ctl.state
    assert ctl.rotation.state["switched_at"] > 0
    status = ctl.status()["screensaver"]
    assert status["rotating"] and status["wear"]["uploads"] == uploads + 1
    assert status["wear"]["years_left"] > 5 and status["effect"] == "fx"


def test_editing_the_playlist_does_not_upload(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    writes = len(bus.lcd)
    ctl.start_content(_playlist(tmp_path, fake_ttfx, effects=["fx", "big"], rotate_minutes=60, shuffle=True))
    assert ctl.job["label"] == "playlist saved" and len(bus.lcd) == writes
    assert ctl.cfg.content.rotate_minutes == 60 and ctl._rotation_active()
    ctl.start_content(_playlist(tmp_path, fake_ttfx, border=[4, 2, 4, 2]))   # the loops look different
    _wait_done(ctl)
    assert len(bus.lcd) > writes


def test_boot_rotation_swaps_once_per_boot():
    from aorus_lcd import playlist
    c = C.parse({"content": {"type": "ttfx", "rotate_minutes": 15, "rotate_on_boot": True}}).content
    r = playlist.Rotation({})
    boot = 1_000_000.0
    r.state["switched_at"] = boot - 3600                           # swapped during the last boot
    assert r.due_at(c, booted_at=boot) == boot
    r.switched(1, "fx", now=boot + 120)                            # swapped this boot
    assert r.due_at(c, booted_at=boot) is None
    assert r.due_at(c, booted_at=boot + 7200) == boot + 7200
    r.state["switched_at"] = boot - 60                             # a quick reboot: skipped
    assert r.due_at(c, booted_at=boot) is None
    with pytest.raises(ValueError, match="rotate_on_boot"):
        C.parse({"content": {"type": "ttfx", "rotate_on_boot": True}})


def test_boot_rotation_waits_for_the_next_boot(tmp_path, fake_ttfx, monkeypatch):
    from aorus_lcd import playlist
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    writes = len(bus.lcd)
    ctl.start_content(_playlist(tmp_path, fake_ttfx, rotate_on_boot=True))
    assert ctl.job["label"] == "playlist saved" and len(bus.lcd) == writes
    booted = ctl.rotation.state["switched_at"] - 3600              # it swapped since this boot
    monkeypatch.setattr(playlist, "boot_time", lambda: booted)
    ctl._maybe_rotate()
    assert ctl._bg_thread is None and len(bus.lcd) == writes
    status = ctl.status()["screensaver"]
    assert status["on_boot"] and status["due_at"] is None
    ctl.rotation.state["switched_at"] = time.time() - 3600         # then the PC restarts
    monkeypatch.setattr(playlist, "boot_time", lambda: time.time() - 60)
    ctl._maybe_rotate()
    ctl._bg_thread.join(20)
    assert ctl.bg["phase"] == "idle", ctl.bg
    assert len(bus.lcd) > writes and ctl.rotation.state["switched_at"] > time.time() - 60
    assert ctl.status()["screensaver"]["due_at"] is None           # once per boot


def test_changes_during_a_background_upload_wait_for_it(tmp_path):
    ctl, bus = make(tmp_path)
    ctl.bg["phase"] = "uploading"                                  # as if a rotation were streaming
    writes = len(bus.lcd)
    ctl.set_overlay({"enabled": True, "widgets": ["temp"]})
    ctl.set_power(False)
    assert len(bus.lcd) == writes, "nothing may reach the panel mid-upload"
    assert ctl.tick() == 1.0 and len(bus.lcd) == writes
    ctl.bg["phase"] = "idle"
    ctl.tick()
    ops = [w[0] for w in bus.lcd[writes:]]
    assert P.OP_SET_DISPLAY in ops and P.OP_OPEN_LCD in ops and not ctl._pending


def test_rotation_is_limited_to_protect_the_flash(tmp_path):
    with pytest.raises(ValueError, match="wears"):
        C.parse({"content": {"type": "ttfx", "rotate_minutes": 5}})


def test_a_panel_that_reset_to_its_slideshow_gets_its_content_back(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    writes = len(bus.lcd)
    bus.slideshow = True                                           # a reset
    ctl._info_at, ctl._fed_at = 0, time.monotonic()                # (no heartbeat due yet)
    ctl.tick()
    assert not bus.slideshow and bus.mode == P.MODE_GIF
    assert ctl.panel_info["mode"] == P.MODE_GIF
    ops = [w[0] for w in bus.lcd[writes:]]
    assert P.OP_UPLOAD_HEADER not in ops                           # no upload: the flash kept it
    modes = len([w for w in bus.lcd if w[0] == P.OP_SET_MODE])
    bus.slideshow = True                                           # again at once: not re-selected
    ctl._info_at, ctl._fed_at = 0, time.monotonic()                # within the minute
    ctl.tick()
    assert len([w for w in bus.lcd if w[0] == P.OP_SET_MODE]) == modes

def test_a_restart_after_a_reset_does_not_upload_again(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    writes = len(bus.lcd)
    bus.slideshow = True
    ctl.apply_all()
    assert not bus.slideshow and bus.mode == P.MODE_GIF
    assert P.OP_UPLOAD_HEADER not in [w[0] for w in bus.lcd[writes:]]


def test_sending_the_same_screensaver_again_uploads_it_again(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    content = _playlist(tmp_path, fake_ttfx)
    ctl.start_content(content)
    _wait_done(ctl)
    writes = len(bus.lcd)
    ctl.start_content(content)                                     # "Send to panel" once more
    _wait_done(ctl)
    assert P.OP_UPLOAD_HEADER in [w[0] for w in bus.lcd[writes:]]


def test_a_reset_during_an_upload_uploads_the_content_again(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    assert "flash_unsure" not in ctl.state
    ctl.state["flash_unsure"] = True                               # it crashed mid-upload
    writes = len(bus.lcd)
    bus.slideshow = True
    ctl._info_at = 0
    ctl.tick()
    _wait_done(ctl)
    assert P.OP_UPLOAD_HEADER in [w[0] for w in bus.lcd[writes:]]
    assert bus.mode == P.MODE_GIF and "flash_unsure" not in ctl.state
    writes = len(bus.lcd)
    bus.slideshow = True                                           # at startup, too
    ctl.state["flash_unsure"] = True
    ctl.apply_all()
    assert P.OP_UPLOAD_HEADER in [w[0] for w in bus.lcd[writes:]]


def _wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert condition()


def test_stopping_closes_a_playlist_upload_first(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    bank, bus.delay = bus.bank, 0.002
    ctl.rotation.state["switched_at"] = 0
    ctl._maybe_rotate()
    _wait_for(lambda: ctl.bg["done"] > 5)
    ctl.detach(reason="stopping")
    assert not ctl._bg_thread.is_alive()
    assert not bus.uploading and bus.bank == bank                  # closed; nothing switched
    assert ctl.state["flash_unsure"] == "foreground"               # held: written again next start
    with pytest.raises(OSError, match="stopping"):
        ctl.set_power(True)


def test_a_foreground_upload_cut_off_by_a_stop_is_redone(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    content = _playlist(tmp_path, fake_ttfx)
    ctl.start_content(content)
    _wait_done(ctl)
    bus.delay = 0.002
    ctl.start_content(content)
    _wait_for(lambda: ctl.job["done"] > 5)
    ctl.detach()
    assert not bus.uploading and ctl.job["state"] == "error"
    assert ctl.state["flash_unsure"] == "foreground"
    bus.delay, writes = 0.0, len(bus.lcd)
    ctl.attach(Panel(bus, lock=BusLock("aorus-lcd-test-web"), sleep=lambda s: None))
    ctl.apply_all()                                                # what the service does next
    assert P.OP_UPLOAD_HEADER in [w[0] for w in bus.lcd[writes:]]
    assert "flash_unsure" not in ctl.state


def test_a_failed_playlist_upload_is_redone(tmp_path, fake_ttfx):
    ctl, bus = make(tmp_path)
    ctl.start_content(_playlist(tmp_path, fake_ttfx))
    _wait_done(ctl)
    bus.fail_after = 5                                             # the bus fails mid-stream
    ctl.rotation.state["switched_at"] = 0
    ctl._maybe_rotate()
    ctl._bg_thread.join(20)
    assert ctl.bg["phase"] == "error" and ctl.state["flash_unsure"] == "foreground"
    writes = len(bus.lcd)
    assert ctl.tick() == 1.0 and len(bus.lcd) == writes            # hands off while it closes
    assert 30 <= ctl.status()["settling_seconds"] <= 40
    with pytest.raises(Exception, match="try again"):
        ctl.start_content(_playlist(tmp_path, fake_ttfx))
    bus.fail_after = None
    ctl._quiet_until = 0
    ctl.rotation.state["retry_at"] = 0                              # the settle period is over
    ctl.tick()
    _wait_done(ctl)
    ops = [w[0] for w in bus.lcd[writes:]]
    assert P.OP_GIF_HOLD in ops and P.OP_UPLOAD_HEADER in ops       # the foreground way
    assert "flash_unsure" not in ctl.state


def test_progress_reports_the_time_left(tmp_path):
    from aorus_lcd import controller as K
    record = {"phase": "uploading"}
    K._progress(record, 1, 1000)
    record["since"] -= 10                                           # 10 s ago
    K._progress(record, 101, 1000)
    eta = K._with_eta(record)["eta_seconds"]
    assert "since" not in K._with_eta(record) and 85 <= eta <= 95


def test_a_heartbeat_keeps_the_panel_from_its_own_slideshow(tmp_path):
    """Without a sensor feed for ~25 s the panel plays its own slideshow, overlay
    or not: the service sends one every few seconds (it draws nothing)."""
    ctl, bus = make(tmp_path)
    assert not ctl.cfg.overlay.enabled
    feeds = lambda: [w for w in bus.lcd if w[0] == P.OP_SENSOR_FEED and w[1:5] == P.MAGIC]
    ctl.tick()
    assert len(feeds()) == 1
    ctl.tick()
    assert len(feeds()) == 1                                       # not again at once
    ctl._fed_at -= ctl.HEARTBEAT_SECONDS
    ctl.tick()
    assert len(feeds()) == 2
    ctl.bg["phase"], ctl._fed_at = "uploading", 0.0                # never during an upload
    ctl.tick()
    assert len(feeds()) == 2
