# aorus-lcd

Control the "LCD Edge View" screen on the Gigabyte Aorus Master RTX 5090 from
Linux — no Gigabyte software needed.

```
sudo .venv/bin/python aorus_lcd.py image wallpaper.png
```

That resizes `wallpaper.png` to 320x170, uploads it over i2c, and switches the
panel to display it. (See [Install](#install) for the one-time venv setup;
`sudo ./aorus_lcd.py ...` also works if `smbus2` and `Pillow` are installed
system-wide.)

## Status: ALPHA

This is a reverse-engineered protocol implementation, not an official driver.

- The protocol was recovered by capturing Gigabyte Control Center (GCC)'s i2c
  traffic on Windows and decompiling `ucVga.dll`. There is no vendor spec.
- It has been tested on exactly **one card**: an Aorus Master RTX 5090, using
  what this tool calls the "legacy 0x61" protocol.
- Other Gigabyte cards with an LCD side-screen may use a different, newer
  protocol (referred to in the decompile as "LcdEx", at i2c address 0x76).
  This tool does not speak that protocol. On such a card it will either fail
  to find a compatible bus, or `probe` will report no ACK at 0x61 — it will
  not attempt anything unverified.
- Commands marked `[experimental]` in `--help` (`brightness`, `poweroff-mode`,
  `raw`, `raw-read`) have semantics inferred from the decompiled code, not
  confirmed against hardware behavior. Use them to explore, not to depend on.

## Safety

- This tool writes only to i2c address **0x61**, the LCD controller. It never
  writes to **0x71**, the card's RGB/ARGB lighting controller, even though
  both sit on the same bus.
- The GPU's internal i2c bus is located by its sysfs adapter **name**
  (`NVIDIA i2c adapter 1 at ...`), never by guessing a `/dev/i2c-N` path. On a
  multi-GPU or otherwise unusual system this avoids landing on the wrong bus.
- Before any write, the tool sends a zero-length probe to 0x61 and requires an
  ACK. If nothing acks, it refuses to write and tells you why (`probe`,
  `resolve_bus` in `aorus_lcd.py`).
- Nothing here flashes firmware or touches persistent GPU state beyond the
  panel's own image memory. The worst observed failure mode during
  development was the panel showing stale or black content until the next
  upload or a power cycle — never a bricked card.

## Install

```bash
sudo modprobe i2c-dev
```

To load `i2c-dev` automatically on boot:

```bash
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf
```

Then set up the tool itself:

```bash
python3 -m venv .venv
.venv/bin/pip install smbus2 Pillow
```

`smbus2` is required for everything; `Pillow` is required for `image`,
`text`, and `gif`. `probe`, `on`, `off`, `mode`, `carousel`, and `selftest`
work without Pillow.

You need permission to open the i2c device node. Either run the tool with
`sudo`, or add yourself to the `i2c` group and re-login:

```bash
sudo usermod -aG i2c "$USER"
```

## Usage

Every subcommand accepts a top-level `--bus N` to force a specific
`/dev/i2c-N` instead of autodetecting the NVIDIA bus by adapter name:

```bash
sudo .venv/bin/python aorus_lcd.py --bus 7 probe
```

Check the panel is reachable:

```bash
sudo .venv/bin/python aorus_lcd.py probe
```

Turn the panel on or off:

```bash
sudo .venv/bin/python aorus_lcd.py on
sudo .venv/bin/python aorus_lcd.py off
```

Switch to a built-in display mode (0-7; 3=image, 4=text, 5=gif, 6=chibi —
0-2 are Gigabyte's built-in screens, e.g. GPU stats; 7 is accepted but
unconfirmed — a GCC quirk remaps it to internal value 9):

```bash
sudo .venv/bin/python aorus_lcd.py mode 3
```

Show a static image (any format Pillow can open; resized to 320x170):

```bash
sudo .venv/bin/python aorus_lcd.py image wallpaper.png
```

Show text, with color/background/size and effect controls:

```bash
sudo .venv/bin/python aorus_lcd.py text "hello" \
    --color ff8800 --bg 000000 --size 32 --no-effect
```

`--color`/`--bg` default to GCC's own gray-on-black (`8b8d8b`/`000000`); the
gray is what the panel's rainbow effect (enabled by default, `--no-effect`
to skip it) uses as a luminance mask.

Play an animated gif (frames are RLE-compressed and streamed to a live
framebuffer):

```bash
sudo .venv/bin/python aorus_lcd.py gif animation.gif --frame-delay 40
```

`--frame-delay` overrides the per-frame delay in milliseconds; by default the
delay is derived from the gif's own frame timing.

`image`, `text`, and `gif` all also accept `--no-mode` (upload the content
but leave the current display mode alone) and `--chunk-delay SEC` (override
the 10ms pacing between 256-byte i2c writes, default `0.01`).

Cycle through built-in modes automatically:

```bash
sudo .venv/bin/python aorus_lcd.py carousel 0,1,4 --arg 5
```

Experimental commands — inferred semantics, use with caution:

```bash
sudo .venv/bin/python aorus_lcd.py brightness 80
sudo .venv/bin/python aorus_lcd.py poweroff-mode
sudo .venv/bin/python aorus_lcd.py raw "aa 01 02"
sudo .venv/bin/python aorus_lcd.py raw-read "eb 03" --len 8
```

Run the built-in protocol self-checks (pure functions, no hardware, no
Pillow, safe to run anywhere):

```bash
.venv/bin/python aorus_lcd.py selftest
```

## How it works

The panel is 320x170; pixel data is little-endian RGB565, row-major.

Every command to the panel is a 256-byte i2c block write of the form
`[opcode, CB 55 AC 38, params...]`, zero-padded to 256 bytes.

Uploading an image, text render, or gif is a fixed sequence of such frames:

1. `F2` BEGIN marker
2. `F1` header — 19 meaningful bytes: target framebuffer address, an
   animated/static flag, chunk count, frame count, per-frame delay, and a
   size-dependent mode byte
3. the payload itself, split into 256-byte zero-padded chunks
4. `F2` END marker

then `E5` SetMode selects what the panel actually displays.

Framebuffer targets seen in the F1 header:

| Target       | Used by       | Display mode |
|--------------|---------------|--------------|
| `0x01300000` | static image  | 3 |
| `0x01320000` | text          | 4 |
| `0x00000000` | animated gif (live buffer) | 5 |

Display modes 0-6 confirmed on hardware: 0-2 are Gigabyte's built-in screens
(e.g. GPU stats), 3 = static image, 4 = text, 5 = gif, 6 = chibi.

Ordering matters and differs by content type: a gif streams into framebuffer
0, a *live* buffer the panel renders as bytes arrive, so `SetMode 5` must be
sent **before** streaming starts. Image and text land in their own numbered
framebuffers, so their `SetMode` is sent **after** the upload completes —
this is the sequence observed in live GCC captures, and `upload_content()` in
`aorus_lcd.py` implements it accordingly.

A gif payload is `<frame count: u16 LE>` followed by one 10-byte table entry
per frame (`end offset: u32 LE` — the inclusive end of that frame's blob
within the payload, `width: u16`, `height: u16`, `format: u16` — `3` means
RLE), followed by the concatenated RLE-compressed frames.

The RLE grammar per frame is a stream of tokens, each starting with a 2-byte
little-endian head where bit 15 is a run flag and the low 15 bits are a pixel
count:

- run: head with bit15 set, count = number of repeated pixels, followed by
  one pixel (2 bytes) — only emitted for runs of 3+ identical pixels
- literal: head with bit15 clear, count = number of pixels, followed by that
  many raw pixels

This encoder (`rle_encode_frame` in `aorus_lcd.py`) is byte-identical to
GCC's own `Compress_RLE`, validated by re-encoding every frame of GCC's
`animation.bin` and diffing byte-for-byte (60/60 frames matched exactly).

This is a clean-room reimplementation from observed traffic and decompiled
IL; no Gigabyte code is included.

## Troubleshooting

**`probe` says it can't find the NVIDIA bus.** Either `i2c-dev` isn't loaded
(`sudo modprobe i2c-dev`), or your card doesn't expose an adapter named
`NVIDIA i2c adapter 1 at ...` — which likely means it's not an Aorus Master
RTX 5090, or it uses the newer LcdEx (0x76) protocol this tool doesn't speak.
Pass `--bus N` if you know the right bus and want to try anyway.

**Permission denied.** Run with `sudo`, or add yourself to the `i2c` group
and re-login (see Install above).

**Gif upload shows a black screen.** Don't upload with `--no-mode` and then
separately switch to mode 5 — a gif's framebuffer is a *live* buffer, so the
panel must already be in gif mode before the frames start arriving. Let
`gif` set the mode itself (the default), or if you do need manual control,
send `mode 5` before streaming, not after.

**Panel shows nothing (or the wrong thing) after a reboot.** The panel
retains the last static image it was shown across power cycles, so after a
fresh boot it may still be showing whatever was there before, or nothing
until you upload again. Run `image` (or `mode 3` if you'd already uploaded
one) once after boot to pin it. A systemd unit to automate this may be added
later.

## License

MIT — see [LICENSE](LICENSE).
