"""I2C through the NVIDIA resource manager (RM), in pure Python.

Why not /dev/i2c-N: the kernel's "NVIDIA i2c adapter" sends every transfer
with NV402C flags = 0, i.e. a hard-wired 100 kHz, and holds the GPU lock the
whole time. Gigabyte's software asks for 400 kHz per transaction through
NVAPI. NV402C_CTRL_CMD_I2C_TRANSACTION takes the speed per transaction and is
exported to unprivileged clients, so opening /dev/nvidiactl the way the NVIDIA
userspace libraries do gets the same behaviour with no root and no kernel
changes.

Object chain: root client -> NV01_DEVICE_0 -> NV20_SUBDEVICE_0 -> NV40_I2C.
Struct layouts follow NVIDIA's open-gpu-kernel-modules SDK headers (x86-64);
the size asserts below pin them.
"""
import ctypes
import errno
import fcntl
import os

from .base import Transport, TransportError

NV_IOCTL_MAGIC = ord("F")
NV_ESC_CARD_INFO = 200
NV_ESC_REGISTER_FD = 201
NV_ESC_RM_FREE = 0x29
NV_ESC_RM_CONTROL = 0x2A
NV_ESC_RM_ALLOC = 0x2B

NV01_ROOT_CLIENT = 0x41
NV01_DEVICE_0 = 0x80
NV20_SUBDEVICE_0 = 0x2080
NV40_I2C = 0x402C
NV0000_CTRL_CMD_GPU_GET_ID_INFO_V2 = 0x205
NV402C_CTRL_CMD_I2C_TRANSACTION = 0x402C0105
I2C_BLOCK_RW = 2          # NV402C_CTRL_I2C_TRANSACTION_TYPE_I2C_BLOCK_RW
SMBUS_QUICK_RW = 0
FLAG_PING = 1 << 10       # NV402C_CTRL_I2C_FLAGS_TRANSACTION_MODE_PING

# NV402C_CTRL_I2C_FLAGS_SPEED_MODE values (bits 4:1)
SPEED_MODES = {3: 5, 10: 4, 33: 3, 100: 0, 200: 1, 300: 7, 400: 2}
INTERNAL_PORT = 1

NV_STATUS = {0x14: "I2C error (NACK or stalled bus)", 0x56: "not supported",
             0x65: "timeout", 0x1F: "invalid argument", 0xFFFF: "generic error"}

u8, u16, u32, i32, u64 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint64


class NVOS21(ctypes.Structure):          # NV_ESC_RM_ALLOC
    _fields_ = [("hRoot", u32), ("hObjectParent", u32), ("hObjectNew", u32), ("hClass", u32),
                ("pAllocParms", u64), ("paramsSize", u32), ("status", u32)]


class NVOS00(ctypes.Structure):          # NV_ESC_RM_FREE
    _fields_ = [("hRoot", u32), ("hObjectParent", u32), ("hObjectOld", u32), ("status", u32)]


class NVOS54(ctypes.Structure):          # NV_ESC_RM_CONTROL
    _fields_ = [("hClient", u32), ("hObject", u32), ("cmd", u32), ("flags", u32),
                ("params", u64), ("paramsSize", u32), ("status", u32)]


class DeviceAlloc(ctypes.Structure):     # NV0080_ALLOC_PARAMETERS
    _fields_ = [("deviceId", u32), ("hClientShare", u32), ("hTargetClient", u32),
                ("hTargetDevice", u32), ("flags", u32), ("vaSpaceSize", u64),
                ("vaStartInternal", u64), ("vaLimitInternal", u64), ("vaMode", u32)]


class SubdeviceAlloc(ctypes.Structure):  # NV2080_ALLOC_PARAMETERS
    _fields_ = [("subDeviceId", u32)]


class PciInfo(ctypes.Structure):         # nv_pci_info_t
    _fields_ = [("domain", u32), ("bus", u8), ("slot", u8), ("function", u8),
                ("vendor_id", u16), ("device_id", u16)]


class CardInfo(ctypes.Structure):        # nv_ioctl_card_info_t
    _fields_ = [("valid", u8), ("pci_info", PciInfo), ("gpu_id", u32), ("interrupt_line", u16),
                ("reg_address", u64), ("reg_size", u64), ("fb_address", u64), ("fb_size", u64),
                ("minor_number", u32), ("dev_name", u8 * 10)]


class GpuIdInfo(ctypes.Structure):       # NV0000_CTRL_GPU_GET_ID_INFO_V2_PARAMS
    _fields_ = [("gpuId", u32), ("gpuFlags", u32), ("deviceInstance", u32), ("subDeviceInstance", u32),
                ("sliStatus", u32), ("boardId", u32), ("gpuInstance", u32), ("numaId", i32)]


class BlockRW(ctypes.Structure):         # NV402C_CTRL_I2C_TRANSACTION_DATA_I2C_BLOCK_RW
    _fields_ = [("bWrite", u8), ("messageLength", u32), ("pMessage", u64)]


class TransData(ctypes.Union):
    _fields_ = [("block", BlockRW), ("raw", u8 * 80)]


class I2cTransaction(ctypes.Structure):  # NV402C_CTRL_I2C_TRANSACTION_PARAMS
    _fields_ = [("portId", u8), ("flags", u32), ("deviceAddress", u16), ("transType", u32),
                ("transData", TransData)]


class RegisterFd(ctypes.Structure):
    _fields_ = [("ctl_fd", ctypes.c_int)]


for _struct, _size in ((NVOS21, 32), (NVOS00, 16), (NVOS54, 32), (DeviceAlloc, 56),
                       (CardInfo, 72), (GpuIdInfo, 32), (I2cTransaction, 96)):
    assert ctypes.sizeof(_struct) == _size, _struct.__name__
assert CardInfo.minor_number.offset == 56 and I2cTransaction.transData.offset == 16


def _ioc(nr, size):
    return (3 << 30) | (size << 16) | (NV_IOCTL_MAGIC << 8) | nr   # _IOWR('F', nr, size)


class RmError(TransportError):
    def __init__(self, status, what):
        self.status = status
        detail = os.strerror(-status) if status < 0 else NV_STATUS.get(status, "NVIDIA driver error")
        super().__init__(f"{what}: {detail} (status {status:#x})")


class NvRmTransport(Transport):
    """LCD traffic on the GPU's internal I2C port via /dev/nvidiactl."""

    name = "nvrm"

    def __init__(self, gpu, speed_khz=400):
        if speed_khz not in SPEED_MODES:
            raise ValueError(f"speed must be one of {sorted(SPEED_MODES)} kHz")
        self.gpu, self.speed_khz = gpu, speed_khz
        self._ctl = self._dev = -1
        self._client = self._i2c = 0
        try:
            self._open()
        except BaseException:
            self.close()
            raise

    # -- RM plumbing -----------------------------------------------------------
    def _ioctl(self, fd, nr, obj, what):
        try:
            fcntl.ioctl(fd, _ioc(nr, ctypes.sizeof(obj)), obj, True)
        except OSError as e:
            raise RmError(-(e.errno or errno.EIO), what) from None

    def _alloc(self, parent, cls, params=None):
        p = NVOS21(hRoot=self._client, hObjectParent=parent, hClass=cls)
        if params is not None:
            p.pAllocParms, p.paramsSize = ctypes.addressof(params), ctypes.sizeof(params)
        self._ioctl(self._ctl, NV_ESC_RM_ALLOC, p, f"allocating RM class {cls:#x}")
        if p.status:
            raise RmError(p.status, f"allocating RM class {cls:#x}")
        return p.hObjectNew

    def _control(self, obj, cmd, params, what):
        p = NVOS54(hClient=self._client, hObject=obj, cmd=cmd,
                   params=ctypes.addressof(params), paramsSize=ctypes.sizeof(params))
        self._ioctl(self._ctl, NV_ESC_RM_CONTROL, p, what)
        return p.status

    def _open(self):
        self._ctl = os.open("/dev/nvidiactl", os.O_RDWR | os.O_CLOEXEC)
        self._dev = os.open(f"/dev/nvidia{self.gpu.minor}", os.O_RDWR | os.O_CLOEXEC)
        self._ioctl(self._dev, NV_ESC_REGISTER_FD, RegisterFd(self._ctl), "registering GPU fd")
        self._client = self._alloc(0, NV01_ROOT_CLIENT)
        # A /dev/nvidiaN minor is not necessarily the RM device instance.
        cards = (CardInfo * 32)()
        self._ioctl(self._ctl, NV_ESC_CARD_INFO, cards, "reading card info")
        card = next((c for c in cards if c.valid and c.minor_number == self.gpu.minor), None)
        if card is None:
            raise RmError(-errno.ENODEV, f"GPU minor {self.gpu.minor}")
        ids = GpuIdInfo(gpuId=card.gpu_id)
        status = self._control(self._client, NV0000_CTRL_CMD_GPU_GET_ID_INFO_V2, ids, "GPU id info")
        if status:
            raise RmError(status, "GPU id info")
        device = self._alloc(self._client, NV01_DEVICE_0,
                             DeviceAlloc(deviceId=ids.deviceInstance, hClientShare=self._client))
        subdevice = self._alloc(device, NV20_SUBDEVICE_0, SubdeviceAlloc(ids.subDeviceInstance))
        self._i2c = self._alloc(subdevice, NV40_I2C)

    def close(self):
        if self._client and self._ctl >= 0:
            # Freeing the client frees everything under it.
            p = NVOS00(hRoot=self._client, hObjectParent=self._client, hObjectOld=self._client)
            try:
                fcntl.ioctl(self._ctl, _ioc(NV_ESC_RM_FREE, ctypes.sizeof(p)), p, True)
            except OSError:
                pass
        self._client = self._i2c = 0
        for fd in (self._dev, self._ctl):
            if fd >= 0:
                os.close(fd)
        self._ctl = self._dev = -1

    # -- bus operations -----------------------------------------------------------
    def _transaction(self, address, flags, trans_type, write, buf, what):
        if not self._i2c:
            raise TransportError("RM transport is closed")
        t = I2cTransaction(portId=INTERNAL_PORT, flags=flags, deviceAddress=address << 1,
                           transType=trans_type)
        t.transData.block.bWrite = 1 if write else 0
        if buf is not None:
            t.transData.block.messageLength = len(buf)
            t.transData.block.pMessage = ctypes.addressof(buf)
        return self._control(self._i2c, NV402C_CTRL_CMD_I2C_TRANSACTION, t, what)

    def _speed_flags(self, speed_khz):
        return SPEED_MODES[speed_khz or self.speed_khz] << 1

    def write(self, address, data, speed_khz=None):
        data = bytes(data)
        self.check(address, len(data))
        buf = (u8 * len(data)).from_buffer_copy(data)
        what = f"write {len(data)} bytes to {address:#04x}"
        status = self._transaction(address, self._speed_flags(speed_khz), I2C_BLOCK_RW, True, buf, what)
        if status:
            raise RmError(status, what)

    def read(self, address, length, speed_khz=None):
        self.check(address, length, write=False)
        buf = (u8 * length)()
        what = f"read {length} bytes from {address:#04x}"
        status = self._transaction(address, self._speed_flags(speed_khz), I2C_BLOCK_RW, False, buf, what)
        if status:
            raise RmError(status, what)
        return bytes(buf)

    def ping(self, address, speed_khz=None):
        self.check(address, 1, write=False)          # no probing the RGB controller
        what = f"ping {address:#04x}"
        status = self._transaction(address, self._speed_flags(speed_khz) | FLAG_PING,
                                   SMBUS_QUICK_RW, True, None, what)
        if status == 0x14:
            return False
        if status:
            raise RmError(status, what)
        return True
