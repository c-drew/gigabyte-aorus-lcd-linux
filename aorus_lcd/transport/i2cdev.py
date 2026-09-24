"""Legacy transport: the kernel's /dev/i2c-N "NVIDIA i2c adapter 1".

Fixed at 100 kHz by the driver. Some panels/firmware answer at that speed
(it is what albancreton/aorus-master-linux shipped with); prefer nvrm.
"""
import glob
import os

from .base import LCD_ADDRESS, Transport, TransportError

ADAPTER_PREFIX = "NVIDIA i2c adapter 1 at"


def find_adapter(gpu=None, sys_root="/sys/class/i2c-dev"):
    """/dev/i2c-N number of the GPU's internal port, matched by adapter name."""
    found = []
    for path in glob.glob(os.path.join(sys_root, "i2c-*")):
        try:
            name = open(os.path.join(path, "name")).read().strip()
        except OSError:
            continue
        if not name.startswith(ADAPTER_PREFIX):
            continue
        if gpu is not None:
            bus, dev_fn = gpu.pci.split(":")[1:]
            dev, fn = dev_fn.split(".")
            if not name.endswith(f" {int(bus, 16):x}:{dev}.{fn}"):
                continue
        found.append(int(path.rsplit("-", 1)[1]))
    if len(found) != 1:
        raise TransportError("could not find exactly one NVIDIA internal i2c adapter "
                             "(is i2c-dev loaded? sudo modprobe i2c-dev)")
    return found[0]


class I2cDevTransport(Transport):
    name = "i2c-dev"

    def __init__(self, bus_number):
        try:
            from smbus2 import SMBus, i2c_msg
        except ImportError as e:
            raise TransportError("the i2c-dev transport needs smbus2 (pip install smbus2)") from e
        self._msg = i2c_msg
        self.bus_number = bus_number
        self._bus = SMBus(bus_number)

    def write(self, address, data):
        self.check(address, len(data))
        try:
            self._bus.i2c_rdwr(self._msg.write(address, bytes(data)))
        except OSError as e:
            raise TransportError(f"write to {address:#04x} on i2c-{self.bus_number}: {e}") from e

    def read(self, address, length):
        self.check(address, length)
        if address != LCD_ADDRESS:
            raise ValueError("bootloader access needs the nvrm transport")
        msg = self._msg.read(address, length)
        try:
            self._bus.i2c_rdwr(msg)
        except OSError as e:
            raise TransportError(f"read from {address:#04x} on i2c-{self.bus_number}: {e}") from e
        return bytes(msg)

    def ping(self, address):
        # The NVIDIA adapter rejects zero-length writes, so presence can only be
        # shown by a real command/response exchange (Panel.probe does that).
        raise TransportError("i2c-dev cannot ping; use Panel.probe()")

    def close(self):
        self._bus.close()
