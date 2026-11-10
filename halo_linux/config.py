"""Settings in ~/.config/halobattery/config.json (all optional)."""
from __future__ import annotations

import json
import logging

from . import paths

log = logging.getLogger("halobattery")

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
