"""halo: command line for the Linux daemon.

  halo daemon          run the poll loop (what the systemd user service runs)
  halo status [--json] show the latest reading of every device
  halo poll            poll once now, without the daemon, and print the result
  halo refresh         ask the running daemon to poll now
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
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    fn = {"status": cmd_status, "poll": cmd_poll, "refresh": cmd_refresh,
          "daemon": cmd_daemon}.get(args.cmd or "status")
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
