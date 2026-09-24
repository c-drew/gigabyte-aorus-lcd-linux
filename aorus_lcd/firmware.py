"""LCD firmware recovery through the panel MCU's IAP bootloader.

Symptom this fixes: the side screen stays black, 0x61 never answers, and an
I2C scan of the GPU's internal port shows 0x22 (and the RGB controller 0x75).
That is the LCD MCU sitting in its bootloader because its application
firmware is missing or invalid; asking it to start the app does nothing.

The fix is what Gigabyte's LCD firmware updater does, reproduced
byte-for-byte from its GvLcdFwUpdate.dll (verified by running the DLL's own
routines in an emulator and diffing every bus write):

  1. Erase12ByteMode  0x8203  range 0x1000..0xEFFF
  2. SubmitCRCAP      0x8104  writes the app header at 0x1020..0x1027:
                              [CRC16(app[0x28:]) | 0x1A0C0000][len - 0x28]
  3. FlashAP12Byte    0x8204  range 0x1000..0x1000+len-1, then the image as
                              8-byte chunks + CRC16
  4. ChangeToAP       0x8102  start the application

Commands are [cmd u16][~cmd u16] (+ [len u16][crc16][payload]), little-endian,
CRC-16/XMODEM. After each command the updater polls status by writing
00 00 00 00 and reading 4 bytes (non-zero = ready). Never read the
bootloader without that write first: an idle read stalls the whole bus until
the PC is fully powered off.

The bootloader itself (0x0000..0x0FFF) is never erased, so an interrupted
flash leaves the panel exactly as recoverable as before. No Gigabyte
firmware is distributed here: `extract` pulls it from the official updater
you download from Gigabyte's support page.
"""
import hashlib
import time

from .transport import BOOTLOADER_ADDRESSES, LCD_ADDRESS

APP_BASE = 0x1000
ERASE_END = 0xEFFF            # fixed in the vendor updater
HEADER_START, HEADER_END = 0x1020, 0x1027
POLL_LIMIT = 3000

CMD_TO_APP = 0x8102
CMD_SUBMIT_CRC = 0x8104
CMD_ERASE = 0x8203
CMD_FLASH = 0x8204

# Application images this project has verified end to end, by SHA-256.
KNOWN_IMAGES = {
    "369fccb127900cd316baa8bf135197ada0ee1e2a013fa0f65a0d5b4b430facf9": {
        "version": "1.3", "size": 58612, "subsystem": (0x1458, 0x416E),
        "package": "GV-N5090AORUSM-32GD_LCD_F1.3.exe (AORUS GeForce RTX 5090 MASTER 32G)"},
}


def crc16(data, crc=0):
    """CRC-16/XMODEM (poly 0x1021, init 0), as in GvLcdFwUpdate.dll."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _head(cmd):
    return cmd.to_bytes(2, "little") + (cmd ^ 0xFFFF).to_bytes(2, "little")


def _with_crc(block):
    """[len u16][crc placeholder][payload...] -> CRC over the block with the field zeroed."""
    crc = crc16(block[:2] + b"\0\0" + block[4:])
    return block[:2] + crc.to_bytes(2, "little") + block[4:]


def short_command(cmd, payload=b""):
    """Helper 0x4060: 4 bytes, or 4 + [len][crc][payload] when there is a payload."""
    if not payload:
        return _head(cmd)
    body = bytes([4 + len(payload), 0]) + b"\0\0" + payload
    return _head(cmd) + _with_crc(body)


def range_command(cmd, start, end):
    """Helper 0x42B0: a 4-byte header, then [0C 00][crc][start u32][end u32]."""
    body = bytes([12, 0]) + b"\0\0" + start.to_bytes(4, "little") + end.to_bytes(4, "little")
    return _head(cmd), _with_crc(body)


def app_header(image):
    return ((0x1A0C0000 | crc16(image[0x28:])).to_bytes(4, "little")
            + (len(image) - 0x28).to_bytes(4, "little"))


def plan_boot():
    """Ops that ask the bootloader to start the installed application."""
    return [("write", short_command(CMD_TO_APP)), ("sleep", 0.1), ("poll",)]


def plan_flash(image):
    """The vendor updater's full sequence as ("write", bytes) / ("sleep", s) / ("poll",)."""
    ops = []
    hdr, body = range_command(CMD_ERASE, APP_BASE, ERASE_END)
    ops += [("write", hdr), ("poll",), ("write", body), ("sleep", 0.5), ("poll",)]
    rng = HEADER_START.to_bytes(4, "little") + HEADER_END.to_bytes(4, "little")
    ops += [("write", short_command(CMD_SUBMIT_CRC, rng)), ("sleep", 0.1), ("poll",)]
    # 128-byte block + CRC; the bootloader programs only the 8-byte header.
    # The vendor leaves the rest uninitialised; FF leaves flash cells untouched.
    table = app_header(image) + b"\xff" * 120
    ops += [("write", table + crc16(table).to_bytes(2, "little")), ("poll",)]
    hdr, body = range_command(CMD_FLASH, APP_BASE, APP_BASE + len(image) - 1)
    ops += [("write", hdr), ("poll",), ("write", body), ("sleep", 0.5), ("poll",)]
    padded = image + b"\xff" * (-len(image) % 8)
    for off in range(0, len(padded), 8):
        chunk = padded[off:off + 8]
        ops += [("write", chunk + crc16(chunk).to_bytes(2, "little")), ("poll",)]
    return ops + plan_boot()


def identify(image):
    return KNOWN_IMAGES.get(hashlib.sha256(image).hexdigest())


def detect(transport):
    """'app', ('bootloader', address), or None if nothing answers."""
    if transport.ping(LCD_ADDRESS):
        return "app"
    found = [a for a in BOOTLOADER_ADDRESSES if transport.ping(a)]
    if len(found) > 1:
        raise RuntimeError("both bootloader addresses answer; refusing an ambiguous target")
    return ("bootloader", found[0]) if found else None


def poll(transport, address, sleep=time.sleep):
    """Vendor status poll: write 00000000, read 4; non-zero means ready."""
    for _ in range(POLL_LIMIT):
        try:
            transport.write(address, b"\0\0\0\0")
            reply = transport.read(address, 4)
            if any(reply):
                return reply
        except OSError:
            pass
        sleep(0.001)
    raise TimeoutError(f"bootloader at {address:#04x} never reported ready")


def execute(transport, address, ops, sleep=time.sleep, progress=None):
    """Run a plan against the bootloader. Returns the last status reply."""
    writes = sum(op[0] == "write" for op in ops)
    done, reply = 0, None
    for op in ops:
        if op[0] == "write":
            transport.write(address, op[1])
            done += 1
            if progress:
                progress(done, writes)
        elif op[0] == "sleep":
            sleep(op[1])
        else:
            reply = poll(transport, address, sleep)
    return reply


def wait_for_app(transport, timeout=10.0, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if transport.ping(LCD_ADDRESS):
            return True
        sleep(0.25)
    return False


def extract(updater_exe):
    """Pull the application image out of Gigabyte's LCD updater (.NET exe).
    Needs the optional `dnfile` package."""
    try:
        import dnfile
    except ImportError as e:
        raise RuntimeError("extracting needs dnfile (pip install dnfile)") from e
    pe = dnfile.dnPE(str(updater_exe))
    for res in pe.net.resources or []:
        for entry in getattr(res.data, "entries", None) or []:
            data = entry.data
            if entry.name.lower() == "resource/ap" and isinstance(data, (bytes, bytearray)):
                size = int.from_bytes(data[:4], "little")
                if size == len(data) - 4:
                    return bytes(data[4:])
    raise ValueError(f"{updater_exe} does not contain an LCD application image")
