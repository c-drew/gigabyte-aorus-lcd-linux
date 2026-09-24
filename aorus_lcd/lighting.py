"""GPU RGB lighting: Gigabyte RGB Fusion 2 "Blackwell" controller at 0x75.

Same I2C port as the LCD. Protocol from OpenRGB's
GigabyteRGBFusion2BlackwellGPUController, as ported and validated on an RTX
5090 Master by CodeTorchAI/AorusLcd: one 64-byte packet per zone, then a save
packet that persists the setting in the card (so it survives reboots).

The controller is write-only: never read from it (that wedges the bus until
a power-off), and only write at 400 kHz (nvrm transport).
"""
from dataclasses import dataclass
import time

from .transport import RGB_ADDRESS

PACKET = 64
REG_MODE, REG_SAVE, REG_COLOR = 0x12, 0x13, 0x16
ZONES = 6                     # AORUS 5090/5080 Master "gaming" layout
COLOR_OFFSET = 11
MODES = {"off": 0x01, "static": 0x01, "breathing": 0x02, "flashing": 0x03, "dual-flashing": 0x04,
         "color-cycle": 0x05, "wave": 0x06, "gradient": 0x07, "color-shift": 0x08,
         "tricolor": 0x09, "dazzle": 0x0A}
MULTI_COLOR = {"color-shift", "tricolor", "dazzle"}


@dataclass
class Lighting:
    mode: str = "static"
    colors: tuple = ((255, 0, 0),)
    brightness: int = 5          # 1..10
    speed: int = 3               # 1 (slowest) .. 6


def zone_packet(zone, light):
    if light.mode not in MODES:
        raise ValueError(f"unknown lighting mode {light.mode!r}; choose from {', '.join(MODES)}")
    colors = [(0, 0, 0)] if light.mode == "off" else list(light.colors) or [(0, 0, 0)]
    brightness = 1 if light.mode == "off" else max(1, min(10, light.brightness))
    if light.mode == "breathing":
        brightness = 10                                  # hardware ignores it; OpenRGB forces max
    extra = colors[:(PACKET - COLOR_OFFSET) // 3] if light.mode in MULTI_COLOR else []
    p = bytearray(PACKET)
    p[0:11] = bytes([REG_MODE, 0x01, MODES[light.mode], max(1, min(6, light.speed)), brightness,
                     *colors[0], 0x00, zone, len(extra)])
    for i, rgb in enumerate(extra):
        p[COLOR_OFFSET + 3 * i:COLOR_OFFSET + 3 * i + 3] = bytes(rgb)
    return bytes(p)


def save_packet():
    return bytes([REG_SAVE, 0x01]) + bytes(PACKET - 2)


def packets(light, save=True):
    return [zone_packet(z, light) for z in range(ZONES)] + ([save_packet()] if save else [])


class GpuRgb:
    """Writes lighting packets with the pacing the controller needs (it NAKs
    back-to-back writes)."""

    def __init__(self, transport, lock, sleep=time.sleep, delay=0.02):
        self.t, self.lock, self.sleep, self.delay = transport, lock, sleep, delay

    def apply(self, light, save=True):
        with self.lock:
            for packet in packets(light, save):
                for attempt in range(3):
                    try:
                        self.t.write(RGB_ADDRESS, packet)
                        break
                    except OSError:
                        if attempt == 2:
                            raise
                        self.sleep(self.delay * 2)
                self.sleep(self.delay)
