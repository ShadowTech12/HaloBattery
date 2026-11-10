<div align="center">

<img src="docs/logo.png" alt="" width="96">

# Halo Battery for Linux

**Battery levels and battery health of your wireless mouse, keyboard and headset, in the KDE Plasma tray.**<br>
One icon per device. No vendor software. Tracks charge cycles and battery wear over time.

[![Linux](https://img.shields.io/badge/Linux-KDE%20Plasma%206-1d99f3)](#install)
[![Upstream](https://img.shields.io/badge/fork%20of-HeyOkay%2FHaloBattery-lightgrey)](https://github.com/HeyOkay/HaloBattery)
[![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

This is a Linux port of [**HeyOkay/HaloBattery**](https://github.com/HeyOkay/HaloBattery), a Windows tray app that reads battery levels straight from the receivers of 30+ brands. The device protocols (the `providers/` folder) are the upstream work. This fork adds a Linux daemon, a CLI, a Qt tray for KDE Plasma and battery health analysis.

**On Windows?** Use the [upstream releases](https://github.com/HeyOkay/HaloBattery/releases/latest). The Windows app (`halo_battery.pyw`) is still in this repository, unchanged.

## Features

- **Tray icons.** A ring for each device fills with its level. It turns amber near your alert level and red below it, breathes green while charging and greys out while the device sleeps. Hover for level, time left and health. Click for the dashboard.
- **Notifications.** Low battery (once per discharge) and "fully charged, you can unplug it", through the desktop notification service.
- **Time left.** Hours of *use* left, measured from how fast the device actually drains. Sleep and switched-off time are not counted.
- **Battery health.** Every reading goes into a local SQLite log. From that log Halo Battery works out:
  - equivalent charge cycles, and cycles per day
  - runtime per full charge, compared with the first discharges it measured (health %)
  - runtime lost per cycle, and roughly how many days until the battery reaches 80 % of its original runtime
  - charge speed, and full-charge voltage for devices that report voltage (e.g. Logitech G915)
  - charging habits: hours a week left on the charger at 100 %, and how many deep discharges
- **Dashboard.** A 7-day level chart and the health figures for each device.
- **CLI.** `halo status`, `halo health`, both with `--json` for scripts and status bars.
- **Safe on the receiver.** Reads through `/dev/hidraw` alongside the kernel driver, so your keyboard and mouse keep working, and it runs alongside Solaar.

## Tested hardware

| Device | Connection | Level | Voltage |
|---|---|---|---|
| Logitech MX Master 3S | Logi Bolt receiver | ✅ | – |
| Logitech G915 | Lightspeed receiver | ✅ | ✅ |
| Audeze Maxwell | USB dongle | ✅ | – |
| Xbox controller | Bluetooth | 🚧 planned (UPower) | – |

Other devices from the [upstream list](docs/devices.md) that use a USB receiver or cable *should* work, because their providers only use hidapi. They are untested on Linux. Bluetooth devices are not read yet: upstream's Bluetooth code is Windows-only.

Tested on Arch Linux with KDE Plasma 6.6 (Wayland). The tray uses StatusNotifierItem, so other trays that support it (GNOME with the AppIndicator extension, waybar, etc.) should work too.

## Install

You need Python 3.10+, [uv](https://github.com/astral-sh/uv) (or plain `venv` + `pip`), and `notify-send` (package `libnotify`) for notifications.

```bash
git clone https://github.com/ShadowTech12/HaloBattery.git
cd HaloBattery
git checkout linux-port

# 1. Python environment (uv brings its own Python; the system Python is not touched)
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 2. Let your user read the receivers and headset (hidraw access for the seat user)
sudo install -m644 linux/70-halobattery.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --action=add --subsystem-match=hidraw && sudo udevadm settle

# 3. Install and start the systemd user services (daemon + tray) and the `halo` command
linux/install.sh
```

`linux/install.sh` runs the app from the folder you cloned. After you `git pull`, run it again to restart the services. Options:

- `linux/install.sh --no-tray`: the daemon only, no tray icons (useful for headless setups or status bars).
- `linux/install.sh --remove`: stop and remove the services and the `halo` command.

The udev rule is not removed by `--remove`. To remove it: `sudo rm /etc/udev/rules.d/70-halobattery.rules`.

`70-halobattery.rules` grants access to Logitech (`046d`) and Audeze (`3329`) devices. For another brand, add a line with its USB vendor id (find it with `lsusb`).

## Usage

```
halo status [--json]   latest reading of every device (from the daemon)
halo health [--json]   battery health, cycles, runtime trend, expected life
halo poll [-v]         poll once now without the daemon; -v prints provider diagnostics
halo refresh           ask the running daemon to poll now
halo tray [--dashboard]  run the tray (what halobattery-tray.service runs)
halo daemon            run the poll loop (what halobattery.service runs)
```

```
$ halo status
DEVICE                                        LEVEL  STATE       LEFT  DRAIN
Audeze Maxwell                                96%    on battery
G915 WIRELESS RGB MECHANICAL GAMING KEYBOARD  75%    charging
MX Master 3S                                  50%    on battery
```

LEFT and DRAIN fill in after about 30 minutes of use and a 3 % drop since the last charge.

Services and logs:

```bash
systemctl --user status halobattery halobattery-tray
journalctl --user -u halobattery -u halobattery-tray -f
```

## Configuration

Optional. Put it in `~/.config/halobattery/config.json`, then run `systemctl --user restart halobattery halobattery-tray`.

```json
{
  "interval": 60,
  "low": 20,
  "notify": true,
  "notify_full": true,
  "disabled": [],
  "miss_polls": 2
}
```

| Key | Meaning |
|---|---|
| `interval` | seconds between polls (5–3600) |
| `low` | % at or below which the low-battery notification fires and the ring turns red |
| `notify` / `notify_full` | low-battery / fully-charged notifications |
| `disabled` | provider class names to skip, e.g. `["RazerProvider"]` (see `providers/__init__.py`) |
| `miss_polls` | polls a device may be missing before its icon is removed |

## How battery health is estimated

Devices report a percentage of their *current* capacity, so a worn battery still says 100 % when full. Wear shows up in other ways, and Halo Battery looks for them:

1. **Runtime per charge.** For each discharge, Halo Battery fits a line through the level against *time in use*. Time asleep, time switched off and gaps while the PC sleeps are left out. From that line it gets hours per 100 %. The median of the first 3 measured discharges is the baseline. The median of the last 3 is "now". **Health % = now / baseline.**
2. **Fade per cycle.** It fits a line through runtime against equivalent cycles (total % discharged ÷ 100). With the cycles you use per day, that gives the days until runtime drops to 80 % of the baseline. Until a fade can be measured, the projection assumes a typical Li-ion life of about 500 cycles to 80 %.
3. **Charge rate.** A smaller battery fills faster, so a rising %/h while charging means wear. The rate is measured below 95 %, because the slow top-off at the end would hide the trend.
4. **Full-charge voltage** for devices that report millivolts (Logitech HID++ feature 0x1001).

Status: **learning** (fewer than 4 measured discharges), **good** (≥ 90 %), **fair** (80–90 %), **worn** (< 80 %).

**Limits.** These are estimates. How hard you use a device changes how fast it drains: headset volume and ANC, keyboard backlight, mouse polling rate. Rolling medians smooth this out, but not completely. A discharge is measured only once it has covered at least 8 points and 30 minutes of use. Expect the numbers to mean something after a few weeks of normal use. The baseline is the battery *when Halo Battery first saw it*, not when it was new.

## Files

| Path | Contents |
|---|---|
| `~/.local/share/halobattery/samples.db` | every reading (SQLite, ~1–2 MB per device-year): the health history |
| `~/.local/share/halobattery/history.json` | the current discharge, for "time left" |
| `~/.config/halobattery/config.json` | settings |
| `$XDG_RUNTIME_DIR/halobattery/state.json` | the latest poll, read by the CLI and the tray |
| `~/.config/systemd/user/halobattery{,-tray}.service` | the services written by `install.sh` |

Delete `samples.db` to start the health history over, e.g. after replacing a battery.

## Troubleshooting

- **A device is missing.** Run `halo poll -v`. If the provider says it could not open the device, check the permissions with `ls -l /dev/hidraw*`. Your devices should show a `+` (ACL) after the mode. If they don't, install the udev rule and run the `udevadm trigger` line from [Install](#install) again.
- **Audeze shows no level.** The headset must be on and linked to the dongle. On Linux the dongle always calls itself "Audeze Maxwell Dongle", so Halo Battery tries a read on every poll. While the headset is off, each poll takes about 1.5 s longer.
- **Keyboard or mouse stopped working while testing your own scripts.** You used hidapi's libusb backend (`import hid` on Linux), which detaches the kernel driver. Unplug and replug the receiver. Halo Battery uses the `hidraw` backend (`providers/__init__.py`) to avoid exactly this.
- **No tray icons.** Check that your tray supports StatusNotifierItem and that the service is running: `systemctl --user status halobattery-tray`.
- **"GLIBC_2.xx not found" when creating the venv.** Your system Python is newer than your glibc (a partial upgrade). `uv venv --python 3.13` uses uv's own Python and avoids it.

## Development

```bash
.venv/bin/python -m pytest -q --continue-on-collection-errors tests
```

The Linux code lives in `halo_linux/` (daemon, CLI, health, tray) and `linux/` (udev rule, installer). Tests for it are in `tests/test_linux_halo.py`. Some upstream tests cover the Windows tray, Bluetooth and XInput. On Linux, 13 of them can't load and 3 fail.

To add a device, follow upstream's [CONTRIBUTING.md](.github/CONTRIBUTING.md). New protocol support belongs upstream, so both platforms get it. Linux-specific fixes belong here.

### Roadmap

- [ ] Bluetooth devices through UPower / BlueZ (Xbox controller first)
- [ ] Test more receivers on Linux and widen the udev rule
- [ ] AUR package (`PKGBUILD`)
- [ ] Waybar / Polybar output module

## Credits

All device protocols come from [HeyOkay/HaloBattery](https://github.com/HeyOkay/HaloBattery). Upstream got them from OpenRazer, HeadsetControl, Solaar, rivalcfg, G-Helper and other open projects, and from captures and test runs sent by its community. **[Full credits →](docs/credits.md)**

MIT licensed, see [LICENSE](LICENSE). The original copyright is kept.
