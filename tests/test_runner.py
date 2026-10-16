"""
Tests for pipeline/runner.py: validation, reliability, baseline control,
and the golden end-to-end run on the real data.

Run from the repository root:
  python3 -m unittest discover -s tests -v
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import factory_simulator as fs  # noqa: E402
import spc_detector as spc  # noqa: E402
from pipeline import runner as rn  # noqa: E402
from pipeline.source import Batch, MesSource  # noqa: E402

CSV = os.path.join(ROOT, "manufacturing.csv")
with open(spc.DEFAULT_BASELINE) as _f:
    BASE = json.load(_f)
logging.getLogger("gate").setLevel(logging.CRITICAL)  # keep test output readable


def unit(vid, hour=0, defect=False, **overrides):
    """A unit at the baseline median (PASS), or with a stiff left aileron."""
    u = {"vehicle_id": vid, "build_date_utc": f"2026-02-01T{hour:02d}:00:00",
         "build_shift": "A", "factory_site": "PLANT", "line_id": "LN1",
         "rework_count": 0, "rework_codes": None, "final_qc_pass": True,
         "final_qc_timestamp_utc": f"2026-02-01T{hour + 1:02d}:00:00"}
    for c in spc.MEASURE_COLS:
        m = BASE["measures"][c]
        u[c] = m["median"]
        if defect and c.startswith("aileron_l_"):
            u[c] = m["median"] + 4 * m["sigma"]  # D = sqrt(5 * 16) = 8.9 -> SIGNATURE
    u.update(overrides)
    return u


class ScriptedSource:
    """Returns the scripted frames in order; cursor = index of the next one."""

    def __init__(self, frames):
        self.frames = [pd.DataFrame(f) for f in frames]

    def fetch_new(self, cursor):
        i = cursor or 0
        if i >= len(self.frames):
            return Batch(pd.DataFrame(), i, "2026-02-01T23:00:00")
        f = self.frames[i]
        return Batch(f, i + 1, str(f["build_date_utc"].iloc[0]) if "build_date_utc" in f else None)


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "gate.db")
        self.baseline = os.path.join(self.tmp.name, "baseline.json")
        shutil.copy(spc.DEFAULT_BASELINE, self.baseline)
        self.runners = []

    def tearDown(self):
        for r in self.runners:
            r.close()
        self.tmp.cleanup()

    def runner(self, source, **kw):
        kw.setdefault("poll_s", 0.01)
        r = rn.Runner(source, self.db, self.baseline, **kw)
        self.runners.append(r)
        return r

    def errors(self, r):
        return [tuple(x) for x in r.store.conn.execute(
            "SELECT vehicle_id, reason FROM ingest_errors ORDER BY id")]


# ---------------------------------------------------------------- validation

class ValidationTest(RunnerTestBase):
    def test_blank_id_rejected_and_logged(self):
        r = self.runner(ScriptedSource([[unit("A"), unit("")]]))
        r.run_once()
        self.assertEqual(r.store.table_count("units"), 1)
        self.assertEqual(len(self.errors(r)), 1)
        self.assertIn("blank vehicle_id", self.errors(r)[0][1])
        run = r.store.conn.execute("SELECT rows_read, new, rejected FROM runs").fetchone()
        self.assertEqual(tuple(run), (2, 1, 1))

    def test_non_numeric_reading_quarantined_and_logged(self):
        col = "rudder_servo_sweep_time_ms"
        r = self.runner(ScriptedSource([[unit("A"), unit("B", **{col: "ERR"})]]))
        r.run_once()
        s = r.store.get_score("B")
        self.assertEqual(s["verdict"], "QUARANTINE")
        self.assertIn("INCOMPLETE", s["reasons"])
        self.assertIsNone(r.store.get_unit("B")[col])  # stored as not measured
        (vid, reason), = self.errors(r)
        self.assertEqual(vid, "B")
        self.assertIn(f"{col}='ERR'", reason)
        self.assertEqual(r.store.conn.execute("SELECT rejected FROM runs").fetchone()[0], 0)
        self.assertEqual(r.store.get_events("B")[0]["event_type"], "QUARANTINE_RAISED")

    def test_blank_reading_quarantined_not_logged(self):
        r = self.runner(ScriptedSource([[unit("A", **{"elevon_l_servo_peak_current_ma": None})]]))
        r.run_once()
        self.assertIn("INCOMPLETE", r.store.get_score("A")["reasons"])
        self.assertEqual(self.errors(r), [])

    def test_median_unit_passes_defect_quarantines(self):
        r = self.runner(ScriptedSource([[unit("A"), unit("D", defect=True)]]))
        r.run_once()
        self.assertEqual(r.store.get_score("A")["verdict"], "PASS")
        s = r.store.get_score("D")
        self.assertEqual((s["verdict"], s["worst_servo"]), ("QUARANTINE", "aileron_l"))
        self.assertIn("SIGNATURE", s["reasons"])

    def test_missing_column_stalls_then_recovers(self):
        good = [unit("A")]
        bad = [{k: v for k, v in unit("B", hour=1).items() if k != "aileron_l_servo_sweep_time_ms"}]
        src = ScriptedSource([good, bad])
        r = self.runner(src)
        r.run_once()
        for _ in range(3):  # retries: same cursor, logged once
            with self.assertRaises(rn.SchemaError):
                r.run_once()
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        r.run_forever(stop)  # catches, stalls, backs off
        self.assertEqual(r.status["state"], "STALLED")
        self.assertEqual(r.cursor, 1)
        self.assertEqual(r.store.get_meta("cursor"), 1)
        self.assertEqual(sum("missing required column" in e[1] for e in self.errors(r)), 1)
        self.assertEqual(r.store.get_meta("runner_status")["state"], "STALLED")
        src.frames[1] = pd.DataFrame([unit("B", hour=1)])  # feed fixed
        r.run_once()
        self.assertEqual((r.cursor, r.status["state"]), (2, "RUNNING"))
        self.assertEqual(r.store.get_score("B")["verdict"], "PASS")

    def test_extra_column_dropped_and_logged_once(self):
        r = self.runner(ScriptedSource([[unit("A")], [unit("B", hour=1, surprise=1)],
                                        [unit("C", hour=2, surprise=2)]]))
        for _ in range(3):
            r.run_once()
        self.assertEqual(r.store.table_count("units"), 3)
        self.assertEqual(sum("surprise" in e[1] for e in self.errors(r)), 1)


# --------------------------------------------------------------- reliability

class ReliabilityTest(RunnerTestBase):
    def test_failed_batch_writes_nothing_and_retries(self):
        r = self.runner(ScriptedSource([[unit("A")], [unit("D", hour=1, defect=True)]]))
        r.run_once()
        real = r.store.write_batch
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk hiccup")
            return real(*a, **kw)
        r.store.write_batch = flaky
        with self.assertRaises(RuntimeError):
            r.run_once()
        self.assertEqual((r.cursor, r.store.table_count("units")), (1, 1))
        r.run_once()
        self.assertEqual(r.cursor, 2)
        self.assertEqual(len(r.store.get_events("D")), 1)

    def test_backoff_doubles_and_caps(self):
        r = self.runner(ScriptedSource([[{"vehicle_id": "X"}]]))  # always a schema error
        waits = []

        class FakeStop:
            def is_set(self):
                return len(waits) >= 7

            def wait(self, s):
                waits.append(s)
        r.run_forever(FakeStop())
        self.assertEqual(waits, [1, 2, 4, 8, 16, 30, 30])

    def test_idle_heartbeat_without_runs_rows(self):
        r = self.runner(ScriptedSource([[unit("A")]]), heartbeat_s=0)
        r.run_once()
        before = r.store.get_meta("last_poll_at")
        time.sleep(1.1)
        self.assertEqual(r.run_once(), 0)
        self.assertEqual(r.store.table_count("runs"), 1)
        self.assertGreater(r.store.get_meta("last_poll_at"), before)
        self.assertEqual(r.store.get_meta("feed_clock"), "2026-02-01T23:00:00")
        self.assertEqual(r.status["state"], "IDLE")

    def test_rework_rescored_and_new_hold_only_on_change(self):
        frames = [[unit("A"), unit("D", defect=True)],
                  [unit("D", hour=1, defect=True, rework_count=1)],   # still held: no new hold
                  [unit("A", hour=1, defect=True, rework_count=1)]]   # newly held
        r = self.runner(ScriptedSource(frames))
        for _ in range(3):
            r.run_once()
        self.assertEqual([h["verdict"] for h in r.store.get_score_history("D")],
                         ["QUARANTINE", "QUARANTINE"])
        self.assertEqual(len(r.store.get_events("D")), 1)
        self.assertEqual([h["verdict"] for h in r.store.get_score_history("A")],
                         ["PASS", "QUARANTINE"])
        self.assertEqual(len(r.store.get_events("A")), 1)

    def test_duration_immune_to_wall_clock_jumps(self):
        from unittest import mock
        mono = iter(x * 0.25 for x in range(100))   # +250 ms per reading
        wall = iter(f"2026-03-01T12:00:{59 - k:02d}.000+00:00" for k in range(100))  # runs backwards
        r = self.runner(ScriptedSource([[unit("A")]]), clock=lambda: next(mono))
        got = []
        r.listeners.append(got.append)
        with mock.patch("pipeline.store.utc_now", lambda: next(wall)), \
                mock.patch("pipeline.runner.utc_now", lambda: next(wall)):
            r.run_once()
        run = dict(r.store.conn.execute("SELECT started_at, finished_at, duration_ms FROM runs").fetchone())
        self.assertLess(run["finished_at"], run["started_at"])  # wall clock went backwards...
        self.assertEqual(run["duration_ms"], 250)                # ...duration is still right
        self.assertEqual(got[0]["duration_ms"], 500)              # summary: start to after commit

    def test_broken_listener_does_not_stop_gate(self):
        def boom(_):
            raise RuntimeError("dashboard down")
        got = []
        r = self.runner(ScriptedSource([[unit("D", defect=True)]]), listeners=[boom, got.append])
        r.run_once()
        self.assertEqual(r.store.get_score("D")["verdict"], "QUARANTINE")
        h, = got[0]["holds"]
        self.assertEqual((h["vehicle_id"], h["held_at"], h["final_qc_at"]),
                         ("D", "2026-02-01T00:00:00", "2026-02-01T01:00:00"))


# ------------------------------------------------- feed integration (real I/O)

class FeedIntegrationTest(RunnerTestBase):
    def write_csv(self, n_hours=6, per_hour=5):
        rows = [unit(f"V-{h}-{k}", hour=h, defect=(h == 3 and k == 0))
                for h in range(n_hours) for k in range(per_hour)]
        path = os.path.join(self.tmp.name, "m.csv")
        pd.DataFrame(rows).sample(frac=1, random_state=0).to_csv(path, index=False)
        return path

    def test_restart_matches_uninterrupted(self):
        csv = self.write_csv()
        feed = os.path.join(self.tmp.name, "mes.db")
        fs.run(csv, feed, "max", quiet=True)
        a = self.runner(MesSource(feed))
        for _ in range(3):
            a.run_once()
        a.close()
        self.runners.remove(a)
        b = self.runner(MesSource(feed))  # restarted gate
        self.assertEqual(b.cursor, 15)
        b.run_forever(until_idle=True)
        ref_db = os.path.join(self.tmp.name, "ref.db")
        ref = rn.Runner(MesSource(feed), ref_db, self.baseline)
        ref.run_forever(until_idle=True)
        q = "SELECT vehicle_id, verdict, reasons, scored_sim FROM scores ORDER BY vehicle_id"
        self.assertEqual(b.store.conn.execute(q).fetchall(), ref.store.conn.execute(q).fetchall())
        self.assertEqual(b.store.table_count("units"), 30)
        ref.close()

    def test_replaced_feed_stalls_instead_of_going_silent(self):
        csv = self.write_csv(n_hours=2)
        feed = os.path.join(self.tmp.name, "mes.db")
        fs.run(csv, feed, "max", quiet=True)
        r = self.runner(MesSource(feed))
        r.run_forever(until_idle=True)
        self.assertEqual(r.cursor, 10)
        r.source.close()
        fs.run(csv, feed, "max", reset=True, quiet=True)  # factory feed recreated
        with self.assertRaises(rn.FeedChangedError):
            r.run_once()
        self.assertEqual(r.cursor, 10)

    def test_factory_and_gate_running_at_the_same_time(self):
        csv = self.write_csv(n_hours=10)
        feed = os.path.join(self.tmp.name, "mes.db")
        stop = threading.Event()
        r = self.runner(MesSource(feed), poll_s=0.01)
        gate = threading.Thread(target=r.run_forever, args=(stop,))
        gate.start()
        fs.run(csv, feed, speed=0.05, quiet=True)  # publishes while the gate polls
        deadline = time.time() + 10
        while r.cursor != 50 and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        gate.join()
        self.assertEqual(r.store.table_count("units"), 50)
        self.assertEqual(r.store.verdict_counts(), {"PASS": 49, "FLAG": 0, "QUARANTINE": 1})
        self.assertEqual(r.store.get_events("V-3-0")[0]["at_sim"], "2026-02-01T03:00:00")
        self.assertEqual(r.store.table_count("runs"), 10)  # one per published hour


# ---------------------------------------------------------- baseline control

class BaselineControlTest(RunnerTestBase):
    def edit_baseline(self, version=None, **rules):
        with open(self.baseline) as f:
            b = json.load(f)
        if version:
            b["version"] = version
        b["rules"].update(rules)
        with open(self.baseline, "w") as f:
            json.dump(b, f)

    def test_pinned_drift_warns_but_scoring_unchanged(self):
        # D for this unit is 2.0 * sqrt(5) = 4.47: PASS under quarantine_d 6, FLAG d 4.5
        mild = {c: BASE["measures"][c]["median"] + 2.0 * BASE["measures"][c]["sigma"]
                for c in spc.MEASURE_COLS if c.startswith("rudder_")}
        frames = [[unit("A", **mild)], [unit("B", hour=1, **mild)]]
        r = self.runner(ScriptedSource(frames))
        r.run_once()
        self.edit_baseline(version="test-lower", quarantine_d=3.0)
        st = r.check_baseline_drift()
        self.assertEqual((st["on_disk"]["state"], st["on_disk"]["version"]), ("differs", "test-lower"))
        r.run_once()
        self.assertEqual(r.store.get_score("B")["verdict"], r.store.get_score("A")["verdict"])
        self.assertEqual(r.store.get_score("B")["baseline_version"], BASE["version"])
        os.remove(self.baseline)
        self.assertEqual(r.check_baseline_drift()["on_disk"]["state"], "missing")

    def test_restart_promotes_and_records(self):
        r = self.runner(ScriptedSource([[unit("A")]]))
        r.run_once()
        r.close()
        self.runners.remove(r)
        self.edit_baseline(version="test-v2")
        r2 = self.runner(ScriptedSource([[unit("A")]]))
        rows = r2.store.conn.execute(
            "SELECT version, previous_version FROM baselines ORDER BY id").fetchall()
        self.assertEqual([tuple(x) for x in rows], [(BASE["version"], None), ("test-v2", BASE["version"])])
        self.assertEqual(r2.store.count_other_baselines("test-v2"), 1)

    def test_same_version_different_content_refused(self):
        r = self.runner(ScriptedSource([[unit("A")]]))
        r.run_once()
        r.close()
        self.runners.remove(r)
        self.edit_baseline(quarantine_d=5.0)  # content changed, version not
        with self.assertRaises(ValueError) as cm:
            self.runner(ScriptedSource([]))
        self.assertIn("new version", str(cm.exception))

    def test_rescore_keeps_history_and_raises_new_holds(self):
        mild = {c: BASE["measures"][c]["median"] + 2.0 * BASE["measures"][c]["sigma"]
                for c in spc.MEASURE_COLS if c.startswith("rudder_")}
        r = self.runner(ScriptedSource([[unit("A", **mild), unit("D", defect=True)]]))
        r.run_once()
        self.assertEqual(r.store.get_score("A")["verdict"], "PASS")
        r.close()
        self.runners.remove(r)
        self.edit_baseline(version="test-strict", quarantine_d=4.0)
        r2 = self.runner(ScriptedSource([]))
        out = r2.rescore()
        self.assertEqual((out["rescored"], out["holds"]), (2, ["A"]))  # D already held
        self.assertEqual([(h["verdict"], h["baseline_version"]) for h in r2.store.get_score_history("A")],
                         [("PASS", BASE["version"]), ("QUARANTINE", "test-strict")])
        self.assertEqual(len(r2.store.get_events("D")), 1)
        self.assertEqual(r2.rescore()["rescored"], 0)  # idempotent


# ----------------------------------------------------------------- golden run

@unittest.skipUnless(os.path.exists(CSV), "manufacturing.csv not present")
class GoldenRunTest(unittest.TestCase):
    """The whole month: simulator (with rework and bad-row injection) -> feed
    -> runner, compared vehicle for vehicle with classify on the full CSV."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.feed = feed = os.path.join(cls.tmp.name, "mes.db")
        fs.run(CSV, feed, "max", rework=1, bad=2, quiet=True)
        cls.holds = []
        cls.r = rn.Runner(MesSource(feed), os.path.join(cls.tmp.name, "gate.db"),
                          listeners=[lambda s: cls.holds.extend(s["holds"])])
        t = time.time()
        cls.r.run_forever(until_idle=True)
        cls.elapsed = time.time() - t
        df = pd.read_csv(CSV)
        cls.expected, _, _ = spc.classify(df, spc.load_baseline(spc.DEFAULT_BASELINE))
        cls.qc = df.set_index("vehicle_id")[["final_qc_timestamp_utc", "final_qc_pass"]]

    @classmethod
    def tearDownClass(cls):
        cls.r.close()
        cls.tmp.cleanup()

    def scores(self):
        return pd.read_sql_query("SELECT vehicle_id, verdict, reasons, worst_d FROM scores",
                                 self.r.store.conn)

    def test_real_units_match_detector_vehicle_for_vehicle(self):
        got = self.scores()
        got = got[got.vehicle_id.str.startswith("GLDR-")].set_index("vehicle_id").sort_index()
        exp = self.expected.set_index("vehicle_id")[["verdict", "reasons", "worst_d"]].sort_index()
        self.assertEqual(len(got), 200_000)
        self.assertEqual(got["verdict"].value_counts().to_dict(),
                         {"PASS": 193_500, "FLAG": 6_498, "QUARANTINE": 2})
        self.assertTrue((got["verdict"] == exp["verdict"]).all())
        self.assertTrue((got["reasons"] == exp["reasons"]).all())
        self.assertTrue((got["worst_d"] == exp["worst_d"]).all())
        print(f"\n  [golden] 200,000 real units: 193,500 PASS / 6,498 FLAG / 2 QUARANTINE, "
              f"identical to classify() on every unit; gate time {self.elapsed:.0f} s")

    def test_holds_before_final_qc_and_qc_passed_them(self):
        held = {h["vehicle_id"]: h for h in self.holds if h["vehicle_id"].startswith("GLDR-")}
        self.assertEqual(set(held), {"GLDR-0421", "GLDR-200000"})
        for vid, at in (("GLDR-0421", "2026-02-06T03:00:00"), ("GLDR-200000", "2026-02-18T07:00:00")):
            self.assertEqual(held[vid]["held_at"], at)
            self.assertLess(held[vid]["held_at"], held[vid]["final_qc_at"])
            self.assertEqual(held[vid]["final_qc_at"], self.qc.at[vid, "final_qc_timestamp_utc"])
            ev = [e for e in self.r.store.get_events(vid) if e["event_type"] == "QUARANTINE_RAISED"]
            self.assertEqual([e["at_sim"] for e in ev], [at])  # one hold, despite any rework
            print(f"  [golden] {vid} held {at}, final QC {held[vid]['final_qc_at']} "
                  f"(QC result {held[vid]['final_qc_pass']})")
        self.assertTrue(all(self.qc.loc[list(held), "final_qc_pass"].astype(str).str.lower()
                            .isin(["true", "1", "pass", "1.0"])))

    def test_injected_bad_rows_handled(self):
        errs = pd.read_sql_query("SELECT vehicle_id, reason FROM ingest_errors", self.r.store.conn)
        blank = errs.reason.str.startswith("blank vehicle_id").sum()
        bad = errs.reason.str.contains("non-numeric").sum()
        sim = self.scores()
        sim = sim[sim.vehicle_id.str.startswith("SIMBAD-")]
        self.assertEqual((blank, bad, len(sim)), (672, 672, 672))
        self.assertTrue((sim.verdict == "QUARANTINE").all())
        self.assertTrue(sim.reasons.str.contains("INCOMPLETE").all())
        print(f"  [golden] injected: {blank} blank-ID rows rejected and logged; "
              f"{bad} 'ERR' readings logged and quarantined INCOMPLETE")

    def test_rework_rescored_with_history(self):
        import sqlite3
        feed = sqlite3.connect(self.feed)
        rows, distinct = feed.execute(
            "SELECT COUNT(*), COUNT(DISTINCT vehicle_id) FROM production_units "
            "WHERE rework_codes LIKE '%SIM%'").fetchone()
        feed.close()
        self.assertEqual(rows, 671)  # 1 republish per hour after the first
        # A unit picked twice is republished identically the second time:
        # same fingerprint, so it is ignored, not re-scored again.
        hist = self.r.store.table_count("score_history")
        self.assertEqual(hist, 200_000 + 672 + distinct)
        multi = self.r.store.conn.execute(
            "SELECT COUNT(*) FROM (SELECT vehicle_id FROM score_history WHERE vehicle_id IN "
            "(SELECT vehicle_id FROM units WHERE rework_codes LIKE '%SIM%') "
            "GROUP BY vehicle_id HAVING COUNT(*) = 2)").fetchone()[0]
        self.assertEqual(multi, distinct)  # every reworked unit: original + one re-score
        print(f"  [golden] {rows} rework republishes of {distinct} units: each re-scored once, "
              f"{rows - distinct} identical repeat(s) ignored; score_history {hist:,} rows")

if __name__ == "__main__":
    unittest.main()
