"""Rendering to RGB565 and the pure parts of the transports."""
import ctypes

import pytest

from aorus_lcd import protocol as P
from aorus_lcd import render as R
from aorus_lcd.transport import gpu as G
from aorus_lcd.transport import i2cdev, nvrm

PIL = pytest.importorskip("PIL.Image")


def _solid(rgb):
    return PIL.new("RGB", (P.W, P.H), rgb)


def test_rgb565_known_pixels():
    red = R.to_rgb565(_solid((255, 0, 0)))
    assert len(red) == P.FRAME_BYTES and red == b"\x00\xf8" * P.FRAME_PIXELS
    assert R.to_rgb565(_solid((0, 255, 0)))[:2] == b"\xe0\x07"
    assert R.to_rgb565(_solid((0, 0, 255)))[:2] == b"\x1f\x00"
    assert R.to_rgb565(_solid((7, 3, 7)))[:2] == b"\x00\x00"


def test_rgb565_round_trip_preview():
    im = R.from_rgb565(R.to_rgb565(_solid((255, 0, 0))))
    assert im.getpixel((0, 0)) == (248, 0, 0)


def test_logo_is_recoloured_on_black(tmp_path):
    art = PIL.new("RGBA", (100, 40), (0, 0, 0, 0))
    art.paste((0, 0, 0, 255), (10, 10, 90, 30))
    path = tmp_path / "logo.png"
    art.save(path)
    im = R.logo(path, "#ff0000", scale=0.5)
    assert im.getpixel((0, 0)) == (0, 0, 0)
    assert im.getpixel((P.W // 2, P.H // 2)) == (255, 0, 0)


def test_opaque_logo_uses_brightness(tmp_path):
    art = PIL.new("RGB", (100, 40), (255, 255, 255))
    art.paste((0, 0, 0), (10, 10, 90, 30))
    path = tmp_path / "logo.png"
    art.save(path)
    im = R.logo(path, "#00ff00", scale=0.5)                  # dark shape on white -> inverted
    assert im.getpixel((P.W // 2, P.H // 2)) == (0, 255, 0)


def test_text_and_colors():
    im = R.text("HELLO", 28, "#ffffff")
    px = R.to_rgb565(im)
    assert b"\xff\xff" in px and px[:2] == b"\x00\x00"
    assert R.parse_color("f00") == (255, 0, 0) and R.parse_color("#00ff7f") == (0, 255, 127)


def test_gif_frames_and_pulse(tmp_path):
    frames = [PIL.new("RGB", (64, 64), (40 * i, 0, 0)) for i in range(4)]
    path = tmp_path / "t.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=50, loop=0)
    out, delay = R.gif_frames(path)
    assert len(out) == 4 and delay == 50 and out[0].size == (P.W, P.H)
    assert len(R.pulse(_solid((255, 0, 0)), 8)) == 8


def test_rm_struct_layouts_match_nvidia_headers():
    assert ctypes.sizeof(nvrm.I2cTransaction) == 96
    assert nvrm.I2cTransaction.transData.offset == 16
    assert ctypes.sizeof(nvrm.CardInfo) == 72 and nvrm.CardInfo.minor_number.offset == 56
    assert nvrm._ioc(nvrm.NV_ESC_RM_CONTROL, 32) == 0xC020462A


def test_rm_speed_flags():
    assert nvrm.SPEED_MODES[400] << 1 == 4 and nvrm.SPEED_MODES[100] == 0


def _fake_gpu_tree(tmp_path, subdevice="416e"):
    dev = tmp_path / "pci" / "0000:01:00.0"
    dev.mkdir(parents=True)
    for name, value in {"vendor": "0x10de", "class": "0x030000", "subsystem_vendor": "0x1458",
                        "subsystem_device": f"0x{subdevice}"}.items():
        (dev / name).write_text(value + "\n")
    proc = tmp_path / "proc" / "0000:01:00.0"
    proc.mkdir(parents=True)
    (proc / "information").write_text("Model: \t\t NVIDIA GeForce RTX 5090\nDevice Minor: \t 0\n")
    return {"sys_root": tmp_path / "pci", "proc_root": tmp_path / "proc"}


def test_find_gpu(tmp_path):
    gpu = G.find_gpu(**_fake_gpu_tree(tmp_path))
    assert gpu.pci == "0000:01:00.0" and gpu.minor == 0 and gpu.verified
    assert "RTX 5090" in gpu.describe()


def test_find_adapter_by_name(tmp_path):
    root = tmp_path / "i2c-dev"
    for n, name in {2: "NVIDIA i2c adapter 1 at 1:00.0", 3: "NVIDIA i2c adapter 3 at 1:00.0",
                    7: "AMDGPU DM i2c hw bus 0"}.items():
        (root / f"i2c-{n}").mkdir(parents=True)
        (root / f"i2c-{n}" / "name").write_text(name + "\n")
    gpu = G.Gpu("0000:01:00.0", 0, 0x1458, 0x416E, "x")
    assert i2cdev.find_adapter(gpu, sys_root=str(root)) == 2
