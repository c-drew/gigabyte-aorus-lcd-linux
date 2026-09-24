# Gigabyte AORUS LCD Edge View on Linux

Control the **LCD Edge View** side screen of the **Gigabyte AORUS GeForce RTX 5090 MASTER**
(and likely other AORUS Master cards with the same panel) from Linux: logos, images, GIFs,
text, **live GPU stats** and the card's **RGB lighting**, from a browser UI or the command
line, kept at every boot by a small systemd service. No Gigabyte Control Center, no Windows,
no root.

It can also **bring a black, dead-looking panel back to life**: if the LCD's firmware is
stuck in its bootloader, `aorus-lcd firmware` reflashes it with Gigabyte's own firmware,
byte-for-byte the way their updater does.

```bash
aorus-lcd logo omarchy.svg --color '#ff0000'          # any SVG/PNG, recoloured
aorus-lcd gif party.gif
aorus-lcd text "hello from linux" --color '#ff0000'
aorus-lcd overlay --widgets temp,usage,power,clock    # the panel's own stats widgets
sudo scripts/install.sh                               # keep it that way on every boot
```

## Status

Tested on one card: **AORUS GeForce RTX 5090 MASTER 32G** (PCI subsystem `1458:416e`,
LCD firmware 1.3), NVIDIA open driver 615.71.09, CachyOS/Arch. The panel protocol is
reverse-engineered; other AORUS Master cards (5080, 5070 Ti, 40-series) use the same
family and may work. Reports welcome.

## Why this exists (and what's different)

Earlier Linux tools talk to the panel through `/dev/i2c-N`. The NVIDIA kernel driver runs
that bus at a hard-wired **100 kHz** and holds the GPU lock for every transfer; Gigabyte's
Windows software asks for **400 kHz**. On some cards and firmware the panel simply does not
answer at 100 kHz. This project:

- **Talks to the NVIDIA resource manager directly** (`/dev/nvidiactl`, the same path the
  NVIDIA userspace libraries use), in pure Python with `ctypes`: 400 kHz per transaction,
  no root, no kernel module, no build step.
- **Fixes GIFs stuck on "Loading"**: Gigabyte's software asks the panel to erase its flash in
  64 KB blocks for anything over 20 KB, and the panel firmware gives up on those erases too
  early ([EKYavsil's analysis](https://github.com/EKYavsil/aorus-lcd-panel-reverse-engineering)).
  This tool always uses 4 KB sector erases and waits until every erase has finished.
- **Live GPU stats** through the panel firmware's own overlay widgets, fed from NVML.
  Updates are only sent when a value moves, because every panel write costs ~6 ms of
  NVIDIA driver time.
- **Recovers a panel stuck in its bootloader** (see below).

## Web UI

The service serves a small control page at **http://127.0.0.1:5090** (also "AORUS LCD" in
your app launcher after `install.sh`): drop in a logo, image or GIF, type text, pick a
colour, see an exact preview (the same renderer that uploads, animations included), send it,
and tune the stats overlay and the GPU's RGB lighting. **Presets** save a whole look (screen
+ stats + lighting) and switch in one click. Choices made there persist across reboots; if
you edit `/etc/aorus-lcd/config.toml` afterwards, the file wins.

Without the service, `aorus-lcd gui` starts the same thing in the foreground and opens it.

It only listens on 127.0.0.1, rejects requests from other sites (Host check + a required
header), and grants nothing a local program could not already do.

## GPU RGB lighting

The card's RGB Fusion 2 controller (0x75) sits on the same I2C bus, so the tool can set it
too: static, breathing, flashing, colour cycle, wave, gradient, colour shift, tricolour,
dazzle or off, brightness 1-10, speed 1-6, saved to the card so it survives reboots. It is
**off by default** (`[lighting] enabled = false`) so it never fights OpenRGB. The controller
is write-only: the tool never reads it (a read wedges the bus) and only writes at 400 kHz.
Protocol from OpenRGB's `GigabyteRGBFusion2BlackwellGPUController` via CodeTorchAI/AorusLcd.

## Install

Requirements: Python 3.11+, Pillow, the NVIDIA driver (open or proprietary), and
`rsvg-convert` (librsvg) for SVG logos.

```bash
git clone https://github.com/c-drew/gigabyte-aorus-lcd-linux
cd gigabyte-aorus-lcd-linux
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/aorus-lcd status
```

```
GPU:       NVIDIA GeForce RTX 5090 [1458:416e] at 0000:01:00.0
LCD:       firmware 1.3, on, mode 3 (image)
Overlay:   temp, clock, usage, power (rotating every 3s)
```

### Run it at every boot

```bash
cp examples/config.toml my-lcd/config.toml     # edit it; put your logo next to it
sudo scripts/install.sh my-lcd                  # /opt/aorus-lcd + /etc/aorus-lcd + systemd
journalctl -u aorus-lcd -f
sudo systemctl reload aorus-lcd                 # after editing /etc/aorus-lcd/config.toml
```

The service runs as an unprivileged dynamic user. It uploads your content only when it
changed (the panel keeps its image across power cycles, and every upload rewrites its
flash), turns on the stats overlay, and keeps it fed. See
[`examples/config.toml`](examples/config.toml) for every option.

## Commands

| Command | What it does |
|---|---|
| `status` | GPU, panel firmware, display mode, overlay; detects a stuck bootloader |
| `logo FILE [--color C] [--scale S] [--anchor A] [--offset X,Y] [--pulse]` | SVG/PNG logo recoloured on black; `--pulse` uploads a breathing-glow GIF |
| `image FILE [--fit contain\|cover]` | a picture, fitted to 320x170 |
| `gif FILE` | an animated GIF |
| `text "..." [--color C] [--size N] [--wave]` | a text message; `--wave` = the panel's own animated rainbow wave |
| `overlay [--widgets ...] [--color C] [--data-pos X,Y] [--off]` | the firmware's stats widgets: `temp clock usage fan vram_clock vram fps power` |
| `stats` | feed live values to the overlay in the foreground |
| `power on\|off`, `mode N` | panel power; 0-2 Gigabyte stat screens, 3 image, 4 text, 5 gif, 6 chibi clock |
| `gui` | open the web UI (starts the service in the foreground if it is not running) |
| `apply`, `daemon` | apply a config once, or run the service (with the web UI) |
| `query HEX` | send a read command (`de`, `df`, `d6`, `eb 02` ...) and print the 4-byte reply |
| `firmware extract/boot/flash` | bootloader recovery, below |

Every content command takes `--preview out.png` to render without touching the hardware.

Layout notes: the firmware draws the stats block (a small label over a big value) from
`--data-pos` (default `146,64`) toward the right edge, and it runs off the bottom of the 170 px
panel for y much above 64. A square logo on the left makes a clean two-column layout
(`logo icon.png --anchor left --offset 20,0 --scale 0.7`); a wide wordmark fits in the top band
(`--anchor top --scale 0.62 --offset 0,8`). The overlay style is
stored per display mode; `overlay` sets image and GIF mode together.

Everything is sent through the panel's GIF mode, RLE-compressed; stills are single-frame
GIFs. Gigabyte's separate image/text framebuffers are unreliable on this firmware (a raw
upload can complete and draw nothing) and cannot take compressed data, so a still logo
uploads in ~2 s instead of ~12 s.

## Black screen? Firmware recovery

Symptom: the side screen is black (or never shows anything), `aorus-lcd status` reports
**STUCK IN BOOTLOADER**, and a scan of the GPU's internal I2C port shows `0x22` but not
`0x61`. The LCD's microcontroller is sitting in its IAP bootloader because its application
firmware is missing or corrupt (an interrupted update, for example). Power cycles do not
help; asking it to start the app does nothing.

```bash
aorus-lcd firmware boot                        # try starting the installed firmware first
# Download the LCD firmware for your card from Gigabyte's support page, e.g.
# GV-N5090AORUSM-32GD_LCD_F1.3.exe, then:
pip install dnfile
aorus-lcd firmware extract GV-N5090AORUSM-32GD_LCD_F1.3.exe -o lcd.bin
aorus-lcd firmware flash lcd.bin --dry-run     # every bus write, nothing sent
aorus-lcd firmware flash lcd.bin --yes         # ~1-2 minutes, don't power off
```

The sequence (erase `0x8203`, CRC header `0x8104`, program `0x8204`, start `0x8102`) was
recovered from Gigabyte's `GvLcdFwUpdate.dll` and verified by running the DLL's own routines
in an emulator and diffing all 14,672 bus operations against this implementation. The same
byte sequence brought the author's panel back from a stuck bootloader. Only the
application area (`0x1000-0xF4F3`) is erased; the bootloader is never touched, so a failed
attempt leaves the panel exactly as recoverable as before. Only firmware images whose
SHA-256 is known, for the matching card, are accepted. No Gigabyte firmware is included in
this repository.

**Never read from the bootloader without a status request first**: an idle read makes it
hold the I2C bus, and only a full power-off (PSU switch) releases it. This tool never does
that.

## How it works

- `aorus_lcd/transport/nvrm.py`: root client -> `NV01_DEVICE_0` -> `NV20_SUBDEVICE_0` ->
  `NV40_I2C`, then `NV402C_CTRL_CMD_I2C_TRANSACTION` on internal port 1 with a per-call
  speed. Transports only allow the LCD addresses (`0x61`, bootloader `0x22`/`0x23`), never
  the RGB controller at `0x75`.
- `aorus_lcd/protocol.py`: 256-byte command frames `[op, CB 55 AC 38, ...]` (the firmware
  ignores shorter ones), uploads `F2 -> F1 -> chunks -> F2`, frame tables, the RLE encoder
  (byte-identical to Gigabyte's `Compress_RLE`), overlay `E1`/`EA`/`E3`.
- `aorus_lcd/panel.py`: pacing, mode handling, and a cross-process bus lock (an abstract
  Unix socket) so the CLI and the service never interleave frames.
- `aorus_lcd/render.py`, `content.py`: Pillow rendering to little-endian RGB565; add a
  content type by adding a builder.
- `aorus_lcd/sensors.py`: NVML through `ctypes`. `controller.py`: the service's single owner
  of the panel (settings, uploads as background jobs, presets); `daemon.py`: the service loop;
  `web.py` + `static/index.html`: the UI (stdlib only, no build step); `lighting.py`: GPU RGB;
  `firmware.py`: bootloader recovery.

`/dev/i2c-N` is still available with `--transport i2c-dev` (needs `smbus2` and i2c-dev).

## Credits

- [albancreton/aorus-master-linux](https://github.com/albancreton/aorus-master-linux): the
  original Linux tool and protocol recovery (upload framing, RLE encoder, GIF container);
  this project started from it (MIT).
- [CodeTorchAI/AorusLcd](https://github.com/CodeTorchAI/AorusLcd): overlay, sensor feed and
  template protocol, recovery notes (MIT).
- [EKYavsil/aorus-lcd-panel-reverse-engineering](https://github.com/EKYavsil/aorus-lcd-panel-reverse-engineering):
  the panel firmware's flash-erase analysis and the firmware updater's IAP flow.
- [PrivateGER/Gigabyte-Aorus-LCD-Driver](https://github.com/PrivateGER/Gigabyte-Aorus-LCD-Driver):
  an independent Linux RM transport for the 5080 Master ICE.
- NVIDIA's [open-gpu-kernel-modules](https://github.com/NVIDIA/open-gpu-kernel-modules)
  headers for the RM ABI.

Not affiliated with Gigabyte or NVIDIA. AORUS and LCD Edge View are Gigabyte trademarks.

## License

MIT, see [LICENSE](LICENSE).
