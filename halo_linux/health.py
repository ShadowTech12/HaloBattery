"""Battery health from the raw sample log (samples.py).

What a device tells us is a percentage of its *current* capacity, so a worn battery
still reads 100 % when full. Wear shows up instead as:

  * shorter runtime: hours of use per 100 % of charge, measured over each discharge;
  * faster charging: % per hour while on the charger rises as there is less to fill;
  * for devices that report voltage (Logitech 0x1001), a lower voltage when full.

From the log this module derives, per device:

  sessions        discharges (on battery, between charges) and charges
  cycles          equivalent full cycles: total % discharged / 100
  runtime_h       hours of use per 100 %, least-squares fit per discharge session
  health_pct      recent runtime / baseline runtime (median of the first sessions)
  fade_per_cycle  runtime lost per cycle, fitted over all sessions
  projection      days until runtime reaches 80 % of baseline, from the fade and the
                  cycles per day; without a measurable fade, from a typical Li-ion life
                  of TYPICAL_CYCLES cycles to 80 %
  habits          hours a week left on the charger at 100 %, deep discharges (<= 5 %)

"Use" time counts only while the device is awake and on battery: the interval after a
row counts with that row's state, capped at MAX_GAP (the daemon was not running, or
the PC slept). Everything here is an estimate, and it needs a few weeks of data.
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

MAX_GAP = 900.0            # s: the most one interval between rows may count
RESET_RISE = 3             # a rise this large without "charging" means it was charged
MIN_SESSION_DROP = 8       # percentage points a discharge must cover to be measured
MIN_SESSION_USE = 1800.0   # s of use a discharge must cover to be measured
MIN_CHARGE_RISE = 15       # percentage points a charge must cover to give a rate
BASELINE_SESSIONS = 3      # the first N measured discharges set the baseline
RECENT_SESSIONS = 3        # the last N give the current runtime
MIN_SESSIONS_FOR_HEALTH = BASELINE_SESSIONS + 1
TYPICAL_CYCLES = 500       # equivalent cycles to ~80 % for a typical Li-ion cell
END_OF_LIFE = 0.80         # runtime fraction of baseline taken as "worn"
DEEP_DISCHARGE = 5         # % at or below which a discharge counts as deep
DAY = 86400.0


@dataclass
class Row:
    ts: float
    level: Optional[int]
    charging: bool
    online: bool
    coarse: bool = False
    voltage_mv: Optional[int] = None


@dataclass
class Discharge:
    start: float
    end: float
    start_level: int
    end_level: int
    use_s: float
    runtime_h: Optional[float] = None    # hours per 100 %, None when too short to measure
    cycle_at: float = 0.0                # equivalent cycles before this session


@dataclass
class Charge:
    start: float
    end: float
    start_level: int
    end_level: int
    rate_per_h: Optional[float] = None   # % per hour
    full_mv: Optional[int] = None        # highest voltage seen while charging


@dataclass
class Health:
    key: str
    name: str
    kind: str
    first_seen: float
    last_seen: float
    days_observed: float
    cycles: float
    cycles_per_day: Optional[float]
    discharges: int
    measured_discharges: int
    charges: int
    baseline_runtime_h: Optional[float]
    current_runtime_h: Optional[float]
    health_pct: Optional[float]
    fade_per_cycle_h: Optional[float]
    charge_rate_first: Optional[float]
    charge_rate_recent: Optional[float]
    full_mv_first: Optional[int]
    full_mv_recent: Optional[int]
    days_to_80: Optional[float]
    projection_basis: str
    hours_full_on_charger_per_week: Optional[float]
    deep_discharges: int
    status: str                          # learning / good / fair / worn
    advice: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# -- maths ---------------------------------------------------------------------------
def slope(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Least-squares slope of ys against xs; None without spread in xs."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def _median(v: List[float]) -> Optional[float]:
    return statistics.median(v) if v else None


# -- sessions ------------------------------------------------------------------------
def split_sessions(rows: List[Row]) -> Tuple[List[Discharge], List[Charge], float, float]:
    """(discharges, charges, total % discharged, seconds at 100 % on the charger)."""
    discharges: List[Discharge] = []
    charges: List[Charge] = []
    dropped = 0.0
    full_on_charger = 0.0
    cur_d: Optional[dict] = None
    cur_c: Optional[dict] = None
    prev_level: Optional[int] = None

    def close_d():
        nonlocal cur_d
        if cur_d and len(cur_d["pts"]) >= 2:
            pts = cur_d["pts"]
            d = Discharge(pts[0][1], pts[-1][1], pts[0][2], pts[-1][2], pts[-1][0])
            drop = d.start_level - d.end_level
            if drop >= MIN_SESSION_DROP and d.use_s >= MIN_SESSION_USE:
                s = slope([p[0] for p in pts], [p[2] for p in pts])   # % per s of use
                if s is not None and s < 0:
                    d.runtime_h = round(100.0 / -s / 3600.0, 2)
            discharges.append(d)
        cur_d = None

    def close_c():
        nonlocal cur_c
        if cur_c and len(cur_c["pts"]) >= 2:
            pts = cur_c["pts"]
            c = Charge(pts[0][0], pts[-1][0], pts[0][1], pts[-1][1], full_mv=cur_c["mv"])
            # the rate comes from the part below 95 %: the last few % are a slow
            # constant-voltage top-off that would hide the trend
            body = [p for p in pts if p[1] < 95]
            if len(body) >= 2 and body[-1][1] - body[0][1] >= MIN_CHARGE_RISE:
                s = slope([p[0] for p in body], [p[1] for p in body])
                if s and s > 0:
                    c.rate_per_h = round(s * 3600.0, 1)
            charges.append(c)
        cur_c = None

    for i, r in enumerate(rows):
        nxt = rows[i + 1].ts if i + 1 < len(rows) else r.ts
        dt = min(max(nxt - r.ts, 0.0), MAX_GAP)
        if r.level is None:
            continue
        if r.charging:
            close_d()
            if cur_c is None:
                cur_c = {"pts": [], "mv": None}
            cur_c["pts"].append((r.ts, r.level))
            if r.voltage_mv:
                cur_c["mv"] = max(cur_c["mv"] or 0, r.voltage_mv)
            if r.level >= 100:
                full_on_charger += dt
        else:
            close_c()
            if prev_level is not None and r.level >= prev_level + RESET_RISE:
                close_d()            # charged while we were not looking
            if prev_level is not None and r.level < prev_level and not r.coarse:
                dropped += prev_level - r.level
            if r.online:
                if cur_d is None:
                    cur_d = {"pts": [], "use": 0.0}
                cur_d["pts"].append((cur_d["use"], r.ts, r.level))
                cur_d["use"] += dt
        prev_level = r.level
    close_d()
    close_c()
    return discharges, charges, dropped, full_on_charger


def analyse(key: str, name: str, kind: str, rows: List[Row]) -> Health:
    rows = sorted(rows, key=lambda r: r.ts)
    discharges, charges, dropped, full_s = split_sessions(rows)
    first, last = (rows[0].ts, rows[-1].ts) if rows else (0.0, 0.0)
    days = max((last - first) / DAY, 0.0)

    # cycles at each session start, for the fade fit
    acc = 0.0
    for d in discharges:
        d.cycle_at = round(acc / 100.0, 3)
        acc += max(d.start_level - d.end_level, 0)
    cycles = round(dropped / 100.0, 2)
    cpd = round(cycles / days, 3) if days >= 1 else None

    measured = [d for d in discharges if d.runtime_h]
    baseline = _median([d.runtime_h for d in measured[:BASELINE_SESSIONS]])
    current = _median([d.runtime_h for d in measured[-RECENT_SESSIONS:]])
    health = None
    fade = None
    if len(measured) >= MIN_SESSIONS_FOR_HEALTH and baseline:
        health = round(min(current / baseline, 1.5) * 100.0, 1)
        f = slope([d.cycle_at for d in measured], [d.runtime_h for d in measured])
        fade = round(f, 4) if f is not None else None

    days_to_80 = None
    basis = ""
    if fade is not None and fade < 0 and baseline and current and cpd:
        target = END_OF_LIFE * baseline
        cycles_left = max(current - target, 0.0) / -fade
        days_to_80 = round(cycles_left / cpd, 0)
        basis = "measured runtime fade"
    elif cpd:
        days_to_80 = round(max(TYPICAL_CYCLES - cycles, 0.0) / cpd, 0)
        basis = f"typical Li-ion life ({TYPICAL_CYCLES} cycles to 80%)"

    rated = [c for c in charges if c.rate_per_h]
    mvs = [c.full_mv for c in charges if c.full_mv]
    deep = sum(1 for d in discharges if d.end_level <= DEEP_DISCHARGE)
    full_week = round(full_s / 3600.0 / days * 7, 1) if days >= 1 else None

    if health is None:
        status = "learning"
    elif health >= 90:
        status = "good"
    elif health >= 80:
        status = "fair"
    else:
        status = "worn"

    advice = []
    if full_week is not None and full_week > 24:
        advice.append(f"Left on the charger at 100% about {full_week:g} h a week; "
                      "unplug once full to slow wear.")
    if deep >= 3 and len(discharges) and deep / len(discharges) > 0.3:
        advice.append(f"{deep} of {len(discharges)} discharges went to {DEEP_DISCHARGE}% or less; "
                      "charging around 20% is gentler on the cell.")
    if status == "worn":
        advice.append("Runtime is below 80% of when it was new; consider a battery replacement.")
    if status == "learning":
        need = max(MIN_SESSIONS_FOR_HEALTH - len(measured), 0)
        advice.append(f"Still learning: {need} more full-ish discharge(s) needed "
                      "for a health estimate.")

    return Health(
        key=key, name=name, kind=kind, first_seen=first, last_seen=last,
        days_observed=round(days, 1), cycles=cycles, cycles_per_day=cpd,
        discharges=len(discharges), measured_discharges=len(measured), charges=len(charges),
        baseline_runtime_h=baseline, current_runtime_h=current, health_pct=health,
        fade_per_cycle_h=fade,
        charge_rate_first=_median([c.rate_per_h for c in rated[:BASELINE_SESSIONS]]),
        charge_rate_recent=_median([c.rate_per_h for c in rated[-RECENT_SESSIONS:]]),
        full_mv_first=int(_median(mvs[:BASELINE_SESSIONS])) if mvs else None,
        full_mv_recent=int(_median(mvs[-RECENT_SESSIONS:])) if mvs else None,
        days_to_80=days_to_80, projection_basis=basis,
        hours_full_on_charger_per_week=full_week, deep_discharges=deep,
        status=status, advice=advice)


# -- database ------------------------------------------------------------------------
def load_rows(db_path: str) -> Dict[str, Tuple[str, str, List[Row]]]:
    """key -> (latest name, kind, rows) from the sample log, read-only."""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return {}
    out: Dict[str, Tuple[str, str, List[Row]]] = {}
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(samples)")}
        mv = "voltage_mv" if "voltage_mv" in cols else "NULL"
        q = (f"SELECT key, name, kind, ts, level, charging, online, coarse, {mv} "
             "FROM samples ORDER BY key, ts")
        for key, name, kind, ts, level, chg, onl, coarse, volt in db.execute(q):
            ent = out.get(key)
            if ent is None:
                ent = out[key] = (name, kind, [])
            elif name != ent[0]:
                out[key] = ent = (name, kind, ent[2])
            ent[2].append(Row(ts, level, bool(chg), bool(onl), bool(coarse), volt))
    except sqlite3.OperationalError:
        pass
    finally:
        db.close()
    return out


def report(db_path: str) -> List[Health]:
    return sorted((analyse(k, n, kind, rows) for k, (n, kind, rows) in load_rows(db_path).items()),
                  key=lambda h: h.name.lower())


def levels_since(db_path: str, key: str, since: float) -> List[Tuple[float, int, bool]]:
    """(ts, level, charging) of one device since `since`, for charts."""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return [(ts, lv, bool(c)) for ts, lv, c in db.execute(
                "SELECT ts, level, charging FROM samples WHERE key=? AND ts>=? "
                "AND level IS NOT NULL ORDER BY ts", (key, since))]
        finally:
            db.close()
    except sqlite3.OperationalError:
        return []
