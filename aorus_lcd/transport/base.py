"""Transport interface: how bytes reach the LCD controller on the GPU's I2C bus."""

# 7-bit addresses on the GPU's internal I2C port (port 1).
LCD_ADDRESS = 0x61                 # LCD application firmware
BOOTLOADER_ADDRESSES = (0x22, 0x23)  # LCD IAP bootloader (vendor 0x44 / 0x46)
RGB_ADDRESS = 0x75                 # RGB Fusion 2 controller: WRITE-ONLY (a read wedges the bus)
ALLOWED_ADDRESSES = (LCD_ADDRESS, *BOOTLOADER_ADDRESSES)
WRITE_ONLY_ADDRESSES = (RGB_ADDRESS,)


class TransportError(OSError):
    """A bus transaction failed (NACK, timeout, stalled bus, driver error)."""


class Transport:
    """Minimal byte-level bus. Implementations restrict themselves to the LCD
    addresses, plus writes (never reads) to the RGB controller."""

    name = "abstract"

    def write(self, address: int, data: bytes) -> None:
        raise NotImplementedError

    def read(self, address: int, length: int) -> bytes:
        raise NotImplementedError

    def ping(self, address: int) -> bool:
        """Address-only presence check. False on NACK."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def check(address: int, length: int, write: bool = True) -> None:
        if address not in ALLOWED_ADDRESSES and not (write and address in WRITE_ONLY_ADDRESSES):
            raise ValueError(f"address {address:#04x} is not allowed"
                             + ("" if write else " for reads (the RGB controller is write-only)"))
        if not 1 <= length <= 256:
            raise ValueError("transfers must be 1..256 bytes")
