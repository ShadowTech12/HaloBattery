"""Raw reading log in SQLite.

halo.history keeps only the current discharge and forgets it on every charge. Battery
health (runtime per cycle, charge-time trend, cycle count) needs the long record, so
every poll's reading is appended here. Rows are written only when something changed or
HEARTBEAT seconds passed, which keeps the file small (about 1-2 MB per device-year).
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Dict, Optional, Tuple

HEARTBEAT = 600.0   # s: write an unchanged reading at most this often

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts       REAL    NOT NULL,      -- unix time
    key      TEXT    NOT NULL,      -- DeviceStatus.key
    name     TEXT    NOT NULL,
    kind     TEXT    NOT NULL DEFAULT '',
    level    INTEGER,               -- 0..100, NULL = unknown
    charging INTEGER NOT NULL,
    online   INTEGER NOT NULL,
    coarse   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS samples_key_ts ON samples (key, ts);
"""


class SampleLog:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self._last: Dict[str, Tuple[Tuple, float]] = {}

    def record(self, st, now: Optional[float] = None) -> bool:
        """Append one reading; returns True when a row was written."""
        now = time.time() if now is None else now
        sig = (st.level, bool(st.charging), bool(st.online))
        prev = self._last.get(st.key)
        if prev and prev[0] == sig and now - prev[1] < HEARTBEAT:
            return False
        self.db.execute(
            "INSERT INTO samples (ts, key, name, kind, level, charging, online, coarse) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, st.key, st.name, st.kind or "", st.level, int(bool(st.charging)),
             int(bool(st.online)), int(bool(st.approx))))
        self._last[st.key] = (sig, now)
        return True

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
