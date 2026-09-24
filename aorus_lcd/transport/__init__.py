"""Bus transports and a factory that picks the GPU and opens one."""
from .base import (ALLOWED_ADDRESSES, BOOTLOADER_ADDRESSES, LCD_ADDRESS, RGB_ADDRESS,
                   Transport, TransportError)
from .gpu import Gpu, find_gpu, list_gpus

TRANSPORTS = ("nvrm", "i2c-dev")


def open_transport(kind="nvrm", gpu=None, speed_khz=400, i2c_bus=None):
    """Open a transport to the LCD on `gpu` (a Gpu, or None to autodetect)."""
    gpu = gpu or find_gpu()
    if kind == "nvrm":
        from .nvrm import NvRmTransport
        return NvRmTransport(gpu, speed_khz)
    if kind == "i2c-dev":
        from .i2cdev import I2cDevTransport, find_adapter
        return I2cDevTransport(i2c_bus if i2c_bus is not None else find_adapter(gpu))
    raise ValueError(f"unknown transport {kind!r} (choose from {', '.join(TRANSPORTS)})")


__all__ = ["ALLOWED_ADDRESSES", "BOOTLOADER_ADDRESSES", "LCD_ADDRESS", "RGB_ADDRESS", "Gpu",
           "Transport", "TransportError", "TRANSPORTS", "find_gpu", "list_gpus", "open_transport"]
