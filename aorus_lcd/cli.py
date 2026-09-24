"""aorus-lcd: command line for the Gigabyte AORUS "LCD Edge View" side screen."""
import argparse
import logging
import sys
import time
from dataclasses import replace

from . import __version__
from . import config as C
from . import content, daemon, firmware
from . import protocol as P
from .panel import Panel
from .render import parse_color
from .transport import TRANSPORTS, TransportError, find_gpu, open_transport


def _gpu(args):
    return find_gpu(args.gpu or None)


def _panel(args):
    t = open_transport(args.transport, _gpu(args), args.speed)
    panel = Panel(t)
    try:
        panel.probe()
    except TransportError:
        t.close()
        raise TransportError("the LCD (0x61) does not answer; run `aorus-lcd status` "
                             "(its firmware may be stuck in the bootloader)") from None
    return panel


def _progress(done, total):
    if sys.stderr.isatty():
        print(f"\r  {done}/{total}", end="" if done < total else "\n", file=sys.stderr, flush=True)


def _xy(s):
    x, y = (int(v) for v in s.split(","))
    return [x, y]


# ---- commands ---------------------------------------------------------------------

def cmd_status(args):
    gpu = _gpu(args)
    print(f"GPU:       {gpu.describe()}{'' if gpu.verified else '  (not a verified card)'}")
    with open_transport(args.transport, gpu, args.speed) as t:
        where = firmware.detect(t) if args.transport == "nvrm" else "app"
        if where == "app":
            s = Panel(t).status()
            names = {0: "stats 1", 1: "stats 2", 2: "stats 3", 3: "image", 4: "text", 5: "gif",
                     6: "chibi clock", 7: "carousel"}
            print(f"LCD:       firmware {s['firmware']}, {'on' if s['on'] else 'off'}, "
                  f"mode {s['mode']} ({names.get(s['mode'], '?')})")
            print(f"Overlay:   {', '.join(s['widgets']) or 'off'}"
                  + (f" (rotating every {s['interval']}s)" if s['widgets'] else ""))
        elif where:
            print(f"LCD:       STUCK IN BOOTLOADER at {where[1]:#04x}: application firmware missing "
                  "or invalid.\n           Try `aorus-lcd firmware boot`, then see "
                  "`aorus-lcd firmware flash --help`.")
            sys.exit(1)
        else:
            print("LCD:       no answer at 0x61 or the bootloader addresses. If a bootloader "
                  "read stalled the bus, only a full power-off (PSU switch) clears it.")
            sys.exit(1)


def cmd_power(args):
    with _panel(args) as p:
        p.power(args.state == "on")


def cmd_mode(args):
    with _panel(args) as p:
        p.set_mode(args.mode)


def _content_cfg(args):
    kind = args.command
    c = C.ContentConfig(type=kind)
    for key in ("source", "text", "color", "background", "scale", "anchor", "fit", "size", "font"):
        value = getattr(args, key, None)
        if value is not None:
            c = replace(c, **{key: value})
    if getattr(args, "offset", None):
        c = replace(c, offset=_xy(args.offset))
    if getattr(args, "pulse", False):
        c = replace(c, animate="pulse")
    C.validate(C.Config(content=c))
    return c


def cmd_content(args):
    c = _content_cfg(args)
    if args.preview:
        content.preview(c).save(args.preview)
        print(f"wrote {args.preview}")
        return
    upload = content.build(c)
    if args.frame_delay and upload.kind == "gif":
        upload = replace(upload, delay_ms=args.frame_delay)
    with _panel(args) as p:
        start = time.monotonic()
        p.carousel([])
        p.upload(upload, args.chunk_delay, progress=_progress)
        print(f"{upload.kind}: {len(upload.payload)} bytes in {len(upload.frames)} writes, "
              f"{time.monotonic() - start:.1f}s")


def cmd_overlay(args):
    with _panel(args) as p:
        if args.off:
            p.overlay([])
            return
        widgets = args.widgets.split(",")
        for kind in (P.TEMPLATE_IMAGE, P.TEMPLATE_GIF):     # the style is stored per mode
            p.template(parse_color(args.color), _xy(args.image_pos), _xy(args.data_pos), True, kind)
        p.overlay(widgets, args.rotate)
        print(f"overlay: {', '.join(widgets)}; run `aorus-lcd stats` or the daemon to feed values")


def cmd_stats(args):
    from .sensors import NvmlSensors
    gpu = _gpu(args)
    sensors = NvmlSensors(gpu.pci)
    with _panel(args) as p:
        try:
            while True:
                s = sensors.read()
                p.feed(s)
                print(f"\r{s.temp}°C {s.clock}MHz {s.usage}% {s.power}W  ", end="", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()


def cmd_apply(args):
    cfg = C.load(args.config)
    if args.gpu:
        cfg.panel.gpu = args.gpu
    with open_transport(cfg.panel.transport, find_gpu(cfg.panel.gpu or None), cfg.panel.speed_khz) as t:
        p = Panel(t)
        p.probe()
        p.power(True)
        daemon.apply_content(p, cfg, daemon.state_dir() / "state.json", args.force, _progress)
        daemon.apply_overlay(p, cfg)


def cmd_daemon(args):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    daemon.run(args.config, args.force)


def cmd_query(args):
    with _panel(args) as p:
        raw = bytes.fromhex(args.hex)
        print(p.query(raw[0], raw[1:]).hex(" "))


# ---- firmware -----------------------------------------------------------------------

def cmd_fw_extract(args):
    image = firmware.extract(args.updater)
    info = firmware.identify(image)
    with open(args.output, "wb") as f:
        f.write(image)
    print(f"wrote {args.output} ({len(image)} bytes): "
          + (f"known image, LCD firmware {info['version']}" if info else "UNKNOWN image (flash will refuse it)"))


def _bootloader(t):
    where = firmware.detect(t)
    if where == "app":
        raise SystemExit("the LCD application is running; nothing to recover")
    if not where:
        raise SystemExit("no bootloader answers (a stalled bus needs a full power-off)")
    return where[1]


def cmd_fw_boot(args):
    with open_transport("nvrm", _gpu(args)) as t:
        address = _bootloader(t)
        reply = firmware.execute(t, address, firmware.plan_boot())
        print(f"bootloader replied {reply.hex(' ')}")
        ok = firmware.wait_for_app(t)
        print("application started" if ok else
              "still in the bootloader: the installed firmware is invalid; see `firmware flash`")
        sys.exit(0 if ok else 1)


def cmd_fw_flash(args):
    image = open(args.image, "rb").read()
    info = firmware.identify(image)
    if not info:
        raise SystemExit("refusing: not a known LCD firmware image (see KNOWN_IMAGES)")
    ops = firmware.plan_flash(image)
    if args.dry_run:
        for op in ops:
            print(op[0], op[1].hex() if op[0] == "write" else (op[1] if op[0] == "sleep" else ""))
        return
    gpu = _gpu(args)
    if (gpu.subvendor, gpu.subdevice) != info["subsystem"]:
        raise SystemExit(f"refusing: image is for {info['package']}, this card is {gpu.describe()}")
    if not args.yes:
        raise SystemExit("this rewrites the LCD's application firmware; re-run with --yes")
    with open_transport("nvrm", gpu) as t:
        address = _bootloader(t)
        print(f"flashing LCD firmware {info['version']} via bootloader {address:#04x} "
              "(do not power off) ...")
        reply = firmware.execute(t, address, ops, progress=_progress)
        print(f"bootloader replied {reply.hex(' ')}")
        ok = firmware.wait_for_app(t)
        print("LCD application is running" if ok else "the application did not start")
        sys.exit(0 if ok else 1)


# ---- parser -------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="aorus-lcd", description=__doc__)
    ap.add_argument("--version", action="version", version=f"aorus-lcd {__version__}")
    ap.add_argument("--transport", choices=TRANSPORTS, default="nvrm",
                    help="nvrm: NVIDIA RM at 400 kHz, no root (default); i2c-dev: /dev/i2c-N at 100 kHz")
    ap.add_argument("--gpu", default="", metavar="PCI", help="GPU PCI address (default: the Gigabyte card)")
    ap.add_argument("--speed", type=int, default=400, help="nvrm bus speed in kHz (default 400)")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("status", help="GPU, panel firmware, mode and overlay").set_defaults(func=cmd_status)
    p = sub.add_parser("power", help="turn the panel on or off")
    p.add_argument("state", choices=("on", "off"))
    p.set_defaults(func=cmd_power)
    p = sub.add_parser("mode", help="0-2 built-in stats, 3 image, 4 text, 5 gif, 6 chibi, 7 carousel")
    p.add_argument("mode", type=int, choices=range(8))
    p.set_defaults(func=cmd_mode)

    def content_cmd(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--preview", metavar="PNG", help="render to a PNG instead of uploading")
        p.add_argument("--chunk-delay", type=float, default=P.PACE_CHUNK, metavar="SEC")
        p.add_argument("--background", "--bg", dest="background", metavar="COLOR")
        p.set_defaults(func=cmd_content, frame_delay=None)
        return p

    p = content_cmd("logo", "a logo (SVG/PNG) recoloured, e.g. red on black")
    p.add_argument("source")
    p.add_argument("--color", default="#ff0000")
    p.add_argument("--scale", type=float, help="fraction of the panel to fill (default 0.8)")
    p.add_argument("--anchor", help="center, top, bottom, left, right, top-left, ...")
    p.add_argument("--offset", metavar="X,Y")
    p.add_argument("--pulse", action="store_true", help="animate as a breathing glow (GIF)")
    p = content_cmd("image", "a picture, fitted to 320x170")
    p.add_argument("source")
    p.add_argument("--fit", choices=("contain", "cover"))
    p = content_cmd("gif", "an animated GIF")
    p.add_argument("source")
    p.add_argument("--fit", choices=("contain", "cover"))
    p.add_argument("--frame-delay", type=int, metavar="MS")
    p = content_cmd("text", "a text message")
    p.add_argument("text")
    p.add_argument("--color", default="#ffffff")
    p.add_argument("--size", type=int)
    p.add_argument("--font")

    p = sub.add_parser("overlay", help="the firmware's GPU-stats widgets over the content")
    p.add_argument("--widgets", default="temp,usage,power", help=f"comma list of: {', '.join(P.WIDGETS)}")
    p.add_argument("--color", default="#ff0000")
    p.add_argument("--data-pos", default="146,64", metavar="X,Y",
                   help="widget position; the block is ~100 px tall, so y <= 64 on the 170 px panel")
    p.add_argument("--image-pos", default="0,0", metavar="X,Y")
    p.add_argument("--rotate", type=int, default=3, metavar="SEC", help="seconds per widget")
    p.add_argument("--off", action="store_true")
    p.set_defaults(func=cmd_overlay)
    p = sub.add_parser("stats", help="feed live GPU stats to the overlay (foreground)")
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("apply", help="apply a config file once (content + overlay)")
    p.add_argument("--config", default=C.DEFAULT_PATH)
    p.add_argument("--force", action="store_true", help="re-upload even if unchanged")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("daemon", help="apply a config and keep the stats fed (systemd service)")
    p.add_argument("--config", default=C.DEFAULT_PATH)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_daemon)
    p = sub.add_parser("query", help="send a read command, e.g. 'de' or 'eb 02', print the reply")
    p.add_argument("hex")
    p.set_defaults(func=cmd_query)

    fw = sub.add_parser("firmware", help="recover an LCD stuck in its bootloader").add_subparsers(
        dest="fw_command", required=True, metavar="ACTION")
    p = fw.add_parser("extract", help="pull the LCD firmware out of Gigabyte's official updater .exe")
    p.add_argument("updater")
    p.add_argument("-o", "--output", default="lcd-firmware.bin")
    p.set_defaults(func=cmd_fw_extract)
    fw.add_parser("boot", help="ask the bootloader to start the installed firmware (no flashing)") \
      .set_defaults(func=cmd_fw_boot)
    p = fw.add_parser("flash", help="reflash the LCD firmware through the bootloader")
    p.add_argument("image")
    p.add_argument("--dry-run", action="store_true", help="print every bus operation, touch nothing")
    p.add_argument("--yes", action="store_true", help="really flash")
    p.set_defaults(func=cmd_fw_flash)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (TransportError, LookupError, ValueError, RuntimeError, OSError) as e:
        sys.exit(f"aorus-lcd: {e}")


if __name__ == "__main__":
    main()
