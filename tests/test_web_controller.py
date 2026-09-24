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
from aorus_lcd.transport import RGB_ADDRESS, Transport
from aorus_lcd.web import serve


class FakeBus(Transport):
    def __init__(self):
        self.lcd, self.rgb, self.mode = [], [], P.MODE_IMAGE

    def write(self, address, data):
        self.check(address, len(data))
        if address == RGB_ADDRESS:
            self.rgb.append(bytes(data))
            return
        assert len(data) == 256
        self.lcd.append(bytes(data))
        if data[0] == P.OP_SET_MODE:
            self.mode = data[5] - 1

    def read(self, address, length):
        self.check(address, length, write=False)
        op = self.lcd[-1][0]
        if op == P.OP_GET_MODE:
            return bytes([op, self.mode + 1, 1, 1])
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
