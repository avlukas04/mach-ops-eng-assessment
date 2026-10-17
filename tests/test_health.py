"""
Tests for pipeline/health.py: chart statistics, incremental counts, the
alert rules of each layer, the gate's feed observation, and the real month.

Run from the repository root:
  python3 -m unittest discover -s tests -v
"""

import datetime as dt
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import factory_simulator as fs  # noqa: E402
import spc_detector as spc  # noqa: E402
from pipeline import health as H  # noqa: E402
from pipeline import runner as rn  # noqa: E402
from pipeline.source import Batch, MesSource  # noqa: E402
from pipeline.store import SERVOS, Store, columns_from_frame  # noqa: E402

CSV = os.path.join(ROOT, "manufacturing.csv")
SCORING = spc.load_baseline(spc.DEFAULT_BASELINE)
with open(H.DEFAULT_HEALTH_BASELINE) as _f:
    HB = json.load(_f)
logging.getLogger("gate").setLevel(logging.CRITICAL)
NOW = dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec="milliseconds")


def make_units(n, hour, rng, start=0, line="LN1", plant="HB-PLANT-1", lot="L1", **overrides):
    """n units built in `hour` with healthy random bench readings."""
    rows = []
    for i in range(n):
        u = {"vehicle_id": f"V{start + i:06d}", "build_date_utc": hour, "build_shift": "A",
             "factory_site": plant, "line_id": line, "qc_station_id": "QC-1",
             "final_qc_pass": 1, "final_qc_timestamp_utc": (pd.Timestamp(hour) + pd.Timedelta(hours=2))
             .strftime("%Y-%m-%dT%H:%M:%S"), "rework_count": 0, "rework_codes": None,
             "qc_exception_count": 0, "imu_cal_status": "PASS", "mag_cal_status": "PASS",
             "harness_strain_relief_ok": 1}
        for s in SERVOS:
            u.update({f"{s}_servo_lot_id": lot, f"{s}_servo_vendor": "VEN", f"{s}_servo_model": "M1"})
        for c in spc.MEASURE_COLS:
            m = SCORING["measures"][c]
            u[c] = m["median"] + rng.normal(0, m["sigma"] * 0.5)  # comfortably inside limits
        u.update(overrides)
        rows.append(u)
    return pd.DataFrame(rows)


def write(store, frame, sim):
    res, _, _ = spc.classify(frame, SCORING)
    by_id = dict(zip(res["vehicle_id"], res.to_dict("records")))
    d = store.diff(frame.to_dict("records"))
    todo = d["new"] + d["changed"]
    return store.write_batch(todo, [by_id[u["vehicle_id"]] for u in todo],
                             baseline_version=SCORING["version"], sim_time=sim,
                             unchanged=d["unchanged"])


def counts_equal(a, b):
    a = {(p, k): v for p, d in a.items() for k, v in d.items()}
    b = {(p, k): v for p, d in b.items() for k, v in d.items()}
    if set(a) != set(b):
        return False
    return all((a[k] == b[k]).all() for k in a)


def brute_limits(n, p, alpha):
    pmf = [math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(n + 1)]
    cdf = np.cumsum(pmf)
    lower = max([k for k in range(n + 1) if cdf[k] <= alpha], default=-1)
    upper = min([k for k in range(n + 1) if 1 - (cdf[k - 1] if k else 0) <= alpha], default=n + 1)
    return lower, upper


class StatisticsTest(unittest.TestCase):
    def test_exact_limits_match_brute_force(self):
        for n, p in [(50, 0.0325), (330, 0.0069), (400, 0.0325), (120, 0.2), (30, 0.5)]:
            self.assertEqual(H.binom_limits(n, p, 0.00135), brute_limits(n, p, 0.00135), (n, p))

    def test_limits_are_exact_tails(self):
        lower, upper = H.binom_limits(330, 0.0069)
        self.assertEqual((lower, upper), (-1, 9))  # 9+ flags in a 330-unit lot-day alarms

    def test_chart_signals(self):
        rules = dict(H.DEFAULT_RULES)
        center = 0.0325
        series = [(f"2026-02-{d:02d}", 300, 10) for d in range(1, 15)]   # p 3.33% > centre
        series[3] = ("2026-02-04", 300, 22)                              # far above
        series[6] = ("2026-02-07", 3000, 50)                             # far below (big n)
        pts = H.chart_points(series, center, rules, active_from="2026-02-13")
        sig = {p["day"]: p["signal"] for p in pts}
        self.assertEqual(sig["2026-02-04"], "above")
        self.assertEqual(sig["2026-02-07"], "below")
        # the run restarts after the low day; 8 above in a row from 02-08 -> 02-15
        self.assertIsNone(sig["2026-02-14"])
        series.append(("2026-02-15", 300, 10))
        pts = H.chart_points(series, center, rules, active_from="2026-02-14")
        self.assertEqual(pts[-1]["signal"], "run")
        self.assertTrue(pts[-1]["active"])
        self.assertFalse(pts[3]["active"])  # old signal stays on the chart, not in status


class SyntheticBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "gate.db")
        self.rng = np.random.default_rng(7)
        first = make_units(200, "2026-02-01T00:00:00", self.rng)
        self.store = Store(self.db_path, columns={**columns_from_frame(first),
                                                  **{c: "REAL" for c in spc.MEASURE_COLS}})
        write(self.store, first, "2026-02-01T00:00:00")
        self.health_bl = os.path.join(self.tmp.name, "health.json")
        shutil.copy(H.DEFAULT_HEALTH_BASELINE, self.health_bl)
        self.h = H.Health(self.db_path, self.health_bl)

    def tearDown(self):
        self.h.close()
        self.store.close()
        self.tmp.cleanup()

    def alerts(self, snap=None, layer=None):
        snap = snap or self.h.snapshot(now=NOW)
        return [a for a in snap["alerts"] if layer is None or a["layer"] == layer]

    def healthy_meta(self, **over):
        meta = {"sim_time": "2026-02-01T00:00:00", "feed_clock": "2026-02-01T00:00:00",
                "runner_status": {"state": "RUNNING", "since": iso(NOW), "message": ""},
                "last_poll_at": iso(NOW - dt.timedelta(seconds=5)),
                "feed": {"clock": "2026-02-01T00:00:00", "cadence_s": 2.0, "state": "running",
                         "published_at": iso(NOW), "silent_s": 1.0, "checked_at": iso(NOW)},
                "baseline": {"in_use": {"version": SCORING["version"], "sha256": "x"},
                             "on_disk": {"state": "same", "version": SCORING["version"], "sha256": "x"}}}
        meta.update(over)
        self.store.set_meta(meta)


class IncrementalTest(SyntheticBase):
    def test_running_counts_equal_full_recount_after_rework_and_rescore(self):
        self.h.refresh()
        self.assertEqual(self.h.timings.keys() >= {"full_load_ms"}, True)
        # New units, reworked units (codes and a raised reading), and a rescore
        write(self.store, make_units(150, "2026-02-01T01:00:00", self.rng, start=1000,
                                     line="LN2"), "2026-02-01T01:00:00")
        old = make_units(200, "2026-02-01T00:00:00", np.random.default_rng(7))
        rework = old.iloc[:30].copy()
        rework["rework_count"] = 1
        rework["rework_codes"] = "R05|R12"
        rework["aileron_l_servo_sweep_current_ma"] = (
            SCORING["measures"]["aileron_l_servo_sweep_current_ma"]["median"]
            + 3.5 * SCORING["measures"]["aileron_l_servo_sweep_current_ma"]["sigma"])  # -> FLAG
        write(self.store, rework, "2026-02-01T02:00:00")
        res, _, _ = spc.classify(old.iloc[30:40], SCORING)
        res.loc[:, "verdict"] = "FLAG"  # a rescore that changes verdicts
        self.store.rescore_batch(res.to_dict("records"), baseline_version="other",
                                 sim_time="2026-02-01T02:00:00")
        n = self.h.refresh()
        self.assertEqual(n, 150 + 30 + 10)
        self.assertIn("update_ms", self.h.timings)
        self.assertTrue(counts_equal(self.h.counts, self.h.recount()))
        fresh = H.Health(self.db_path, self.health_bl)
        fresh.refresh()
        self.assertTrue(counts_equal(self.h.counts, fresh.counts))
        fresh.close()
        snap = self.h.snapshot(now=NOW, refresh=False)
        self.assertEqual(snap["product"]["units"], 350)
        self.assertEqual(snap["product"]["verdicts"]["FLAG"], 40)
        rw = snap["throughput"]["rework"]
        self.assertEqual((rw["reworked"], rw["by_code"]["R05"]["units"]), (30, 30))
        self.assertEqual(self.h.refresh(), 0)  # nothing new: no work

    def test_parts_compact_without_changing_counts(self):
        self.h.COMPACT_PARTS = 3
        self.h.refresh()
        for i in range(6):
            write(self.store, make_units(20, f"2026-02-01T{i + 1:02d}:00:00", self.rng, start=2000 + 20 * i),
                  f"2026-02-01T{i + 1:02d}:00:00")
            again = make_units(200, "2026-02-01T00:00:00", np.random.default_rng(7)).iloc[i * 5:i * 5 + 5].copy()
            again["rework_count"] = i + 1  # the same units reworked repeatedly
            write(self.store, again, f"2026-02-01T{i + 1:02d}:00:00")
            self.h.refresh()
            self.assertLessEqual(len(self.h._parts), 4)
            self.assertTrue(counts_equal(self.h.counts, self.h.recount()))
        self.assertEqual(len(self.h.frame), 320)
        self.assertEqual(int(self.h.frame.loc["V000000", "reworked"]), 1)


class ProductAndQualityTest(SyntheticBase):
    def test_open_hold_is_alarm_until_released(self):
        defect = make_units(1, "2026-02-01T01:00:00", self.rng, start=5000)
        for m in spc.METRICS:
            c = spc.col("aileron_l", m)
            defect[c] = SCORING["measures"][c]["median"] + 4 * SCORING["measures"][c]["sigma"]
        write(self.store, defect, "2026-02-01T01:00:00")
        self.healthy_meta(sim_time="2026-02-01T01:00:00", feed_clock="2026-02-01T02:00:00")
        snap = self.h.snapshot(now=NOW)
        hold = snap["product"]["holds"][0]
        self.assertEqual((hold["vehicle_id"], hold["lead_hours"], hold["age_hours"], hold["open"]),
                         ("V005000", 2.0, 1.0, True))
        self.assertEqual([a["severity"] for a in self.alerts(snap, "product")], ["ALARM"])
        self.assertEqual(snap["layers"]["product"], "red")
        self.assertEqual(snap["quality"]["held_before_final_qc"], 1)
        info = self.alerts(snap, "quality")
        self.assertEqual([a["severity"] for a in info], ["INFO"])  # QC passed 100%, gate held 1
        self.assertIn("Final QC passed 100.0%", info[0]["message"])
        self.store.add_event("V005000", "RELEASED", "J. Doe", "bench retest 3x clean")
        snap = self.h.snapshot(now=NOW)
        self.assertFalse(snap["product"]["holds"][0]["open"])
        self.assertEqual(self.alerts(snap, "product"), [])
        self.assertEqual(snap["layers"]["product"], "green")

    def test_open_warnings_by_type(self):
        w = make_units(4, "2026-02-01T01:00:00", self.rng, start=6000, qc_station_id="QC-2")
        w.loc[0, "qc_exception_count"] = 2
        w.loc[1, "imu_cal_status"] = "WARN"
        w.loc[2, "mag_cal_status"] = "WARN"
        w.loc[3, "harness_strain_relief_ok"] = 0
        w.loc[0, "mag_cal_status"] = "WARN"  # two warnings on one unit count once in "any"
        write(self.store, w, "2026-02-01T01:00:00")
        ow = self.h.snapshot(now=NOW)["quality"]["open_warnings"]
        self.assertEqual({k: v["units"] for k, v in ow.items()},
                         {"qc_exception": 1, "imu_cal_warn": 1, "mag_cal_warn": 2,
                          "strain_relief_fail": 1, "any": 4})
        self.assertEqual(ow["any"]["by_qc_station"], {"QC-2": 4})


    def test_throughput_watch_needs_two_slow_hours_in_a_row(self):
        normal = {line: 50 for line in self.h.hb["throughput"]["line_units_per_hour_median"]}
        slow = dict(normal, **{"HB-PLANT-1/LN2": 20})
        h0, h1, h2 = "2026-02-01T00:00:00", "2026-02-01T01:00:00", "2026-02-01T02:00:00"
        # One slow hour (the month's quietest real line-hour was 27): no watch
        self.assertEqual(self.h.throughput_watches(h1, {h0: normal, h1: slow}), [])
        # Slow, then recovered: no watch
        self.assertEqual(self.h.throughput_watches(h2, {h0: normal, h1: slow, h2: normal}), [])
        # Slow two build hours in a row: WATCH, reporting the latest hour's count
        self.assertEqual(self.h.throughput_watches(h2, {h1: slow, h2: slow}),
                         [("HB-PLANT-1/LN2", 20, 49.0)])
        # Exactly at the limit is not under it (half of 49 is 24.5; 25 is fine)
        edge = dict(normal, **{"HB-PLANT-1/LN2": 25})
        self.assertEqual(self.h.throughput_watches(h2, {h1: edge, h2: edge}), [])
        # A stopped line (0 units) for two hours while others build: WATCH
        stopped = {k: v for k, v in normal.items() if k != "OC-PLANT-2/LN3"}
        self.assertEqual(self.h.throughput_watches(h2, {h1: stopped, h2: stopped}),
                         [("OC-PLANT-2/LN3", 0, 49.0)])
        # The previous hour has no units at all: a feed gap, left to the gate layer
        self.assertEqual(self.h.throughput_watches(h2, {h2: slow}), [])


class GateHealthTest(SyntheticBase):
    def gate_alerts(self, **meta):
        self.healthy_meta(**meta)
        return {a["id"].split(":", 1)[1]: a["severity"] for a in self.alerts(layer="gate")}

    def test_healthy_gate_is_green(self):
        self.healthy_meta()
        snap = self.h.snapshot(now=NOW)
        self.assertEqual(self.alerts(snap, "gate"), [])
        self.assertEqual(snap["layers"]["gate"], "green")

    def test_stalled_runner_is_alarm(self):
        a = self.gate_alerts(runner_status={"state": "STALLED", "since": iso(NOW),
                                            "message": "SchemaError: feed is missing ..."})
        self.assertEqual(a, {"stalled": "ALARM"})

    def test_heartbeat_older_than_30s_is_alarm(self):
        self.assertEqual(self.gate_alerts(last_poll_at=iso(NOW - dt.timedelta(seconds=29))), {})
        self.assertEqual(self.gate_alerts(last_poll_at=iso(NOW - dt.timedelta(seconds=31))),
                         {"heartbeat": "ALARM"})

    def test_feed_silent_over_3x_cadence_is_alarm_but_finished_is_info(self):
        feed = {"clock": "2026-02-01T00:00:00", "cadence_s": 2.0, "state": "running",
                "published_at": iso(NOW), "checked_at": iso(NOW)}
        self.assertEqual(self.gate_alerts(feed=dict(feed, silent_s=5.9)), {})
        self.assertEqual(self.gate_alerts(feed=dict(feed, silent_s=6.1)), {"feed_silent": "ALARM"})
        self.assertEqual(self.gate_alerts(feed=dict(feed, silent_s=600, state="finished")),
                         {"feed_finished": "INFO"})
        self.assertEqual(self.gate_alerts(feed=dict(feed, silent_s=600, cadence_s=None)), {})

    def test_lag_over_3_build_hours_is_watch(self):
        self.assertEqual(self.gate_alerts(feed_clock="2026-02-01T03:00:00"), {})
        self.assertEqual(self.gate_alerts(feed_clock="2026-02-01T04:00:00"), {"lag": "WATCH"})

    def test_latency_from_duration_not_timestamps(self):
        c = self.store.conn
        c.execute("DELETE FROM runs")
        for i, ms in enumerate([100] * 22 + [1500, 1600]):
            # wall clock running backwards must not matter: only duration_ms is used
            c.execute("INSERT INTO runs (started_at, finished_at, rows_read, new, changed, unchanged, "
                      "rejected, quarantines_raised, duration_ms) VALUES (?, ?, 0, 0, 0, 0, 0, 0, ?)",
                      (iso(NOW), iso(NOW - dt.timedelta(seconds=5)), ms))
        a = self.gate_alerts()
        self.assertEqual(a, {"latency": "WATCH"})  # p95 ~1,500 ms > 50% of 2,000 ms
        lat = self.h.snapshot(now=NOW)["gate"]["latency"]
        self.assertEqual((lat["p50_ms"], lat["max_ms"], lat["runs"]), (100.0, 1600.0, 24))

    def test_ingest_errors_are_watch_grouped_by_reason(self):
        run_id = write(self.store, make_units(1, "2026-02-01T01:00:00", self.rng, start=7000),
                       "2026-02-01T01:00:00")["run_id"]
        c = self.store.conn
        for reason in ["blank vehicle_id: row rejected", "non-numeric aileron_l_x='ERR': scored as missing",
                       "non-numeric rudder_y='??': scored as missing"]:
            c.execute("INSERT INTO ingest_errors (run_id, vehicle_id, reason, created_at) VALUES (?, '', ?, ?)",
                      (run_id, reason, iso(NOW)))
        self.healthy_meta()
        g = self.h.snapshot(now=NOW)["gate"]
        self.assertEqual(g["ingest_errors_recent"], {"blank vehicle_id": 1, "non-numeric aileron_l_x": 1,
                                                     "non-numeric rudder_y": 1})
        self.assertEqual(self.gate_alerts(), {"ingest_errors": "WATCH"})

    def test_baseline_drift_is_watch(self):
        drift = {"in_use": {"version": SCORING["version"], "sha256": "x"},
                 "on_disk": {"state": "differs", "version": "next", "sha256": "y"}}
        self.assertEqual(self.gate_alerts(baseline=drift), {"baseline_drift": "WATCH"})
        with open(self.health_bl, "a") as f:
            f.write(" ")
        self.assertEqual(self.gate_alerts(), {"health_baseline_drift": "WATCH"})

    def test_correlated_readings_are_alarm(self):
        self.healthy_meta()
        self.assertEqual(self.gate_alerts(), {})
        u = make_units(3000, "2026-02-01T05:00:00", self.rng, start=10000)
        shared = self.rng.normal(0, 1, len(u))  # one common factor across a servo's metrics
        for m in spc.METRICS:
            c = spc.col("rudder", m)
            u[c] = SCORING["measures"][c]["median"] + 0.4 * SCORING["measures"][c]["sigma"] * shared
        write(self.store, u, "2026-02-01T05:00:00")
        snap = self.h.snapshot(now=NOW)
        self.assertGreater(snap["gate"]["correlation"]["by_servo"]["rudder"]["max_abs_r"], 0.5)
        self.assertEqual({a["id"].split(":", 1)[1]: a["severity"] for a in self.alerts(snap, "gate")},
                         {"correlation": "ALARM"})

    def test_no_database_is_alarm(self):
        h = H.Health(os.path.join(self.tmp.name, "missing.db"), self.health_bl)
        snap = h.snapshot(now=NOW)
        self.assertEqual(snap["status"], "red")
        self.assertEqual([a["id"] for a in snap["alerts"]], ["gate:no_db"])


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FeedObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulator_records_cadence_and_finished(self):
        df = make_units(6, "2026-02-01T00:00:00", np.random.default_rng(1))
        df.loc[3:, "build_date_utc"] = "2026-02-01T01:00:00"
        csv = os.path.join(self.tmp.name, "m.csv")
        feed = os.path.join(self.tmp.name, "mes.db")
        df.to_csv(csv, index=False)
        fs.run(csv, feed, "2", max_hours=1, sleep=lambda s: None, quiet=True)
        st = MesSource(feed).feed_status()
        self.assertEqual((st["cadence_s"], st["state"], st["clock"]), (2.0, "running", "2026-02-01T00:00:00"))
        self.assertTrue(st["published_at"])
        fs.run(csv, feed, "max", sleep=lambda s: None, quiet=True)
        st = MesSource(feed).feed_status()
        self.assertEqual((st["cadence_s"], st["state"]), (None, "finished"))
        self.assertIsNone(MesSource(os.path.join(self.tmp.name, "none.db")).feed_status())

    def test_first_heartbeat_written_when_database_created(self):
        frame = make_units(3, "2026-02-01T00:00:00", np.random.default_rng(2))

        class Src:
            calls = 0

            def fetch_new(self, cursor):
                Src.calls += 1
                if Src.calls == 1:
                    return Batch(frame, 1, "2026-02-01T00:00:00")
                return Batch(pd.DataFrame(), cursor, "2026-02-01T00:00:00")

        r = rn.Runner(Src(), os.path.join(self.tmp.name, "g.db"))
        r.heartbeat()          # before the database exists: nothing to write to
        r.run_once()           # creates the database; heartbeat must follow at once
        self.assertTrue(r.store.get_meta("last_poll_at"))
        r.close()

    def test_runner_measures_silence_on_its_monotonic_clock(self):
        clock = FakeClock()
        status = {"clock": "2026-02-01T00:00:00", "cadence_s": 2.0, "state": "running",
                  "published_at": "1999-01-01T00:00:00+00:00"}  # factory wall clock is wrong

        class Src:
            def feed_status(self):
                return dict(status)

            def fetch_new(self, cursor):
                return Batch(pd.DataFrame(), cursor, status["clock"])

        r = rn.Runner(Src(), os.path.join(self.tmp.name, "g.db"), clock=clock)
        self.assertEqual(r.observe_feed()["silent_s"], 0.0)
        clock.t += 5
        self.assertEqual(r.observe_feed()["silent_s"], 5.0)
        status["clock"] = "2026-02-01T01:00:00"   # factory advanced
        clock.t += 1
        self.assertEqual(r.observe_feed()["silent_s"], 0.0)
        clock.t += 7
        self.assertEqual(r.observe_feed()["silent_s"], 7.0)
        r.close()


@unittest.skipUnless(os.path.exists(CSV), "manufacturing.csv not present")
class RealMonthTest(unittest.TestCase):
    """The whole month through the store, then health: known values, the
    rule set's false-alarm count, and a synthetic drifting lot."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "gate.db")
        df = pd.read_csv(CSV, low_memory=False)
        cls.df = df
        cols = columns_from_frame(df)
        cols.update({c: "REAL" for c in spc.MEASURE_COLS})
        store = Store(cls.db_path, columns=cols)
        res, _, _ = spc.classify(df, SCORING)
        cls.res = res
        rows = store.diff(df.to_dict("records"))["new"]
        by_id = dict(zip(res["vehicle_id"], res.to_dict("records")))
        events = {}
        # Holds raised at build time, as the gate does (one batch per build hour)
        for vid in res.loc[res["verdict"] == "QUARANTINE", "vehicle_id"]:
            events[vid] = df.loc[df["vehicle_id"] == vid, "build_date_utc"].iloc[0]
        store.write_batch(rows, [by_id[r["vehicle_id"]] for r in rows],
                          baseline_version=SCORING["version"], sim_time="2026-02-28T23:00:00")
        for vid, t in events.items():
            store.conn.execute("UPDATE events SET at_sim=? WHERE vehicle_id=?", (t, vid))
        store.set_meta({"sim_time": "2026-02-28T23:00:00", "feed_clock": "2026-02-28T23:00:00"})
        store.close()
        cls.h = H.Health(cls.db_path)
        cls.snap = cls.h.snapshot(now=NOW)

    @classmethod
    def tearDownClass(cls):
        cls.h.close()
        cls.tmp.cleanup()

    def all_signals(self, snap=None):
        snap = snap or self.snap
        return [(c["dim"], c["group"], p["day"], p["signal"])
                for c in snap["stability"]["charts"] for p in c["points"] if p["signal"]]

    def test_known_totals(self):
        s = self.snap
        self.assertEqual(s["product"]["verdicts"], {"QUARANTINE": 2, "FLAG": 6498, "PASS": 193500})
        self.assertEqual(s["product"]["saturated_units"], 1028)
        q = s["quality"]
        self.assertEqual((q["final_qc_pass_share"], q["units"]), (1.0, 200000))
        self.assertEqual(q["open_warnings"]["any"]["units"], 59229)
        self.assertEqual(q["open_warnings"]["qc_exception"]["units"], 32296)
        self.assertEqual((q["held_before_final_qc"], q["held_total"]), (2, 2))
        rw = s["throughput"]["rework"]
        self.assertEqual((rw["reworked"], rw["share"]), (39927, 0.1996))
        self.assertEqual({c: v["units"] for c, v in rw["by_code"].items()},
                         {"R12": 9668, "R21": 9627, "R05": 9571, "R33": 9522, "R19": 9473})
        holds = {h["vehicle_id"]: (h["held_at"], h["lead_hours"]) for h in s["product"]["holds"]}
        self.assertEqual(holds, {"GLDR-0421": ("2026-02-06T03:00:00", 1.0),
                                 "GLDR-200000": ("2026-02-18T07:00:00", 1.0)})

    def test_flag_rate_and_group_charts_in_control(self):
        fleet = [p for c in self.snap["stability"]["charts"] if c["dim"] == "fleet" for p in c["points"]]
        self.assertEqual(sum(p["k"] for p in fleet) / sum(p["n"] for p in fleet), 0.0325)
        summ = self.snap["stability"]["summary"]
        self.assertEqual(sum(v["groups"] for v in summ.values()), 158)
        for dim in ("fleet", "plant", "line", "shift", "qc_station"):
            self.assertEqual(summ[dim]["in_control"], summ[dim]["groups"], dim)
        self.assertEqual(summ["line"]["groups"], 6)  # line IDs repeat across plants
        self.assertEqual(len(fleet), 28)
        self.assertAlmostEqual(min(p["p"] for p in fleet), 0.0288, places=4)
        self.assertAlmostEqual(max(p["p"] for p in fleet), 0.0367, places=4)
        # Exact limits for ~6,900-7,400 units a day: alarm at 3.90-3.92%, low at 2.62-2.64%
        self.assertTrue(all(0.0390 <= p["ucl"] <= 0.0393 and 0.0261 <= p["lcl"] <= 0.0264
                            for p in fleet))

    def test_false_alarm_count_on_a_stable_month(self):
        # Computed independently (exact binomial tails, plain pandas) before
        # this module existed: 2 points above, 0 below, 3 runs of 8+ (5 points)
        sig = self.all_signals()
        above = sorted((d, g, day) for d, g, day, s in sig if s == "above")
        self.assertEqual(above, [("aileron_l.lot_id", "L2026-07C", "2026-02-28"),
                                 ("rudder.model", "AM-9", "2026-02-22")])
        self.assertEqual(sum(s == "below" for *_, s in sig), 0)
        self.assertEqual(sum(s == "run" for *_, s in sig), 5)
        # Only signals on the last 2 days drive status: the 02-28 lot point
        active = [(a["severity"], a["group"]) for a in self.snap["alerts"] if a["layer"] == "stability"]
        self.assertEqual(active, [("ALARM", "aileron_l.lot_id L2026-07C")])

    def test_no_throughput_watch_in_any_hour(self):
        hours = sorted(self.snap["throughput"]["by_hour_line"])
        self.assertEqual(len(hours), 672)
        self.assertEqual([h for h in hours if self.h.throughput_watches(h)], [])

    def test_correlation_assumption_holds(self):
        corr = self.snap["gate"]["correlation"]
        self.assertEqual(corr["units"], 20000)
        self.assertLess(corr["max_abs_r"], 0.03)   # 50 pairs at n=20,000: noise ~0.007 each
        full = []
        for s in SERVOS:
            r = np.corrcoef(self.df[[spc.col(s, m) for m in spc.METRICS]].to_numpy(float), rowvar=False)
            np.fill_diagonal(r, 0)
            full.append(np.abs(r).max())
        self.assertAlmostEqual(max(full), 0.0059, places=4)
        self.assertNotIn("gate:correlation", {a["id"] for a in self.snap["alerts"]})

    def test_drifting_lot_raises_alarm_and_others_stay_clean(self):
        """One left-aileron lot's flag rate rises to about 3x from 02-15.
        Applied as changed units through the store, so it also exercises the
        incremental path on 200,000 units."""
        path = os.path.join(self.tmp.name, "drift.db")
        shutil.copy(self.db_path, path)
        store = Store(path)
        lot = "L2026-03B"
        df, res = self.df, self.res
        rng = np.random.default_rng(15)
        pool = df[(df["aileron_l_servo_lot_id"] == lot) & (df["build_date_utc"] >= "2026-02-15")
                  & (res["flag_aileron_l"].to_numpy() == 0)]
        extra = 2 * HB["rates"]["servo_flag"]["aileron_l"]          # +2x on top of normal
        picked = pool[rng.random(len(pool)) < extra].copy()
        c = "aileron_l_servo_sweep_current_ma"
        picked[c] = SCORING["measures"][c]["median"] + 3.5 * SCORING["measures"][c]["sigma"]  # FLAG
        h = H.Health(path)
        h.refresh()                                                  # full load, before the drift
        write(store, picked, "2026-02-28T23:00:00")
        store.close()
        n = h.refresh()                                              # incremental
        self.assertEqual(n, len(picked))
        self.assertTrue(counts_equal(h.counts, h.recount()))
        snap = h.snapshot(now=NOW, refresh=False)
        h.close()
        before = set(self.all_signals())
        after = set(self.all_signals(snap))
        new = sorted(after - before)
        lot_hits = [x for x in new if x[0] == "aileron_l.lot_id" and x[1] == lot]
        self.assertTrue(any(s == "above" for *_, s in lot_hits), new)
        first = min(day for *_, day, s in lot_hits if s == "above")
        self.assertLessEqual(first, "2026-02-22")  # within 8 days (simulated 90th pct)
        # Nothing else changed except charts containing the same units
        # (that servo's vendor / model, and vehicle charts by a few points)
        others = [x for x in new if x[0].startswith("aileron_l.lot_id") and x[1] != lot]
        self.assertEqual(others, [])
        self.__class__.drift_result = {"units": len(picked), "first_alarm": first, "new": new}
        print(f"\n[drift] {len(picked)} units made FLAG in lot {lot} from 02-15; "
              f"first ALARM {first}; new signals {new}")


if __name__ == "__main__":
    unittest.main()
