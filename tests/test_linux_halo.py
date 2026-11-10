"""Linux daemon, sample log and battery health analysis (halo_linux)."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from halo_linux import config, health, samples  # noqa: E402
from providers import logitech  # noqa: E402
from providers.base import DeviceStatus  # noqa: E402

HOUR = 3600.0


def synth(runtimes, step=60.0, start=1_700_000_000.0, low=20, charge_rate=50.0,
          hold_full_h=2.0, mv=None, idle_h=10.0):
    """Rows for discharges from 100 % down to `low`, each at runtime_h per 100 %,
    with a charge back to 100 % and `hold_full_h` hours on the charger after each.
    `idle_h` hours of the device off between sessions spread the data over days."""
    rows, t = [], start
    for i, rt in enumerate(runtimes):
        lvl = 100.0
        while lvl > low:
            rows.append(health.Row(t, int(round(lvl)), False, True,
                                   voltage_mv=(mv[i] if mv else None)))
            t += step
            lvl -= 100.0 / rt / HOUR * step
        rows.append(health.Row(t, low, False, False))         # switched off
        t += idle_h * HOUR
        lvl = float(low)
        while lvl < 100:
            v = (mv[i] if mv else None)
            rows.append(health.Row(t, int(lvl), True, True, voltage_mv=v))
            t += step
            lvl += charge_rate / HOUR * step
        end = t + hold_full_h * HOUR
        while t < end:
            rows.append(health.Row(t, 100, True, True, voltage_mv=(mv[i] if mv else None)))
            t += step
    return rows


class HealthAnalysisTest(unittest.TestCase):
    def test_new_device_is_learning(self):
        h = health.analyse("k", "Mouse", "mouse", synth([10.0, 10.0]))
        self.assertEqual(h.status, "learning")
        self.assertIsNone(h.health_pct)
        self.assertEqual(h.measured_discharges, 2)
        self.assertTrue(any("learning" in a.lower() for a in h.advice))

    def test_runtime_is_measured_per_100_percent(self):
        h = health.analyse("k", "Mouse", "mouse", synth([12.0, 12.0, 12.0, 12.0]))
        self.assertAlmostEqual(h.baseline_runtime_h, 12.0, delta=0.3)
        self.assertAlmostEqual(h.health_pct, 100.0, delta=3)
        self.assertEqual(h.status, "good")

    def test_fading_runtime_lowers_health_and_projects_life(self):
        rts = [20.0, 20.0, 20.0, 18.0, 17.0, 16.0]
        h = health.analyse("k", "Headset", "headset", synth(rts))
        self.assertAlmostEqual(h.health_pct, 85.0, delta=3)   # median(18,17,16) / 20
        self.assertEqual(h.status, "fair")
        self.assertLess(h.fade_per_cycle_h, 0)
        self.assertEqual(h.projection_basis, "measured runtime fade")
        self.assertGreater(h.days_to_80, 0)

    def test_worn_battery_advises_replacement(self):
        h = health.analyse("k", "Kb", "keyboard", synth([30.0, 30.0, 30.0, 21.0, 20.0, 20.0]))
        self.assertEqual(h.status, "worn")
        self.assertTrue(any("replacement" in a for a in h.advice))

    def test_cycles_count_discharged_percent(self):
        h = health.analyse("k", "M", "mouse", synth([10.0] * 5))   # 5 x 80 % = 4 cycles
        self.assertAlmostEqual(h.cycles, 4.0, delta=0.1)

    def test_charge_rate_of_a_percentage_device(self):
        h = health.analyse("k", "Mouse", "mouse", synth([10.0] * 4))
        self.assertAlmostEqual(h.charge_rate_first, 50.0, delta=3)
        self.assertIsNone(h.full_mv_first)

    def test_voltage_device_has_no_charge_rate_but_a_rested_full_voltage(self):
        mv = [4100, 4090, 4080, 4070, 4060]
        rows = synth([10.0] * 5, mv=mv)
        _, charges, _, _ = health.split_sessions(rows)
        self.assertTrue(all(c.rate_per_h is None for c in charges))
        # the first settled reading of the next discharge gives each charge its voltage
        self.assertEqual([c.full_mv for c in charges], mv[1:] + [None])
        h = health.analyse("k", "Kb", "keyboard", rows)
        self.assertIsNone(h.charge_rate_first)
        self.assertEqual(h.full_mv_first, 4080)     # median of 4090, 4080, 4070

    def test_voltage_device_unplug_step_and_settling_are_ignored(self):
        V = 3850
        rows = [health.Row(i * 60.0, 70 - i, False, True, voltage_mv=V) for i in range(15)]  # 70..56
        t = 15 * 60.0
        for lv in (75, 78, 80):                     # on the charger: reads ~20 points high
            rows.append(health.Row(t, lv, True, True, voltage_mv=3960))
            t += 60
        for i, lv in enumerate((60, 59, 58, 58, 57)):   # settling, within SETTLE_S
            rows.append(health.Row(t + i * 60, lv, False, True, voltage_mv=3860))
        t += health.SETTLE_S + 60
        for i in range(10):                         # settled: 57 .. 48
            rows.append(health.Row(t + i * 60, 57 - i, False, True, voltage_mv=3820))
        d, c, dropped, _ = health.split_sessions(rows)
        self.assertEqual(dropped, 14 + 9)           # not the 80 -> 60 step, not settling
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].start_level, 56)
        self.assertIsNone(c[0].end_level)
        self.assertEqual(d[1].start_level, 57)

    def test_chart_holds_the_level_while_a_voltage_device_charges(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.db")
            log = samples.SampleLog(path)
            for ts, lv, chg in ((0, 56, False), (60, 75, True), (120, 80, True), (180, 60, False)):
                log.record(DeviceStatus("k", "Kb", lv, chg, voltage_mv=3900,
                                        level_from_voltage=True), ts)
            log.close()
            pts = health.levels_since(path, "k", -1)
            self.assertEqual([p[1] for p in pts], [56, 56, 56, 60])
            self.assertEqual([p[2] for p in pts], [False, True, True, False])

    def test_long_time_on_charger_at_full_is_flagged(self):
        h = health.analyse("k", "M", "mouse", synth([10.0] * 4, hold_full_h=20.0))
        self.assertGreater(h.hours_full_on_charger_per_week, 24)
        self.assertTrue(any("unplug" in a for a in h.advice))

    def test_jump_without_charging_flag_starts_a_new_session(self):
        rows = [health.Row(i * 60.0, 90 - i // 10, False, True) for i in range(200)]
        rows += [health.Row(200 * 60.0 + i * 60.0, 100 - i // 10, False, True) for i in range(200)]
        d, c, _, _ = health.split_sessions(rows)
        self.assertEqual(len(d), 2)
        self.assertEqual(c, [])

    def test_asleep_time_is_not_use(self):
        rows = [health.Row(0, 100, False, True), health.Row(60, 99, False, True),
                health.Row(120, 99, False, False), health.Row(100_000, 99, False, True),
                health.Row(100_060, 98, False, True)]
        d, _, _, _ = health.split_sessions(rows)
        self.assertEqual(len(d), 1)
        self.assertLessEqual(d[0].use_s, 3 * 60)

    def test_empty_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(health.report(os.path.join(tmp, "missing.db")), [])


class SampleLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.db")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        db = sqlite3.connect(self.path)
        try:
            return db.execute("SELECT ts, level, voltage_mv FROM samples ORDER BY ts").fetchall()
        finally:
            db.close()

    def test_unchanged_readings_are_written_once_per_heartbeat(self):
        log = samples.SampleLog(self.path)
        st = DeviceStatus("k", "Mouse", 50, voltage_mv=3800)
        self.assertTrue(log.record(st, 0))
        self.assertFalse(log.record(st, 60))
        self.assertFalse(log.record(DeviceStatus("k", "Mouse", 50, voltage_mv=3795), 120))
        self.assertTrue(log.record(DeviceStatus("k", "Mouse", 49), 180))
        self.assertTrue(log.record(DeviceStatus("k", "Mouse", 49), 180 + samples.HEARTBEAT))
        log.close()
        self.assertEqual([r[1] for r in self.rows()], [50, 49, 49])
        self.assertEqual(self.rows()[0][2], 3800)

    def test_old_database_gains_voltage_column(self):
        db = sqlite3.connect(self.path)
        db.executescript("CREATE TABLE samples (ts REAL NOT NULL, key TEXT NOT NULL, "
                         "name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', level INTEGER, "
                         "charging INTEGER NOT NULL, online INTEGER NOT NULL, "
                         "coarse INTEGER NOT NULL DEFAULT 0);"
                         "INSERT INTO samples VALUES (1, 'k', 'M', '', 80, 0, 1, 0);")
        db.commit()
        db.close()
        log = samples.SampleLog(self.path)
        log.record(DeviceStatus("k", "M", 79, voltage_mv=3700), 2)
        log.close()
        self.assertEqual(self.rows(), [(1.0, 80, None), (2.0, 79, 3700)])
        self.assertEqual(len(health.load_rows(self.path)["k"][2]), 2)


class LogitechVoltageTest(unittest.TestCase):
    def test_voltage_feature_gives_millivolts(self):
        self.assertEqual(logitech.battery_mv(logitech.F_VOLTAGE, bytes([0x0E, 0xFF, 0])), 3839)

    def test_charge_complete_flag(self):
        self.assertTrue(logitech.battery_full(logitech.F_VOLTAGE, bytes([0x10, 0x5A, 0x81])))
        self.assertFalse(logitech.battery_full(logitech.F_VOLTAGE, bytes([0x0F, 0x76, 0x80])))
        self.assertFalse(logitech.battery_full(logitech.F_VOLTAGE, bytes([0x0F, 0x76, 0x01])))
        self.assertFalse(logitech.battery_full(logitech.F_UNIFIED, bytes([100, 0, 0x81])))

    def test_other_features_and_bogus_values_give_none(self):
        self.assertIsNone(logitech.battery_mv(logitech.F_UNIFIED, bytes([50, 4, 0, 0])))
        self.assertIsNone(logitech.battery_mv(logitech.F_VOLTAGE, bytes([0x01, 0x00, 0])))


class ConfigTest(unittest.TestCase):
    def test_defaults_and_clamping(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            self.assertEqual(config.load_config(p)["interval"], 60)
            with open(p, "w") as f:
                json.dump({"interval": 1, "low": 30, "bogus": 1}, f)
            cfg = config.load_config(p)
            self.assertEqual(cfg["interval"], 5)
            self.assertEqual(cfg["low"], 30)
            self.assertNotIn("bogus", cfg)

    def test_broken_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            with open(p, "w") as f:
                f.write("{not json")
            self.assertEqual(config.load_config(p), config.DEFAULTS)


class DaemonAlertTest(unittest.TestCase):
    def setUp(self):
        from halo_linux import daemon
        self.daemon_mod = daemon
        d = daemon.Daemon.__new__(daemon.Daemon)     # no providers, no files
        d.cfg = dict(config.DEFAULTS)
        d.alerted_low, d.alerted_full = {}, {}
        d.history = mock.Mock(seconds_left=mock.Mock(return_value=None))
        self.d = d

    def run_levels(self, seq):
        with mock.patch.object(self.daemon_mod, "notify") as n:
            for level, charging in seq:
                self.d._alerts(DeviceStatus("k", "Mouse", level, charging))
        return [c.args[0] for c in n.call_args_list]

    def test_low_alert_once_per_discharge(self):
        sent = self.run_levels([(25, False), (20, False), (18, False), (15, False)])
        self.assertEqual(sent, ["Mouse battery low"])

    def test_low_alert_again_after_a_charge(self):
        sent = self.run_levels([(20, False), (50, True), (19, False)])
        self.assertEqual(sent, ["Mouse battery low", "Mouse battery low"])

    def test_full_alert_once_while_on_charger(self):
        sent = self.run_levels([(99, True), (100, True), (100, True)])
        self.assertEqual(sent, ["Mouse is fully charged"])

    def test_voltage_device_shows_last_resting_level_while_charging(self):
        d = self.d
        d.rest_level = {}
        kb = lambda lv, chg, full=False: DeviceStatus(   # noqa: E731
            "k", "Kb", lv, chg, level_from_voltage=True, charge_full=full)
        self.assertEqual(d.displayed(kb(56, False)).level, 56)
        shown = d.displayed(kb(75, True))
        self.assertEqual((shown.level, shown.approx), (56, "charging (from 56%)"))
        full = d.displayed(kb(100, True, full=True))
        self.assertEqual((full.level, full.approx), (100, ""))

    def test_voltage_device_full_alert_waits_for_the_device_flag(self):
        self.d.rest_level = {"k": 56}
        with mock.patch.object(self.daemon_mod, "notify") as n:
            for lv, full in ((90, False), (100, False), (100, True)):
                st = self.d.displayed(DeviceStatus("k", "Kb", lv, True, level_from_voltage=True,
                                                   charge_full=full))
                self.d._alerts(st)
        self.assertEqual([c.args[0] for c in n.call_args_list], ["Kb is fully charged"])

    def test_percentage_devices_are_shown_as_read(self):
        self.d.rest_level = {}
        st = DeviceStatus("m", "Mouse", 70, True)
        self.assertIs(self.d.displayed(st), st)

    def test_resting_level_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = samples.SampleLog(os.path.join(tmp, "s.db"))
            log.record(DeviceStatus("k", "Kb", 56, False), 0)
            log.record(DeviceStatus("k", "Kb", 75, True), 60)
            log.record(DeviceStatus("m", "M", 40, False, online=False), 60)
            self.assertEqual(log.last_resting_levels(), {"k": 56})
            log.close()

    def test_windows_only_providers_are_skipped(self):
        names = {c.__name__ for c in self.daemon_mod.provider_classes()}
        self.assertIn("LogitechProvider", names)
        self.assertTrue(names.isdisjoint(self.daemon_mod.WINDOWS_ONLY))
        self.assertNotIn("Provider", names)


if __name__ == "__main__":
    unittest.main()
