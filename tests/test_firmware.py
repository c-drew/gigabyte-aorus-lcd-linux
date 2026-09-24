"""Bootloader protocol. Expected bytes come from running Gigabyte's
GvLcdFwUpdate.dll routines in an emulator and recording their I2C writes."""
import pytest

from aorus_lcd import firmware as F


def test_crc16_xmodem():
    assert F.crc16(b"123456789") == 0x31C3


def test_return_to_app_is_four_bytes():
    assert F.short_command(F.CMD_TO_APP).hex() == "0281fd7e"


def test_erase_command_matches_vendor():
    hdr, body = F.range_command(F.CMD_ERASE, F.APP_BASE, F.ERASE_END)
    assert hdr.hex() == "0382fc7d"
    assert body.hex() == "0c00a4e600100000ffef0000"


def test_submit_crc_command_matches_vendor():
    rng = (0x1020).to_bytes(4, "little") + (0x1027).to_bytes(4, "little")
    assert F.short_command(F.CMD_SUBMIT_CRC, rng).hex() == "0481fb7e0c006f7f2010000027100000"


def test_flash_setup_matches_vendor_for_a_58612_byte_image():
    hdr, body = F.range_command(F.CMD_FLASH, F.APP_BASE, F.APP_BASE + 58612 - 1)
    assert hdr.hex() == "0482fb7d"
    assert body.hex() == "0c00041a00100000f3f40000"


def test_plan_shape_on_a_synthetic_image():
    image = bytes(range(256)) * 4 + b"\x42\x42\x42\x42"          # 1028 bytes, not a multiple of 8
    ops = F.plan_flash(image)
    writes = [op[1] for op in ops if op[0] == "write"]
    # erase(2) + submit(2) + flash setup(2) + chunks + boot(1)
    assert len(writes) == 2 + 2 + 2 + 129 + 1
    table = writes[3]
    assert len(table) == 130 and table[:8] == F.app_header(image) and table[8:128] == b"\xff" * 120
    assert table[128:] == F.crc16(table[:128]).to_bytes(2, "little")
    first, last = writes[6], writes[-2]
    assert first == image[:8] + F.crc16(image[:8]).to_bytes(2, "little")
    assert last[:8] == b"\x42" * 4 + b"\xff" * 4
    assert writes[-1].hex() == "0281fd7e"
    # every write is followed by a poll, never a bare read
    for i, op in enumerate(ops):
        if op[0] == "write":
            assert ("poll",) in ops[i + 1:i + 3]


class FakeBootloader:
    def __init__(self, ready_after=2):
        self.log, self.ready_after, self.polls = [], ready_after, 0

    def write(self, address, data):
        self.log.append(("W", address, bytes(data)))

    def read(self, address, length):
        assert self.log[-1] == ("W", address, b"\0\0\0\0"), "bootloader read without a status request"
        self.polls += 1
        return bytes([2, 0x5A, 0xFD, 0xA5]) if self.polls >= self.ready_after else bytes(4)


def test_poll_writes_a_status_request_before_every_read():
    bl = FakeBootloader(ready_after=3)
    assert F.poll(bl, 0x22, sleep=lambda s: None) == bytes([2, 0x5A, 0xFD, 0xA5])
    assert bl.polls == 3


def test_execute_boot_plan():
    bl = FakeBootloader()
    reply = F.execute(bl, 0x22, F.plan_boot(), sleep=lambda s: None)
    assert reply[1] == 0x5A and bl.log[0] == ("W", 0x22, bytes.fromhex("0281fd7e"))


def test_unknown_images_are_not_identified():
    assert F.identify(b"not firmware") is None


def test_poll_times_out(monkeypatch):
    monkeypatch.setattr(F, "POLL_LIMIT", 5)
    with pytest.raises(TimeoutError):
        F.poll(FakeBootloader(ready_after=99), 0x22, sleep=lambda s: None)
