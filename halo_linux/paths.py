"""XDG locations used by the Linux daemon and CLI."""
from __future__ import annotations

import os


def _xdg(var: str, fallback: str) -> str:
    return os.environ.get(var) or os.path.expanduser(fallback)


DATA_DIR = os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "halobattery")
CONFIG_DIR = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "halobattery")
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/halobattery-{os.getuid()}",
                           "halobattery")

HISTORY_PATH = os.path.join(DATA_DIR, "history.json")   # halo.history: drain rate / time left
SAMPLES_DB = os.path.join(DATA_DIR, "samples.db")       # every raw reading, for health analysis
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")    # latest poll, read by the CLI / tray
