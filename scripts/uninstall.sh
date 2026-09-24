#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
systemctl disable --now aorus-lcd.service 2>/dev/null || true
rm -f /etc/systemd/system/aorus-lcd.service /usr/local/bin/aorus-lcd /usr/local/share/applications/aorus-lcd.desktop
rm -rf /opt/aorus-lcd
systemctl daemon-reload
echo "removed (kept /etc/aorus-lcd)"
