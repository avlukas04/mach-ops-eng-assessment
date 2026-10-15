"""
Tests for pipeline/store.py: the live database's write rules.

Run from the repository root:
  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from pipeline import store as st  # noqa: E402

COLUMNS = {"vehicle_id": "TEXT", "build_date_utc": "TEXT", "line_id": "TEXT",
           "rework_count": "INTEGER", "aileron_l_servo_sweep_current_ma": "REAL"}


def unit(vid, current=300.0, rework=0):
    return {"vehicle_id": vid, "build_date_utc": "2026-02-06T03:00:00Z",
            "line_id": "LN1", "rework_count": rework,
            "aileron_l_servo_sweep_current_ma": current}


def score(vid, verdict="PASS", reasons=""):
    s = {"vehicle_id": vid, "verdict": verdict, "reasons": reasons,
         "worst_servo": "aileron_l", "worst_d": 1.0, "worst_metric": "sweep_current_ma",
         "worst_z": 1.0, "saturated": False}
    for sv in st.SERVOS:
        s[f"d_{sv}"] = 0.5
        s[f"flag_{sv}"] = 0
    return s


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.db = st.Store(self.path, columns=COLUMNS)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def ingest(self, rows, verdicts=None, sim="2026-02-06T03:00:00Z"):
        d = self.db.diff(rows)
        todo = d["new"] + d["changed"]
        verdicts = verdicts or {}
        scores = [score(r["vehicle_id"], *verdicts.get(r["vehicle_id"], ("PASS", "")))
                  for r in todo]
        return self.db.write_batch(todo, scores, baseline_version="v1", sim_time=sim,
                                   unchanged=d["unchanged"])

    def test_wal_mode(self):
        mode = self.db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")

    def test_reinsert_same_unit_changes_nothing(self):
        self.ingest([unit("A"), unit("B")])
        r = self.ingest([unit("A"), unit("B")])
        self.assertEqual((r["new"], r["changed"], r["unchanged"]), (0, 0, 2))
        self.assertEqual(self.db.table_count("units"), 2)
        self.assertEqual(self.db.table_count("score_history"), 2)

    def test_fingerprint_ignores_key_order_and_int_float(self):
        a = unit("A")
        b = dict(reversed(list(a.items())))
        b["rework_count"] = 0.0
        self.assertEqual(st.fingerprint(a), st.fingerprint(b))
        self.assertNotEqual(st.fingerprint(a), st.fingerprint(unit("A", rework=1)))

    def test_changed_unit_is_rescored_and_history_kept(self):
        self.ingest([unit("A")])
        r = self.ingest([unit("A", current=420.0, rework=1)],
                        verdicts={"A": ("QUARANTINE", "SIGNATURE aileron_l D=7.0")},
                        sim="2026-02-07T00:00:00Z")
        self.assertEqual((r["new"], r["changed"]), (0, 1))
        self.assertEqual(self.db.get_unit("A")["rework_count"], 1)
        self.assertEqual(self.db.get_unit("A")["first_seen_sim"], "2026-02-06T03:00:00Z")
        self.assertEqual(self.db.get_score("A")["verdict"], "QUARANTINE")
        hist = [h["verdict"] for h in self.db.get_score_history("A")]
        self.assertEqual(hist, ["PASS", "QUARANTINE"])

    def test_quarantine_raises_one_event(self):
        q = {"A": ("QUARANTINE", "SIGNATURE aileron_l D=7.0")}
        r = self.ingest([unit("A")], verdicts=q)
        self.assertEqual(r["quarantines_raised"], 1)
        ev = self.db.get_events("A")
        self.assertEqual([(e["event_type"], e["actor"], e["at_sim"]) for e in ev],
                         [("QUARANTINE_RAISED", "gate", "2026-02-06T03:00:00Z")])
        # Re-scored while still held: no second hold event
        self.ingest([unit("A", current=500.0)], verdicts=q)
        self.assertEqual(len(self.db.get_events("A")), 1)

    def test_failed_batch_writes_nothing(self):
        rows = self.db.diff([unit("A"), unit("B")])["new"]
        bad = [score("A"), score("B", verdict=None)]  # NOT NULL fails on the 2nd unit
        with self.assertRaises(ValueError):
            self.db.write_batch(rows, bad, baseline_version="v1", meta={"cursor": 5})
        for t in ("units", "scores", "score_history", "events", "runs"):
            self.assertEqual(self.db.table_count(t), 0, t)
        self.assertIsNone(self.db.get_meta("cursor"))

    def test_scores_must_match_units(self):
        rows = self.db.diff([unit("A")])["new"]
        with self.assertRaises(ValueError):
            self.db.write_batch(rows, [score("B")], baseline_version="v1")

    def test_unknown_column_rejected(self):
        rows = self.db.diff([dict(unit("A"), surprise=1)])["new"]
        with self.assertRaises(ValueError):
            self.db.write_batch(rows, [score("A")], baseline_version="v1")

    def test_human_event_requires_who_and_why(self):
        self.ingest([unit("A")])
        with self.assertRaises(ValueError):
            self.db.add_event("A", "RELEASED", "", "ok")
        with self.assertRaises(ValueError):
            self.db.add_event("A", "RELEASED", "J. Doe", "  ")
        with self.assertRaises(ValueError):
            self.db.add_event("A", "QUARANTINE_RAISED", "J. Doe", "only the gate raises holds")
        with self.assertRaises(KeyError):
            self.db.add_event("NOPE", "RELEASED", "J. Doe", "ok")
        self.db.add_event("A", "RETEST_PASS", "J. Doe", "bench retest 3x, D < 2",
                          at_sim="2026-02-06T05:00:00Z")
        e = self.db.get_events("A")[-1]
        self.assertEqual((e["event_type"], e["actor"], e["note"], e["at_sim"]),
                         ("RETEST_PASS", "J. Doe", "bench retest 3x, D < 2",
                          "2026-02-06T05:00:00Z"))
        self.assertTrue(e["created_at"])

    def test_run_errors_and_meta_recorded(self):
        rows = self.db.diff([unit("A")])["new"]
        r = self.db.write_batch(rows, [score("A")], baseline_version="v1",
                                sim_time="2026-02-06T03:00:00Z",
                                errors=[("", "vehicle_id empty", {"vehicle_id": ""})],
                                meta={"cursor": 1, "sim_time": "2026-02-06T03:00:00Z"})
        run = dict(self.db.conn.execute("SELECT * FROM runs").fetchone())
        self.assertEqual((run["rows_read"], run["new"], run["rejected"]), (2, 1, 1))
        err = self.db.conn.execute("SELECT run_id, reason FROM ingest_errors").fetchone()
        self.assertEqual(tuple(err), (r["run_id"], "vehicle_id empty"))
        self.assertEqual(self.db.get_meta("cursor"), 1)

    def test_reopen_resumes(self):
        self.ingest([unit("A")])
        self.db.close()
        self.db = st.Store(self.path)  # no columns needed once created
        self.assertEqual(self.db.table_count("units"), 1)
        self.assertEqual(self.ingest([unit("A")])["unchanged"], 1)

    def test_reader_sees_commits_while_writer_works(self):
        reader = st.Store(self.path, readonly=True)
        try:
            self.ingest([unit("A")])
            self.assertEqual(reader.verdict_counts()["PASS"], 1)
            with self.assertRaises(RuntimeError):
                reader.write_batch([], [], baseline_version="v1")
            # Reads from another thread during a write do not error
            errors = []

            def read_loop():
                try:
                    for _ in range(50):
                        reader.verdict_counts()
                except Exception as e:  # pragma: no cover
                    errors.append(e)
            t = threading.Thread(target=read_loop)
            t.start()
            self.ingest([unit(f"U{i}") for i in range(500)])
            t.join()
            self.assertEqual(errors, [])
        finally:
            reader.close()


class RealDataTest(unittest.TestCase):
    """The store round-trips real rows and real detector output exactly."""

    CSV = os.path.join(ROOT, "manufacturing.csv")

    @unittest.skipUnless(os.path.exists(CSV), "manufacturing.csv not present")
    def test_real_rows_round_trip(self):
        import pandas as pd
        import spc_detector as spc
        df = pd.read_csv(self.CSV, nrows=3000)
        ids = pd.read_csv(self.CSV, usecols=["vehicle_id"])["vehicle_id"]
        pos = int(ids.index[ids == "GLDR-0421"][0])  # the CSV is not in ID order
        target = pd.read_csv(self.CSV, skiprows=range(1, pos + 1), nrows=1)
        self.assertEqual(target.vehicle_id[0], "GLDR-0421")
        df = pd.concat([df[df.vehicle_id != "GLDR-0421"], target], ignore_index=True)
        base = spc.load_baseline(spc.DEFAULT_BASELINE)
        res, _, _ = spc.classify(df, base)
        with tempfile.TemporaryDirectory() as tmp:
            db = st.Store(os.path.join(tmp, "r.db"), columns=st.columns_from_frame(df))
            rows = df.to_dict("records")
            d = db.diff(rows)
            by_id = {r["vehicle_id"]: r for r in res.to_dict("records")}
            db.write_batch(d["new"], [by_id[r["vehicle_id"]] for r in d["new"]],
                           baseline_version=base["version"])
            self.assertEqual(db.table_count("units"), len(df))
            self.assertEqual(db.verdict_counts(),
                             {v: int((res.verdict == v).sum()) for v in st.VERDICTS})
            s = db.get_score("GLDR-0421")
            self.assertEqual((s["verdict"], s["worst_servo"]), ("QUARANTINE", "aileron_l"))
            self.assertEqual(s["baseline_version"], base["version"])
            u = db.get_unit("GLDR-0421")
            src = target.iloc[0]
            for c in df.columns:
                want = src[c]
                if pd.isna(want):
                    self.assertIsNone(u[c], c)
                else:
                    self.assertEqual(u[c], want.item() if hasattr(want, "item") else want, c)
            # Re-reading the same rows from the CSV is a no-op
            self.assertEqual(db.diff(df.to_dict("records"))["unchanged"], len(df))
            db.close()


if __name__ == "__main__":
    unittest.main()
