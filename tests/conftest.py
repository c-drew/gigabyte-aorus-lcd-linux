"""Shared fixtures: a stand-in ttfx binary."""
import stat
import textwrap

import pytest

from aorus_lcd import ttfx

ESC = "\x1b"


def _frame(rows):
    """A parity-dump frame: rows of (char, rgb or None)."""
    lines = []
    for row in rows:
        line = ""
        for ch, rgb in row:
            line += f"{ESC}[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{ch}{ESC}[0m" if rgb else ch
        lines.append(line)
    return "\n".join(lines).encode()


@pytest.fixture
def fake_ttfx(tmp_path):
    """A stand-in ttfx: `fx` emits 5 frames (the last one draws a block), `big`
    emits 400, `huge` one oversized frame, `slow` hangs."""
    frames = [_frame([[(" ", None)] * 6] * 3) for _ in range(4)]
    frames.append(_frame([[("█", (255, 0, 0))] * 6, [("a", None)] * 6, [(" ", None)] * 6]))
    dump = b"".join(f"{len(f)}\n".encode() + f + b"\n" for f in frames)
    (tmp_path / "fx.bin").write_bytes(dump)
    (tmp_path / "big.bin").write_bytes(dump * 80)
    (tmp_path / "huge.bin").write_bytes(b"99999999\n" + b"x" * 64)
    script = tmp_path / "ttfx"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        for last; do :; done
        case "$last" in
          --version) echo "ttfx 9.9.9" ;;
          --help) printf 'Commands:\\n  fx  a\\n  big  b\\n  huge  c\\n  slow  d\\n  help  h\\nOptions:\\n' ;;
          slow) cat >/dev/null; sleep 30 ;;
          print) cat >/dev/null; cat "{tmp_path}/fx.bin" ;;
          *) cat >/dev/null; cat "{tmp_path}/$last.bin" ;;
        esac
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    ttfx.version.cache_clear()
    ttfx.effects.cache_clear()
    return str(script)


