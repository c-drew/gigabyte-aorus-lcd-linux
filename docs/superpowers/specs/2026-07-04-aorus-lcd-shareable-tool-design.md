# aorus-master-linux — shareable LCD control tool

**Date:** 2026-07-04
**Status:** approved design
**Source:** cleanup/refactor of `~/Dev/aorus-lcd-re/linux/aorus_lcd.py` (the working
reverse-engineering result) into a public GitHub repo.

## Goal

A clean, auditable, GitHub-shareable tool to control the "LCD Edge View" screen on
the Gigabyte Aorus Master RTX 5090 from Linux. Super-alpha status: no PyPI, no
packaging, no daemon — one reviewed script, a good README, MIT license.

## Non-goals

- Publishing anywhere other than GitHub.
- The Frida-capture/RE tooling: `--replay`, capture parsing, `--extract-bin`,
  `--upload-bin` are **dropped** (they depend on private captures / GCC blobs).
- The boot-time systemd service (stays in the private RE repo; README mentions
  persistence behavior only).
- Multi-card support claims. Tested on exactly one card; README says so.

## Repo layout

```
aorus_lcd.py     # the tool — single file, stdlib + smbus2; Pillow only for image/text/gif
README.md        # what/why/safety/install/usage/protocol summary
LICENSE          # MIT
.gitignore
tests/test_encoders.py   # dev-only pure-function tests (RLE, frame table, headers)
```

Single file (approach A) chosen over a package split: easier to audit before
running as root against a GPU i2c bus, and curl-able.

## CLI: subcommands (replaces 25 flat flags)

```
aorus_lcd.py probe                     # find/confirm the controller (safe, read-only-ish)
aorus_lcd.py on | off                  # OpenLcd (E7)
aorus_lcd.py mode M                    # SetMode (E5), M in 0..7
aorus_lcd.py image PHOTO.png           # static image -> fb 0x01300000, then mode 3
aorus_lcd.py text "hello" [--size N --color RRGGBB --bg RRGGBB --no-effect]
                                       # rendered text -> fb 0x01320000, mode 4, 0xAA effect
aorus_lcd.py gif ANIM.gif [--frame-delay MS]   # RLE upload -> fb 0, mode 5 first, then stream
aorus_lcd.py carousel 0,1,4 [--arg N]  # SetLoop (F3)
aorus_lcd.py brightness V              # SetDisplay (E1) — EXPERIMENTAL, semantics inferred
aorus_lcd.py poweroff-mode             # SetPCPowerOffMode (FA) — EXPERIMENTAL
aorus_lcd.py raw "aa 01 02"            # escape hatch: build+send a command frame
aorus_lcd.py raw-read "eb 03" [--len N]  # escape hatch: command frame + read back
aorus_lcd.py selftest                  # run the pure-function encoder checks, no hardware
```

Global options: `--bus N` (default: autodetect), `--no-mode` (upload without the
SetMode switch), pacing overrides for uploads.

Experimental commands print a clear "experimental / unverified" note when used.

## Bus autodetection (new, ported from the RE repo's deploy wrapper)

When `--bus` is omitted: scan `/sys/class/i2c-dev/i2c-*/name` for the adapter
whose name starts with `NVIDIA i2c adapter 1 at`, then require a 0-length-write
ACK at 0x61 before any real write. Refuse with a clear error (listing candidate
buses) if not found. This removes the main hazard of a shared tool — users
writing to a chipset SMBus by guessing `/dev/i2c-0`.

## Script structure (top to bottom)

1. **Module docstring** — what it drives, safety notes, requirements.
2. **Constants / protocol reference** — addresses (0x61 target; 0x71 RGB = never
   touch), panel geometry (320x170 LE-RGB565), framebuffer targets, opcodes,
   mode table, the 12-byte payload descriptor.
3. **Bus discovery** — `find_nvidia_bus()`, `probe()`.
4. **Frame builders** — `cmd_frame()`, `f2_frame()`, `make_f1_header()`, chunking.
   Header chunk-count arithmetic (`usize//256 + 1`, including the full-pad frame on
   exact multiples) is protocol-observed behavior — keep exactly.
5. **Media encoders** —
   - one shared `image_to_le565(PIL.Image) -> bytes` (currently duplicated 3×),
   - `render_text()` (Pillow),
   - GIF pipeline: `gif_frames()`, `rle_encode_frame()`, `gif_frame_table()`.
     `rle_encode_frame` is byte-exact-validated against GCC's Compress_RLE —
     its quirks (window semantics, <4-px literal tails) MUST NOT be "cleaned up".
6. **Upload sequencing** — build frames, write with **fixed pacing** (0.5 s after
   BEGIN, 1.0 s after F1 header, 10 ms per 256-byte chunk) replacing the
   capture-timestamp replay machinery. GIF ordering rule kept: SetMode 5 BEFORE
   streaming (live fb 0); image/text SetMode AFTER (stored fbs).
7. **Commands** — `open_lcd`, `set_mode`, `set_brightness`, `set_carousel`,
   `power_off_mode`, text-effect (0xAA), `read_cmd`.
8. **CLI** — argparse subparsers; friendly errors for missing smbus2/Pillow,
   missing i2c-dev module, and EACCES (suggest root / i2c group).

## Error handling

- No smbus2 → exit with install hint. No Pillow → only for image/text/gif, exit
  with hint there.
- `/dev/i2c-N` missing → suggest `modprobe i2c-dev`.
- Permission denied → suggest sudo / i2c group.
- No NVIDIA bus found / no ACK → list `/sys/class/i2c-dev` names, refuse to write.

## Testing

- `tests/test_encoders.py` (and `selftest` subcommand sharing the same checks):
  RLE encoder vectors (runs, literals, window edges, <4-px tail), frame-table
  offsets, F1 header field packing, chunk padding. Pure functions, no hardware.
- Hardware verification before calling it done: `probe`, `mode 3`, an `image`
  upload, and a `gif` upload against the real card.

## README outline

1. What this is (photo-worthy one-liner), alpha warning, tested-on-one-card warning.
2. Safety story: writes only to 0x61 after ACK; never touches 0x71 (RGB);
   bus found by name not number.
3. Install: `modprobe i2c-dev`, venv, `pip install smbus2 Pillow`.
4. Usage: every subcommand with a copy-paste example.
5. Protocol reference (short): legacy 0x61 protocol, F1/F2 framing, modes 0–6,
   framebuffer targets, GIF RLE + frame-table format. Credit the RE process.
6. Troubleshooting: wrong bus, no ACK, permissions, black screen after gif
   (mode ordering).
7. Persistence note: panel retains last static image across boots; pin with
   `mode 3` at boot (systemd example left for later).

## License

MIT, copyright 2026 Alban (exact name/handle for the copyright line to be
confirmed by the author before publishing).
