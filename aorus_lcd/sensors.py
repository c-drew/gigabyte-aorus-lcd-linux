"""GPU readings for the overlay, from NVML (libnvidia-ml, ships with the driver)."""
import ctypes

from .protocol import Sample

NVML_SUCCESS = 0
TEMPERATURE_GPU = 0
CLOCK_GRAPHICS, CLOCK_MEM = 0, 2


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _FanSpeedInfo(ctypes.Structure):   # nvmlFanSpeedInfo_t (v1)
    _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]


class NvmlSensors:
    """Reads one GPU by PCI address. Every field degrades to 0 if unavailable."""

    def __init__(self, pci):
        try:
            self.lib = ctypes.CDLL("libnvidia-ml.so.1")
        except OSError as e:
            raise RuntimeError("libnvidia-ml.so.1 not found (NVIDIA driver utilities)") from e
        if self.lib.nvmlInit_v2() != NVML_SUCCESS:
            raise RuntimeError("nvmlInit failed")
        self.handle = ctypes.c_void_p()
        bus_id = pci if pci.count(":") == 2 else f"0000:{pci}"
        if self.lib.nvmlDeviceGetHandleByPciBusId_v2(bus_id.encode(), ctypes.byref(self.handle)):
            raise RuntimeError(f"NVML has no GPU at {pci}")
        self._rpm = hasattr(self.lib, "nvmlDeviceGetFanSpeedRPM")

    def _uint(self, fn, *args):
        v = ctypes.c_uint()
        return v.value if getattr(self.lib, fn)(self.handle, *args, ctypes.byref(v)) == NVML_SUCCESS else 0

    def fan(self):
        if self._rpm:
            info = _FanSpeedInfo(version=ctypes.sizeof(_FanSpeedInfo) | (1 << 24), fan=0)
            if self.lib.nvmlDeviceGetFanSpeedRPM(self.handle, ctypes.byref(info)) == NVML_SUCCESS:
                return info.speed
            self._rpm = False
        return self._uint("nvmlDeviceGetFanSpeed")

    def read(self):
        util, mem = _Utilization(), _Memory()
        usage = util.gpu if self.lib.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(util)) == 0 else 0
        vram = (100 * mem.used / mem.total
                if self.lib.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(mem)) == 0 and mem.total else 0)
        return Sample(
            temp=self._uint("nvmlDeviceGetTemperature", TEMPERATURE_GPU),
            clock=self._uint("nvmlDeviceGetClockInfo", CLOCK_GRAPHICS),
            usage=usage,
            fan=self.fan(),
            vram_clock=self._uint("nvmlDeviceGetClockInfo", CLOCK_MEM),
            vram=round(vram),
            fps=0,                                   # NVML has no frame rate
            power=round(self._uint("nvmlDeviceGetPowerUsage") / 1000),
        )

    def close(self):
        self.lib.nvmlShutdown()
