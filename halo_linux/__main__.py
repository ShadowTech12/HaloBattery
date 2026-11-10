"""halo: command line for the Linux daemon.

  halo daemon          run the poll loop (what the systemd user service runs)
  halo status [--json] show the latest reading of every device
  halo poll            poll once now, without the daemon, and print the result
  halo refresh         ask the running daemon to poll now
  halo health [--json] battery health: cycles, runtime trend, expected life
  halo tray            show tray icons (KDE / any StatusNotifierItem tray)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

from . import paths


def _fmt_rows(devices) -> str:
    from halo import history
    if not devices:
        return "No devices found."
    rows = [("DEVICE", "LEVEL", "STATE", "LEFT", "DRAIN")]
    for d in devices:
        level = d.get("approx") or (f"{d['level']}%" if d.get("level") is not None else "?")
        state = ("charging" if d.get("charging") else "on battery") if d.get("online") else "asleep/off"
        left = history.format_left(d["seconds_left"]) if d.get("seconds_left") else ""
        rate = f"{d['rate_per_hour']}%/h" if d.get("rate_per_hour") else ""
        rows.append((d["name"], level, state, left, rate))
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def cmd_status(args) -> int:
    try:
        with open(paths.STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        print("Daemon not running (no state file). Start it: systemctl --user start halobattery",
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(state, indent=1))
        return 0
    print(_fmt_rows(state["devices"]))
    age = time.time() - state["updated"]
    print(f"\nupdated {int(age)} s ago")
    return 0


def cmd_poll(args) -> int:
    from .daemon import Daemon
    import dataclasses
    d = Daemon()
    devs = [dict(dataclasses.asdict(s), seconds_left=None, rate_per_hour=None)
            for s in d.poll_all()]
    if args.json:
        print(json.dumps(devs, indent=1))
    else:
        print(_fmt_rows(sorted(devs, key=lambda x: x["name"].lower())))
    if args.verbose:
        for p in d.providers:
            for line in p.diagnostics():
                print(line)
    return 0


def cmd_refresh(args) -> int:
    try:
        with open(paths.STATE_PATH, encoding="utf-8") as f:
            pid = json.load(f)["pid"]
        os.kill(pid, signal.SIGUSR1)
    except (FileNotFoundError, ProcessLookupError, KeyError):
        print("Daemon not running.", file=sys.stderr)
        return 1
    return 0


def _h(v, unit="", nd=1):
    if v is None:
        return "-"
    return f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}"


def cmd_health(args) -> int:
    from . import health
    reps = health.report(paths.SAMPLES_DB)
    if args.json:
        print(json.dumps([r.as_dict() for r in reps], indent=1))
        return 0
    if not reps:
        print("No data yet. The daemon logs readings to " + paths.SAMPLES_DB)
        return 0
    for r in reps:
        print(f"{r.name}  [{r.status}]")
        print(f"  observed      {r.days_observed:g} days, {r.cycles:g} cycles"
              + (f" ({r.cycles_per_day:g}/day)" if r.cycles_per_day else ""))
        print(f"  discharges    {r.discharges} ({r.measured_discharges} measured), "
              f"charges {r.charges}, deep {r.deep_discharges}")
        print(f"  runtime       baseline {_h(r.baseline_runtime_h, ' h')}, "
              f"now {_h(r.current_runtime_h, ' h')} per 100%")
        print(f"  health        {_h(r.health_pct, '%')}"
              + (f", fade {r.fade_per_cycle_h:+g} h/cycle" if r.fade_per_cycle_h is not None else ""))
        if r.charge_rate_first or r.charge_rate_recent:
            print(f"  charge rate   first {_h(r.charge_rate_first, '%/h')}, "
                  f"recent {_h(r.charge_rate_recent, '%/h')}")
        if r.full_mv_first:
            print(f"  full voltage  first {r.full_mv_first} mV, recent {r.full_mv_recent} mV")
        if r.days_to_80 is not None:
            print(f"  to 80%        ~{r.days_to_80:g} days ({r.projection_basis})")
        if r.hours_full_on_charger_per_week is not None:
            print(f"  at 100% plugged  {r.hours_full_on_charger_per_week:g} h/week")
        for a in r.advice:
            print(f"  * {a}")
        print()
    print("Estimates; they firm up after a few weeks of normal use.")
    return 0


def cmd_tray(args) -> int:
    from .tray import main as tray_main
    return tray_main(dashboard=args.dashboard)


def cmd_daemon(args) -> int:
    from .daemon import Daemon
    Daemon().run()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="halo", description="Battery levels of wireless devices.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status", help="latest reading from the daemon")
    s.add_argument("--json", action="store_true")
    p = sub.add_parser("poll", help="poll once now, without the daemon")
    p.add_argument("--json", action="store_true")
    sub.add_parser("refresh", help="ask the daemon to poll now")
    sub.add_parser("daemon", help="run the poll loop")
    h = sub.add_parser("health", help="battery health and expected life")
    h.add_argument("--json", action="store_true")
    t = sub.add_parser("tray", help="tray icons")
    t.add_argument("--dashboard", action="store_true", help="also open the dashboard")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    fn = {"status": cmd_status, "poll": cmd_poll, "refresh": cmd_refresh,
          "daemon": cmd_daemon, "health": cmd_health, "tray": cmd_tray}.get(args.cmd or "status")
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
