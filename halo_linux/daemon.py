"""The poll loop: read every provider, keep history, write state, send notifications."""
from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Dict, List, Optional

import providers
from halo import history
from providers.base import DeviceStatus, Provider

from . import paths
from .samples import SampleLog

log = logging.getLogger("halobattery")

# Windows-only providers (WinRT Bluetooth, XInput, Windows.Gaming.Input). Bluetooth
# devices on Linux will come from UPower instead.
WINDOWS_ONLY = {"BluetoothProvider", "XInputProvider"}

DEFAULTS = {
    "interval": 60,          # s between polls
    "low": 20,               # % at or below which a low-battery notification is sent
    "notify": True,
    "notify_full": True,     # tell when a charging device reaches 100 %
    "disabled": [],          # provider class names to skip, e.g. ["RazerProvider"]
    "miss_polls": 2,         # a device missing this many polls in a row is dropped
}


def load_config(path: str = paths.CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("config %s ignored: %s", path, e)
    cfg["interval"] = max(5, min(3600, int(cfg["interval"])))
    return cfg


def provider_classes(disabled=()) -> List[type]:
    out = []
    for name, obj in vars(providers).items():
        if (inspect.isclass(obj) and issubclass(obj, Provider) and obj is not Provider
                and name not in WINDOWS_ONLY and name not in disabled):
            out.append(obj)
    return sorted(out, key=lambda c: c.__name__)


def notify(summary: str, body: str = "", urgency: str = "normal", icon: str = "battery") -> None:
    """Desktop notification through notify-send (libnotify); silently skipped without it."""
    exe = shutil.which("notify-send")
    if not exe:
        log.info("notify: %s %s", summary, body)
        return
    try:
        subprocess.run([exe, "-a", "Halo Battery", "-u", urgency, "-i", icon, summary, body],
                       timeout=5, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("notify-send failed: %s", e)


class Daemon:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or load_config()
        self.providers: List[Provider] = []
        for cls in provider_classes(self.cfg["disabled"]):
            try:
                self.providers.append(cls())
            except Exception as e:  # a broken provider must not stop the others
                log.warning("provider %s not started: %s", cls.__name__, e)
        self.history = history.History(paths.HISTORY_PATH)
        self.history.load()
        self.samples = SampleLog(paths.SAMPLES_DB)
        self.devices: Dict[str, dict] = {}     # key -> last state entry
        self.misses: Dict[str, int] = {}
        self.alerted_low: Dict[str, bool] = {}
        self.alerted_full: Dict[str, bool] = {}
        self.stop = threading.Event()
        self.wake = threading.Event()

    # -- one poll -------------------------------------------------------------------
    def poll_all(self) -> List[DeviceStatus]:
        results: List[DeviceStatus] = []
        for p in self.providers:
            try:
                results.extend(p.poll())
            except Exception as e:
                log.warning("%s poll failed: %s", type(p).__name__, e)
        # one entry per key; prefer a reading with a level
        best: Dict[str, DeviceStatus] = {}
        for st in results:
            cur = best.get(st.key)
            if cur is None or (cur.level is None and st.level is not None):
                best[st.key] = st
        return list(best.values())

    def tick(self) -> None:
        now = time.time()
        seen = set()
        for st in self.poll_all():
            seen.add(st.key)
            self.misses.pop(st.key, None)
            self.history.record(st.key, st.level, st.charging, st.online, now,
                                coarse=bool(st.approx))
            self.samples.record(st, now)
            left = self.history.seconds_left(st.key, st.level)
            entry = dataclasses.asdict(st)
            entry.update(seen=now, seconds_left=left,
                         rate_per_hour=self._rate(st.key))
            self.devices[st.key] = entry
            self._alerts(st)
        for key in list(self.devices):
            if key not in seen:
                self.misses[key] = self.misses.get(key, 0) + 1
                if self.misses[key] >= self.cfg["miss_polls"]:
                    self.devices.pop(key, None)
                    self.misses.pop(key, None)
                else:
                    self.devices[key]["online"] = False
        self.samples.commit()
        self.history.save()
        self.write_state(now)

    def _rate(self, key: str) -> Optional[float]:
        r = self.history.rate(key)          # % per second of use, or None
        return round(r * 3600, 2) if r else None

    def _alerts(self, st: DeviceStatus) -> None:
        if not self.cfg["notify"] or st.level is None or not st.online:
            return
        low = self.cfg["low"]
        if st.charging:
            self.alerted_low[st.key] = False
            if st.level >= 100 and self.cfg["notify_full"] and not self.alerted_full.get(st.key):
                notify(f"{st.name} is fully charged", "You can unplug it.")
                self.alerted_full[st.key] = True
            return
        self.alerted_full[st.key] = False
        if st.level <= low and not self.alerted_low.get(st.key):
            left = self.history.seconds_left(st.key, st.level)
            extra = f", {history.format_left(left)}" if left else ""
            notify(f"{st.name} battery low", f"{st.level}%{extra}",
                   urgency="critical" if st.level <= 5 else "normal", icon="battery-low")
            self.alerted_low[st.key] = True
        elif st.level > low + 5:
            self.alerted_low[st.key] = False

    def write_state(self, now: float) -> None:
        os.makedirs(paths.RUNTIME_DIR, exist_ok=True)
        tmp = paths.STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": now, "pid": os.getpid(), "interval": self.cfg["interval"],
                       "devices": sorted(self.devices.values(), key=lambda d: d["name"].lower())},
                      f, indent=1)
        os.replace(tmp, paths.STATE_PATH)

    # -- loop -----------------------------------------------------------------------
    def run(self) -> None:
        def _stop(*_):
            self.stop.set()
            self.wake.set()      # end the wait between polls at once
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGUSR1, lambda *_: self.wake.set())   # poll now
        log.info("started with %d providers, interval %ss", len(self.providers),
                 self.cfg["interval"])
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:
                    log.exception("poll failed")
                wait = max(1.0, self.cfg["interval"] - (time.monotonic() - started))
                self.wake.wait(wait)
                self.wake.clear()
        finally:
            self.history.save(force=True)
            self.samples.close()
            try:
                os.remove(paths.STATE_PATH)
            except OSError:
                pass
            log.info("stopped")
