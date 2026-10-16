"""
Tests for pipeline/source.py and tools/factory_simulator.py.

Run from the repository root:
  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import factory_simulator as fs  # noqa: E402
from pipeline import source as src  # noqa: E402

CSV = os.path.join(ROOT, "manufacturing.csv")


def synthetic(n_hours=4, per_hour=3):
    """Small fleet, deliberately shuffled like the real file."""
    rows = []
    for h in range(n_hours):
        for k in range(per_hour):
            rows.append({"vehicle_id": f"V-{h}-{k}",
                         "build_date_utc": f"2026-02-01T{h:02d}:00:00",
                         "line_id": "LN1", "rework_count": 0, "rework_codes": None,
                         "aileron_l_servo_sweep_current_ma": 300.0 + k})
    return pd.DataFrame(rows).sample(frac=1, random_state=1).reset_index(drop=True)


def drain(mes, cursor=0):
    """Read the feed to the end; returns (batches, idle batch)."""
    out = []
    while True:
        b = mes.fetch_new(cursor)
        if not len(b):
            return out, b
        out.append(b)
        cursor = b.cursor


class FakeClock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 6))
        self.t += s


# ---------------------------------------------------------------- pacing

class PacerTest(unittest.TestCase):
    def test_max_never_sleeps(self):
        fc = FakeClock()
        p = src.Pacer("max", sleep=fc.sleep, clock=fc.clock)
        for _ in range(5):
            p.wait()
        self.assertEqual(fc.slept, [])

    def test_processing_time_is_absorbed(self):
        fc = FakeClock()
        p = src.Pacer(2, sleep=fc.sleep, clock=fc.clock)
        p.wait()             # tick 1 due at t=2
        fc.t += 0.5          # 0.5 s of processing
        p.wait()             # tick 2 due at t=4 -> sleeps 1.5
        fc.t += 3.0          # overran: now t=7, tick 3 was due at 6
        p.wait()             # no sleep, and no drift added
        self.assertEqual(fc.slept, [2.0, 1.5])

    def test_bad_speed_rejected(self):
        for bad in (0, -1, "fast"):
            with self.assertRaises(ValueError):
                src.Pacer(bad)


# ---------------------------------------------------------------- replay

class ReplayTest(unittest.TestCase):
    def test_shuffled_input_comes_out_in_hour_order(self):
        r = src.ReplaySource(frame=synthetic())
        cursor, hours = 0, []
        while not r.exhausted(cursor):
            b = r.fetch_new(cursor)
            self.assertTrue((b.frame["build_date_utc"] == b.sim_time).all())
            hours.append(b.sim_time)
            cursor = b.cursor
        self.assertEqual(hours, [f"2026-02-01T{h:02d}:00:00" for h in range(4)])

    def test_exhausted_source_returns_empty(self):
        r = src.ReplaySource(frame=synthetic(n_hours=1))
        b = r.fetch_new(1)
        self.assertEqual((len(b), b.cursor), (0, 1))

    def test_missing_required_column(self):
        with self.assertRaises(ValueError):
            src.ReplaySource(frame=synthetic().drop(columns=["build_date_utc"]))


# ------------------------------------------------------- simulator + MES

class MesFeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = os.path.join(self.tmp.name, "m.csv")
        self.feed = os.path.join(self.tmp.name, "mes.db")
        synthetic().to_csv(self.csv, index=False)

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, **kw):
        kw.setdefault("speed", "max")
        return fs.run(self.csv, self.feed, quiet=True, **kw)

    def test_gate_started_before_factory_gets_empty_batches(self):
        mes = src.MesSource(self.feed)
        b = mes.fetch_new(0)
        self.assertEqual((len(b), b.cursor, b.sim_time), (0, 0, None))
        self.publish()
        batches, _ = drain(mes)
        self.assertEqual(sum(map(len, batches)), 12)
        mes.close()

    def test_late_gate_catches_up_in_bounded_batches(self):
        self.publish()
        mes = src.MesSource(self.feed, limit=5)
        sizes, cursor = [], 0
        while True:
            b = mes.fetch_new(cursor)
            if not len(b):
                break
            sizes.append(len(b))
            cursor = b.cursor
        self.assertEqual(sizes, [3, 3, 3, 3])  # one published hour per batch
        self.assertEqual(b.sim_time, "2026-02-01T03:00:00")  # feed clock when idle
        mes.close()

    def test_batch_never_spans_hours(self):
        self.publish()
        mes = src.MesSource(self.feed, limit=100)
        cursor = 0
        for h in range(4):
            b = mes.fetch_new(cursor)
            self.assertEqual(b.sim_time, f"2026-02-01T{h:02d}:00:00")
            self.assertTrue((b.frame["build_date_utc"] == b.sim_time).all())
            cursor = b.cursor
        mes.close()

    def test_factory_resumes_where_it_stopped(self):
        self.publish(max_hours=2)
        self.assertEqual(self.publish(), 4)
        mes = src.MesSource(self.feed)
        ids = pd.concat([b.frame["vehicle_id"] for b in drain(mes)[0]])
        self.assertEqual((len(ids), ids.is_unique), (12, True))
        mes.close()

    def test_gate_resume_matches_uninterrupted(self):
        self.publish()
        a = src.MesSource(self.feed, limit=2)
        first = [a.fetch_new(0)]
        first.append(a.fetch_new(first[0].cursor))
        a.close()
        b = src.MesSource(self.feed, limit=2)  # "restarted" gate, saved cursor
        rest, _ = drain(b, first[-1].cursor)
        b.close()
        c = src.MesSource(self.feed)  # uninterrupted reference
        whole, _ = drain(c)
        c.close()
        ids = lambda bs: [v for x in bs for v in x.frame.vehicle_id]  # noqa: E731
        self.assertEqual(ids(first + rest), ids(whole))
        self.assertEqual([x.sim_time for x in first + rest if len(x)],
                         [f"2026-02-01T{h:02d}:00:00" for h in (0, 0, 1, 1, 2, 2, 3, 3)])

    def test_pacing_between_hours(self):
        fc = FakeClock()
        fs.run(self.csv, self.feed, speed=2, sleep=fc.sleep, quiet=True)
        self.assertEqual(len(fc.slept), 3)  # 4 hours -> 3 waits, none after the last

    def test_rework_republished_newest_wins(self):
        self.publish(rework=2, seed=3)
        mes = src.MesSource(self.feed)
        batches, _ = drain(mes)
        mes.close()
        f = pd.concat([b.frame for b in batches])
        reworked = f[f["rework_codes"].fillna("").str.contains("SIM")]
        # 2 reworks in each of hours 1-3, each of a unit published earlier
        self.assertEqual(len(reworked), 6)
        self.assertTrue((reworked["rework_count"] == 1).all())
        self.assertEqual(f["vehicle_id"].nunique(), 12)
        self.assertEqual(len(f), 18)

    def test_bad_rows_delivered_for_runner_to_reject(self):
        self.publish(bad=3, max_hours=1)
        mes = src.MesSource(self.feed)
        f = mes.fetch_new(0).frame
        mes.close()
        self.assertEqual((f["vehicle_id"] == "").sum(), 2)  # blank IDs are never merged
        bad = f[f["vehicle_id"].str.startswith("SIMBAD-")]
        self.assertEqual(bad[fs.BAD_READING_COL].tolist(), ["ERR"])

    def test_feed_columns_match_csv(self):
        self.publish(max_hours=1)
        mes = src.MesSource(self.feed)
        self.assertEqual(list(mes.columns()), list(synthetic().columns))
        mes.close()


# ------------------------------------------------------------- CSV watch

class CsvWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "export.csv")
        self.df = synthetic()
        self.df.to_csv(self.path, index=False)
        self.w = src.CsvWatchSource(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def settle(self, cursor):
        """Two polls: the first sees the change, the second reads it."""
        b = self.w.fetch_new(cursor)
        self.assertEqual(len(b), 0)
        return self.w.fetch_new(cursor)

    def test_first_read_then_unchanged(self):
        b = self.settle(None)
        self.assertEqual(len(b), 12)
        self.assertEqual(len(self.w.fetch_new(b.cursor)), 0)

    def test_added_and_edited_rows_only(self):
        b = self.settle(None)
        df = self.df.copy()
        df.loc[0, "rework_count"] = 1
        df = pd.concat([df, pd.DataFrame([dict(df.iloc[1], vehicle_id="V-NEW")])])
        df.to_csv(self.path, index=False)
        b2 = self.settle(b.cursor)
        self.assertEqual(sorted(b2.frame.vehicle_id), sorted([self.df.vehicle_id[0], "V-NEW"]))

    def test_half_written_file_not_parsed(self):
        b = self.settle(None)
        with open(self.path, "a") as f:
            f.write("V-PART,2026-02-01T05:00:00,LN1")  # no newline: mid-write
        self.assertEqual(len(self.w.fetch_new(b.cursor)), 0)
        self.assertEqual(len(self.w.fetch_new(b.cursor)), 0)  # settled, still no newline
        with open(self.path, "a") as f:
            f.write(",0,,301.0\n")
        b2 = self.settle(b.cursor)
        self.assertEqual(b2.frame.vehicle_id.tolist(), ["V-PART"])

    def test_missing_file(self):
        w = src.CsvWatchSource(os.path.join(self.tmp.name, "none.csv"))
        self.assertEqual(len(w.fetch_new(None)), 0)


# ------------------------------------------------------------- real data

@unittest.skipUnless(os.path.exists(CSV), "manufacturing.csv not present")
class RealFeedTest(unittest.TestCase):
    """The whole month through simulator -> feed -> MesSource."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.feed = os.path.join(cls.tmp.name, "mes.db")
        fs.run(CSV, cls.feed, speed="max", quiet=True)
        mes = src.MesSource(cls.feed, limit=10_000)
        cls.batches, cursor = [], 0
        while True:
            b = mes.fetch_new(cursor)
            if not len(b):
                cls.idle_clock = b.sim_time
                break
            cls.batches.append(b)
            cursor = b.cursor
        mes.close()
        cls.ids = pd.read_csv(CSV, usecols=["vehicle_id"])["vehicle_id"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_unit_exactly_once(self):
        got = pd.concat([b.frame["vehicle_id"] for b in self.batches])
        self.assertEqual(len(got), 200_000)
        self.assertTrue(got.is_unique)
        self.assertEqual(set(got), set(self.ids))

    def test_672_hours_in_order(self):
        conn = __import__("sqlite3").connect(self.feed)
        hours = [r[0] for r in conn.execute(
            "SELECT published_sim FROM production_units GROUP BY published_sim ORDER BY MIN(seq)")]
        mismatched = conn.execute(
            "SELECT COUNT(*) FROM production_units WHERE published_sim != build_date_utc").fetchone()[0]
        conn.close()
        expected = pd.date_range("2026-02-01T00:00", "2026-02-28T23:00", freq="h").strftime(src.TIME_FMT)
        self.assertEqual(hours, list(expected))
        self.assertEqual(mismatched, 0)
        self.assertEqual(self.idle_clock, "2026-02-28T23:00:00")

    def test_target_units_arrive_at_their_build_hour(self):
        conn = __import__("sqlite3").connect(self.feed)
        got = dict(conn.execute(
            "SELECT vehicle_id, published_sim FROM production_units "
            "WHERE vehicle_id IN ('GLDR-0421', 'GLDR-200000')").fetchall())
        conn.close()
        self.assertEqual(got, {"GLDR-0421": "2026-02-06T03:00:00",
                               "GLDR-200000": "2026-02-18T07:00:00"})

    def test_values_survive_the_feed(self):
        """GLDR-0421's row out of the feed equals its row in the CSV."""
        df = pd.read_csv(CSV)
        want = df[df.vehicle_id == "GLDR-0421"].iloc[0]
        got = pd.concat([b.frame for b in self.batches])
        got = got[got.vehicle_id == "GLDR-0421"].iloc[0]
        for c in df.columns:
            if pd.isna(want[c]):
                self.assertTrue(pd.isna(got[c]), c)
            else:
                self.assertEqual(got[c], want[c], c)


if __name__ == "__main__":
    unittest.main()
