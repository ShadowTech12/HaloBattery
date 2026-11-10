"""Raw reading log in SQLite.

halo.history keeps only the current discharge and forgets it on every charge. Battery
health (runtime per cycle, charge-time trend, cycle count) needs the long record, so
every poll's reading is appended here. Rows are written only when something changed or
HEARTBEAT seconds passed, which keeps the file small (about 1-2 MB per device-year).
Voltage is stored with each row but is not part of "changed": it jitters by a few mV
every poll, and one value per HEARTBEAT is plenty to follow its trend.
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
    coarse   INTEGER NOT NULL DEFAULT 0,
    voltage_mv INTEGER                 -- NULL unless the device reports it
);
CREATE INDEX IF NOT EXISTS samples_key_ts ON samples (key, ts);
"""


class SampleLog:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}
        if "voltage_mv" not in cols:     # databases from before voltage was logged
            self.db.execute("ALTER TABLE samples ADD COLUMN voltage_mv INTEGER")
        self._last: Dict[str, Tuple[Tuple, float]] = {}

    def record(self, st, now: Optional[float] = None) -> bool:
        """Append one reading; returns True when a row was written."""
        now = time.time() if now is None else now
        sig = (st.level, bool(st.charging), bool(st.online))
        prev = self._last.get(st.key)
        if prev and prev[0] == sig and now - prev[1] < HEARTBEAT:
            return False
        self.db.execute(
            "INSERT INTO samples (ts, key, name, kind, level, charging, online, coarse, voltage_mv) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, st.key, st.name, st.kind or "", st.level, int(bool(st.charging)),
             int(bool(st.online)), int(bool(st.approx)), getattr(st, "voltage_mv", None)))
        self._last[st.key] = (sig, now)
        return True

    def last_resting_levels(self) -> Dict[str, int]:
        """key -> the level of the newest reading taken off the charger."""
        q = ("SELECT key, level FROM samples AS s WHERE charging = 0 AND online = 1 "
             "AND level IS NOT NULL AND ts = (SELECT MAX(ts) FROM samples WHERE key = s.key "
             "AND charging = 0 AND online = 1 AND level IS NOT NULL)")
        return {k: lv for k, lv in self.db.execute(q)}

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
