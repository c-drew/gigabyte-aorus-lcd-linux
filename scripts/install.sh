#!/usr/bin/env bash
# Install aorus-lcd system-wide and start it at every boot.
#   sudo scripts/install.sh [--ttfx PATH] [CONFIG_DIR]
# CONFIG_DIR (optional): a directory with your config.toml and any logo/image
# files it references; they are copied to /etc/aorus-lcd/.
# --ttfx PATH (optional): a ttfx binary for the panel to render with instead of
# the system's (e.g. a newer build); it stays installed until you remove
# /opt/aorus-lcd/bin/ttfx. The desktop screensaver keeps the system's ttfx.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
ttfx=""
args=()
while (( $# )); do
    case "$1" in
        --ttfx) ttfx="${2:?--ttfx needs the path of a ttfx binary}"; shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
set -- ${args[@]+"${args[@]}"}
if [[ -n $ttfx && ! ( -f $ttfx && -x $ttfx ) ]]; then
    echo "--ttfx: $ttfx is not an executable file" >&2
    exit 1
fi
repo=$(cd "$(dirname "$0")/.." && pwd)
prefix=/opt/aorus-lcd

# Build from a scratch copy so root never writes into the checkout.
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
cp -r "$repo/pyproject.toml" "$repo/README.md" "$repo/LICENSE" "$repo/aorus_lcd" "$src/"
python3 -m venv "$prefix"
"$prefix/bin/pip" install --quiet --upgrade "$src"
ln -sf "$prefix/bin/aorus-lcd" /usr/local/bin/aorus-lcd
if [[ -n $ttfx ]]; then
    install -m 755 "$ttfx" "$prefix/bin/ttfx"
    echo "the panel renders with the ttfx from $ttfx (remove $prefix/bin/ttfx to go back to the system's)"
fi

install -d -m 755 /etc/aorus-lcd
if [[ $# -ge 1 ]]; then
    install -m 644 "$1"/* /etc/aorus-lcd/
elif [[ ! -e /etc/aorus-lcd/config.toml ]]; then
    install -m 644 "$repo/examples/config.toml" /etc/aorus-lcd/config.toml
fi
"$prefix/bin/python" -I -c "import aorus_lcd.config as c; c.load('/etc/aorus-lcd/config.toml')"

install -m 644 "$repo/systemd/aorus-lcd.service" /etc/systemd/system/aorus-lcd.service
install -D -m 644 "$repo/desktop/aorus-lcd.desktop" /usr/local/share/applications/aorus-lcd.desktop
systemctl daemon-reload
systemctl enable aorus-lcd.service
systemctl restart aorus-lcd.service
echo "installed: web UI at http://127.0.0.1:5090/ (\"AORUS LCD\" in your app launcher)"
echo "logs: journalctl -u aorus-lcd -f"
