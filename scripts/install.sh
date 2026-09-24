#!/usr/bin/env bash
# Install aorus-lcd system-wide and start it at every boot.
#   sudo scripts/install.sh [CONFIG_DIR]
# CONFIG_DIR (optional): a directory with your config.toml and any logo/image
# files it references; they are copied to /etc/aorus-lcd/.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
repo=$(cd "$(dirname "$0")/.." && pwd)
prefix=/opt/aorus-lcd

python3 -m venv "$prefix"
"$prefix/bin/pip" install --quiet --upgrade "$repo"
ln -sf "$prefix/bin/aorus-lcd" /usr/local/bin/aorus-lcd

install -d -m 755 /etc/aorus-lcd
if [[ $# -ge 1 ]]; then
    install -m 644 "$1"/* /etc/aorus-lcd/
elif [[ ! -e /etc/aorus-lcd/config.toml ]]; then
    install -m 644 "$repo/examples/config.toml" /etc/aorus-lcd/config.toml
fi
"$prefix/bin/python" -c "import aorus_lcd.config as c; c.load('/etc/aorus-lcd/config.toml')"

install -m 644 "$repo/systemd/aorus-lcd.service" /etc/systemd/system/aorus-lcd.service
systemctl daemon-reload
systemctl enable aorus-lcd.service
systemctl restart aorus-lcd.service
echo "installed; logs: journalctl -u aorus-lcd -f"
