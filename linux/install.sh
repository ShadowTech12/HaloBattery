#!/usr/bin/env bash
# Install the Halo Battery daemon as a systemd user service, running from this checkout.
#   linux/install.sh            install + enable + start
#   linux/install.sh --no-tray  same, without the tray icons
#   linux/install.sh --remove   stop + disable + remove
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/halobattery.service"
TRAY_UNIT="$UNIT_DIR/halobattery-tray.service"
BIN="$HOME/.local/bin/halo"

if [[ "${1:-}" == "--remove" ]]; then
    systemctl --user disable --now halobattery-tray.service halobattery.service 2>/dev/null || true
    rm -f "$UNIT" "$TRAY_UNIT" "$BIN"
    systemctl --user daemon-reload
    echo "removed"
    exit 0
fi

PY="$REPO/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
    echo "no venv at $REPO/.venv; create it first:" >&2
    echo "  uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
    exit 1
fi

mkdir -p "$UNIT_DIR" "$(dirname "$BIN")"
cat > "$UNIT" <<UNIT
[Unit]
Description=Halo Battery: wireless device battery monitor
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$REPO
ExecStart="$PY" -m halo_linux daemon
Restart=on-failure
RestartSec=10
# hidraw access comes from linux/70-halobattery.rules (uaccess), no extra privileges
NoNewPrivileges=yes

[Install]
WantedBy=graphical-session.target
UNIT

cat > "$TRAY_UNIT" <<UNIT
[Unit]
Description=Halo Battery tray icons
After=graphical-session.target halobattery.service
PartOf=graphical-session.target
Wants=halobattery.service

[Service]
Type=simple
WorkingDirectory=$REPO
ExecStart="$PY" -m halo_linux tray
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
UNIT

cat > "$BIN" <<SH
#!/bin/sh
cd "$REPO" && exec "$PY" -m halo_linux "\$@"
SH
chmod +x "$BIN"

systemctl --user daemon-reload
systemctl --user enable --now halobattery.service
# restart so an update of this checkout takes effect
systemctl --user restart halobattery.service
if [[ "${1:-}" != "--no-tray" ]]; then
    systemctl --user enable --now halobattery-tray.service
    systemctl --user restart halobattery-tray.service
fi
echo "installed: $UNIT"
echo "cli:       $BIN  (try: halo status)"
