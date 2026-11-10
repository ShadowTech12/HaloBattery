"""Tray icons and the battery dashboard (PySide6 / Qt).

One tray icon per device, like the Windows app: a ring that fills with the level,
amber near the alert level, red below it, breathing green while charging and grey
while the device is asleep. Qt's QSystemTrayIcon is a StatusNotifierItem on KDE
Plasma (X11 and Wayland), so no extra tray library is needed.

The tray never talks to the devices: it reads the daemon's state file and the sample
log, so it can be restarted at will and costs nothing between updates.
"""
from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from halo import history

from . import health, paths
from .config import load_config

ICON_SIZE = 64
STATE_CHECK_MS = 2000          # how often the state file's mtime is checked
HEALTH_REFRESH_S = 300         # health is recomputed at most this often
BREATHE_MS = 250               # charging animation step (each step is a D-Bus icon update)
CHART_DAYS = 7

GREEN, AMBER, RED, GREY = QColor("#2ea043"), QColor("#d29922"), QColor("#da3633"), QColor("#8b949e")
KIND_ICONS = {
    "keyboard": ("input-keyboard", "K"),
    "mouse": ("input-mouse", "M"),
    "headset": ("audio-headset", "H"),
    "gamepad": ("input-gaming", "G"),
}


def level_color(level: Optional[int], low: int) -> QColor:
    if level is None:
        return GREY
    if level <= low:
        return RED
    if level <= low + 10:
        return AMBER
    return GREEN


def draw_icon(level: Optional[int], kind: str, online: bool, charging: bool, low: int,
              phase: float = 1.0) -> QIcon:
    """The ring icon. `phase` 0..1 is the breathing brightness while charging."""
    pm = QPixmap(ICON_SIZE, ICON_SIZE)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    w = ICON_SIZE * 0.13
    r = QRectF(w / 2 + 1, w / 2 + 1, ICON_SIZE - w - 2, ICON_SIZE - w - 2)
    track = QColor(GREY)
    track.setAlphaF(0.35)
    p.setPen(QPen(track, w, Qt.SolidLine, Qt.FlatCap))
    p.drawEllipse(r)
    if online and level is not None:
        col = QColor(GREEN if charging else level_color(level, low))
        if charging:
            col.setAlphaF(0.45 + 0.55 * phase)
        p.setPen(QPen(col, w, Qt.SolidLine, Qt.RoundCap if level < 100 else Qt.FlatCap))
        span = int(-360 * 16 * max(level, 2) / 100)
        p.drawArc(r, 90 * 16, span)
    # pictogram in the middle
    name, letter = KIND_ICONS.get(kind, ("battery", "?"))
    inner = ICON_SIZE * 0.52
    off = (ICON_SIZE - inner) / 2
    themed = QIcon.fromTheme(name)
    if not online:
        p.setOpacity(0.45)
    if not themed.isNull():
        themed.paint(p, int(off), int(off), int(inner), int(inner))
    else:
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(inner * 0.8))
        p.setFont(f)
        p.setPen(QApplication.palette().windowText().color())
        p.drawText(QRectF(off, off, inner, inner), Qt.AlignCenter, letter)
    p.end()
    return QIcon(pm)


def describe(d: dict) -> str:
    if d.get("approx"):
        lvl = d["approx"]
    elif d.get("level") is not None:
        lvl = f"{d['level']}%"
    else:
        lvl = "level unknown"
    if not d.get("online"):
        state = "asleep or off"
    elif d.get("charging"):
        state = "fully charged" if d.get("level") == 100 else "charging"
    else:
        state = "on battery"
    return f"{lvl}, {state}"


def time_left(d: dict) -> str:
    s = d.get("seconds_left")
    return history.format_left(s) if s else ""


def refresh_daemon() -> None:
    try:
        with open(paths.STATE_PATH, encoding="utf-8") as f:
            os.kill(json.load(f)["pid"], signal.SIGUSR1)
    except (OSError, ValueError, KeyError):
        pass


# -- chart -----------------------------------------------------------------------------
class LevelChart(QWidget):
    """Level over the last CHART_DAYS days; charging periods shaded."""

    def __init__(self, points, parent=None):
        super().__init__(parent)
        self.points = points
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        fg = pal.windowText().color()
        grid = QColor(fg)
        grid.setAlphaF(0.12)
        m_l, m_b, m_t, m_r = 30, 16, 6, 18
        W, H = self.width() - m_l - m_r, self.height() - m_t - m_b
        now = time.time()
        t0 = now - CHART_DAYS * 86400

        def X(t):
            return m_l + (t - t0) / (now - t0) * W

        def Y(v):
            return m_t + (1 - v / 100.0) * H

        small = QFont(self.font())
        small.setPixelSize(10)
        p.setFont(small)
        for v in (0, 50, 100):
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(m_l, Y(v)), QPointF(m_l + W, Y(v)))
            p.setPen(fg)
            p.drawText(QRectF(0, Y(v) - 7, m_l - 4, 14), Qt.AlignRight | Qt.AlignVCenter, f"{v}")
        for dday in range(CHART_DAYS, -1, -1):
            t = now - dday * 86400
            p.drawText(QRectF(X(t) - 20, m_t + H + 1, 40, 14), Qt.AlignCenter,
                       "now" if dday == 0 else f"-{dday}d")
        if len(self.points) < 2:
            p.drawText(QRectF(m_l, m_t, W, H), Qt.AlignCenter, "collecting data…")
            return
        shade = QColor(GREEN)
        shade.setAlphaF(0.15)
        for (ta, _la, ca), (tb, _lb, _cb) in zip(self.points, self.points[1:]):
            if ca:
                p.fillRect(QRectF(X(ta), m_t, max(X(tb) - X(ta), 1), H), shade)
        path = QPainterPath()
        for i, (t, lv, _c) in enumerate(self.points):
            pt = QPointF(X(max(t, t0)), Y(lv))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        path.lineTo(QPointF(X(now), Y(self.points[-1][1])))
        p.setPen(QPen(pal.highlight().color(), 2))
        p.drawPath(path)


# -- dashboard ---------------------------------------------------------------------------
def _fmt(v, unit="", nd=1):
    if v is None:
        return "–"
    return f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}"


class DeviceCard(QFrame):
    def __init__(self, d: dict, h: Optional[health.Health], low: int):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        title = QLabel(f"<b>{d['name']}</b>")
        title.setWordWrap(True)
        top.addWidget(title, 1)
        status = h.status if h else "learning"
        badge = QLabel(status.upper())
        col = {"good": GREEN, "fair": AMBER, "worn": RED}.get(status, GREY)
        badge.setStyleSheet(f"color: white; background: {col.name()}; border-radius: 4px;"
                            "padding: 1px 6px; font-size: 10px; font-weight: bold;")
        top.addWidget(badge, 0, Qt.AlignTop)
        lay.addLayout(top)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(d.get("level") or 0)
        bar.setFormat(describe(d) + (f" · {time_left(d)}" if time_left(d) else ""))
        bar.setStyleSheet(f"QProgressBar::chunk {{ background: "
                          f"{(GREEN if d.get('charging') else level_color(d.get('level'), low)).name()}; }}")
        lay.addWidget(bar)

        lay.addWidget(LevelChart(health.levels_since(paths.SAMPLES_DB, d["key"],
                                                     time.time() - CHART_DAYS * 86400)))
        if h:
            g = QGridLayout()
            rows = [
                ("Health", _fmt(h.health_pct, "%") + (f"  ({h.fade_per_cycle_h:+.3f} h/cycle)"
                                                     if h.fade_per_cycle_h is not None else "")),
                ("Runtime per 100%", f"{_fmt(h.current_runtime_h, ' h')} now · "
                                     f"{_fmt(h.baseline_runtime_h, ' h')} at first"),
                ("Cycles", f"{h.cycles:g}" + (f" ({h.cycles_per_day:g}/day)" if h.cycles_per_day else "")),
                ("Expected life", (f"~{h.days_to_80:g} days to 80%" if h.days_to_80 is not None else "–")
                                  + (f" · {h.projection_basis}" if h.projection_basis else "")),
                ("Charge rate", f"{_fmt(h.charge_rate_recent, '%/h')} now · "
                                f"{_fmt(h.charge_rate_first, '%/h')} at first"),
            ]
            if h.full_mv_first:
                rows.append(("Full voltage", f"{h.full_mv_recent} mV now · {h.full_mv_first} mV at first"))
            if h.hours_full_on_charger_per_week is not None:
                rows.append(("At 100% plugged", f"{h.hours_full_on_charger_per_week:g} h/week"))
            rows.append(("Observed", f"{h.days_observed:g} days · {h.measured_discharges}/"
                                     f"{h.discharges} discharges measured · {h.charges} charges"))
            for i, (k, v) in enumerate(rows):
                kl = QLabel(k)
                kl.setStyleSheet("color: palette(placeholder-text);")
                g.addWidget(kl, i, 0)
                vl = QLabel(v)
                vl.setWordWrap(True)
                g.addWidget(vl, i, 1)
            g.setColumnStretch(1, 1)
            lay.addLayout(g)
            for a in h.advice:
                al = QLabel("• " + a)
                al.setWordWrap(True)
                lay.addWidget(al)


class Dashboard(QWidget):
    def __init__(self, tray: "Tray"):
        super().__init__()
        self.tray = tray
        self.setWindowTitle("Halo Battery")
        self.setWindowIcon(QIcon.fromTheme("battery"))
        self.resize(520, 720)
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.updated = QLabel()
        bar.addWidget(self.updated, 1)
        btn = QPushButton(QIcon.fromTheme("view-refresh"), "Refresh")
        btn.clicked.connect(self._refresh)
        bar.addWidget(btn)
        outer.addLayout(bar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(self.scroll)
        note = QLabel("Health compares recent runtime per charge with the first measured "
                      "discharges. Estimates firm up after a few weeks of use.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text); font-size: 11px;")
        outer.addWidget(note)
        self.rebuild()

    def _refresh(self):
        refresh_daemon()
        self.tray.health_at = 0.0
        QTimer.singleShot(2500, self.tray.reload)

    def rebuild(self):
        body = QWidget()
        lay = QVBoxLayout(body)
        devs = self.tray.state.get("devices", [])
        if not devs:
            lay.addWidget(QLabel("No devices. Is the daemon running?\n"
                                 "systemctl --user status halobattery"))
        for d in devs:
            lay.addWidget(DeviceCard(d, self.tray.health.get(d["key"]), self.tray.cfg["low"]))
        lay.addStretch(1)
        self.scroll.setWidget(body)
        up = self.tray.state.get("updated")
        self.updated.setText(f"Updated {time.strftime('%H:%M:%S', time.localtime(up))}"
                             if up else "Daemon not running")


# -- tray ----------------------------------------------------------------------------------
class Tray:
    def __init__(self, app: QApplication):
        self.app = app
        self.cfg = load_config()
        self.state: dict = {}
        self.health: Dict[str, health.Health] = {}
        self.health_at = 0.0
        self.icons: Dict[str, QSystemTrayIcon] = {}
        self.menus: Dict[str, QMenu] = {}
        self.mtime = 0.0
        self.phase = 0.0
        self.dashboard: Optional[Dashboard] = None
        # placeholder while no device is known, so the app is reachable from the tray
        self.idle = QSystemTrayIcon(draw_icon(None, "", False, False, 20))
        self.idle.setToolTip("Halo Battery: no devices")
        self.idle.setContextMenu(self._menu(None))
        self.idle.activated.connect(self._activated)

        self.timer = QTimer()
        self.timer.timeout.connect(self._check)
        self.timer.start(STATE_CHECK_MS)
        self.breathe = QTimer()
        self.breathe.timeout.connect(self._breathe)
        self.breathe.start(BREATHE_MS)
        self.reload()

    # state ---------------------------------------------------------------------------
    def _check(self):
        try:
            m = os.stat(paths.STATE_PATH).st_mtime
        except OSError:
            m = -1.0
        if m != self.mtime:
            self.reload()

    def reload(self):
        try:
            self.mtime = os.stat(paths.STATE_PATH).st_mtime
            with open(paths.STATE_PATH, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.mtime, self.state = -1.0, {}
        if time.time() - self.health_at > HEALTH_REFRESH_S:
            self.health = {h.key: h for h in health.report(paths.SAMPLES_DB)}
            self.health_at = time.time()
        self._sync_icons()
        if self.dashboard and self.dashboard.isVisible():
            self.dashboard.rebuild()

    def _sync_icons(self):
        devs = {d["key"]: d for d in self.state.get("devices", [])}
        for key in list(self.icons):
            if key not in devs:
                self.icons.pop(key).hide()
                self.menus.pop(key, None)
        for key, d in devs.items():
            ic = self.icons.get(key)
            if ic is None:
                ic = self.icons[key] = QSystemTrayIcon()
                ic.activated.connect(self._activated)
            menu = self.menus[key] = self._menu(d)
            ic.setContextMenu(menu)
            ic.setToolTip(self._tooltip(d))
            self._paint(ic, d)
            ic.show()
        self.idle.setVisible(not devs)

    def _paint(self, ic: QSystemTrayIcon, d: dict, phase: float = 1.0):
        ic.setIcon(draw_icon(d.get("level"), d.get("kind", ""), d.get("online", False),
                             d.get("charging", False), self.cfg["low"], phase))

    def _breathe(self):
        self.phase = (self.phase + BREATHE_MS / 2500.0) % 1.0
        b = 0.5 - 0.5 * math.cos(2 * math.pi * self.phase)
        for d in self.state.get("devices", []):
            if d.get("charging") and d.get("online") and d.get("level", 0) < 100:
                ic = self.icons.get(d["key"])
                if ic:
                    self._paint(ic, d, b)

    def _tooltip(self, d: dict) -> str:
        lines = [d["name"], describe(d)]
        if time_left(d):
            lines.append(time_left(d))
        h = self.health.get(d["key"])
        if h and h.health_pct is not None:
            lines.append(f"Battery health {h.health_pct:g}% ({h.status})")
        elif h:
            lines.append("Battery health: learning")
        return "\n".join(lines)

    # menu ------------------------------------------------------------------------------
    def _menu(self, d: Optional[dict]) -> QMenu:
        m = QMenu()
        if d:
            head = m.addAction(d["name"])
            head.setEnabled(False)
            info = m.addAction(describe(d) + (f" · {time_left(d)}" if time_left(d) else ""))
            info.setEnabled(False)
            h = self.health.get(d["key"])
            if h:
                txt = (f"Health {h.health_pct:g}% ({h.status})" if h.health_pct is not None
                       else "Health: learning")
                if h.days_to_80 is not None:
                    txt += f" · ~{h.days_to_80:g} days to 80%"
                a = m.addAction(txt)
                a.setEnabled(False)
            m.addSeparator()
        dash = QAction(QIcon.fromTheme("view-statistics"), "Battery health…", m)
        dash.triggered.connect(self.show_dashboard)
        m.addAction(dash)
        ref = QAction(QIcon.fromTheme("view-refresh"), "Refresh now", m)
        ref.triggered.connect(lambda: (refresh_daemon(), QTimer.singleShot(2500, self.reload)))
        m.addAction(ref)
        m.addSeparator()
        q = QAction(QIcon.fromTheme("application-exit"), "Quit tray", m)
        q.triggered.connect(self.app.quit)
        m.addAction(q)
        return m

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_dashboard()

    def show_dashboard(self):
        self.health_at = 0.0
        self.reload()
        if self.dashboard is None:
            self.dashboard = Dashboard(self)
        else:
            self.dashboard.rebuild()
        self.dashboard.show()
        self.dashboard.raise_()
        self.dashboard.activateWindow()


def main(dashboard: bool = False) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Halo Battery")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray available.", file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    keepalive = QTimer()           # let Python see signals while Qt's loop runs
    keepalive.timeout.connect(lambda: None)
    keepalive.start(500)
    tray = Tray(app)               # noqa: F841  (kept alive by the event loop)
    if dashboard:
        tray.show_dashboard()
    return app.exec()
