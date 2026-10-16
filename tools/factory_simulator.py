"""
Factory simulator — stands in for the MES (Step 2 of the dashboard plan)
========================================================================
Publishes manufacturing.csv into a factory feed database one build hour at a
time, on a simulated clock. The quality gate reads that feed through
pipeline.source.MesSource exactly as it would read a real MES table: by an
increasing sequence number. This is the only simulated part of the system;
the factory and the gate are separate processes connected only by the feed.

Feed (outputs/live/mes.db)
--------------------------
  production_units  append-only: seq (increasing), published_sim (clock time
                    the row was published), then every manufacturing.csv column.
                    A changed unit is published again as a new row (newest wins).
  feed_meta         feed_id (unique per feed, so a gate notices a replaced
                    feed), next_hour (resume point), sim_time (factory clock)

Test injections (off by default; clearly marked so they can't be mistaken for
real data):
  --rework N    each hour, republish N already-published units with
                rework_count + 1 and rework code "SIM" appended. Bench readings
                are not changed, so verdicts should not change.
  --bad-rows N  each hour, publish N malformed rows: alternately an empty
                vehicle_id, or vehicle_id "SIMBAD-<n>" with a bench reading of
                "ERR". The gate must log them to ingest_errors, not crash.

Usage (from the repository root)
--------------------------------
  python3 tools/factory_simulator.py --speed 2          # 1 build hour / 2 s (~22 min)
  python3 tools/factory_simulator.py --speed max        # whole month at once
  python3 tools/factory_simulator.py --reset --speed 2  # start the month over

Stopping and starting again resumes from the next unpublished hour.
"""

import argparse
import os
import uuid
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from pipeline.source import FEED_TABLE, Pacer, ReplaySource  # noqa: E402
from pipeline.store import _clean, _ident, columns_from_frame  # noqa: E402

DEFAULT_INPUT = os.path.join(PROJECT_ROOT, "manufacturing.csv")
DEFAULT_FEED = os.path.join(PROJECT_ROOT, "outputs", "live", "mes.db")
BAD_READING_COL = "aileron_l_servo_sweep_current_ma"


def remove_db(path):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


class Feed:
    """Write side of the factory feed."""

    def __init__(self, path, columns):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.columns = list(columns)
        cols = ",\n  ".join(f"{_ident(c)} {t}" for c, t in columns.items())
        # AUTOINCREMENT: seq is never reused, so the gate's cursor stays valid
        self.conn.executescript(f"""
CREATE TABLE IF NOT EXISTS {FEED_TABLE} (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  published_sim TEXT NOT NULL,
  {cols}
);
CREATE INDEX IF NOT EXISTS {FEED_TABLE}_published ON {FEED_TABLE}(published_sim, seq);
CREATE TABLE IF NOT EXISTS feed_meta (key TEXT PRIMARY KEY, value TEXT);""")
        self.conn.execute("INSERT OR IGNORE INTO feed_meta VALUES ('feed_id', ?)",
                          (uuid.uuid4().hex,))
        names = ", ".join(_ident(c) for c in self.columns)
        self._insert = (f"INSERT INTO {FEED_TABLE} (published_sim, {names}) "
                        f"VALUES (?, {', '.join('?' * len(self.columns))})")

    def meta(self, key, default=None):
        r = self.conn.execute("SELECT value FROM feed_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def publish(self, frame, sim_time, next_hour):
        """One transaction per hour: the gate never sees half an hour."""
        rows = [[sim_time] + [_clean(v) for v in rec]
                for rec in frame[self.columns].itertuples(index=False, name=None)]
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany(self._insert, rows)
            c.executemany("INSERT OR REPLACE INTO feed_meta VALUES (?, ?)",
                          [("next_hour", str(next_hour)), ("sim_time", sim_time)])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def close(self):
        self.conn.close()


def rework_rows(replay, published_upto, n, rng):
    """n already-published units with a simulated rework appended."""
    pool = replay.frame.iloc[:published_upto]
    if n <= 0 or pool.empty:
        return pool.iloc[0:0]
    rows = pool.iloc[rng.choice(len(pool), size=min(n, len(pool)), replace=False)].copy()
    rows["rework_count"] = rows["rework_count"].fillna(0) + 1
    codes = rows["rework_codes"].fillna("").astype(str)
    rows["rework_codes"] = np.where(codes == "", "SIM", codes + ";SIM")
    return rows


def bad_rows(template, n, start):
    """n malformed rows built from a real row, alternating the two faults."""
    if n <= 0:
        return template.iloc[0:0]
    rows = template.iloc[[0] * n].copy().astype(object)
    for k in range(n):
        if k % 2 == 0:
            rows.iloc[k, rows.columns.get_loc("vehicle_id")] = ""
        else:
            rows.iloc[k, rows.columns.get_loc("vehicle_id")] = f"SIMBAD-{start + k}"
            rows.iloc[k, rows.columns.get_loc(BAD_READING_COL)] = "ERR"
    return rows


def run(input_path, feed_path, speed, reset=False, rework=0, bad=0, seed=0,
        max_hours=None, sleep=time.sleep, quiet=False):
    if reset:
        remove_db(feed_path)
    replay = ReplaySource(input_path)
    feed = Feed(feed_path, columns_from_frame(replay.frame))
    pacer = Pacer(speed, sleep=sleep)
    rng = np.random.default_rng(seed)
    hour = int(feed.meta("next_hour", 0))
    published = 0
    try:
        while not replay.exhausted(hour) and (max_hours is None or published < max_hours):
            batch = replay.fetch_new(hour)
            parts = [batch.frame,
                     rework_rows(replay, replay.rows_before(hour), rework, rng),
                     bad_rows(batch.frame, bad, hour * max(bad, 1))]
            frame = pd.concat([p for p in parts if len(p)], ignore_index=True)
            feed.publish(frame, batch.sim_time, batch.cursor)
            hour, published = batch.cursor, published + 1
            if not quiet:
                print(f"[factory] {batch.sim_time}  published {len(frame):4d} rows "
                      f"(hour {hour}/{len(replay.hours)})", flush=True)
            if not replay.exhausted(hour):
                pacer.wait()
    finally:
        feed.close()
    return hour


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--feed", default=DEFAULT_FEED)
    ap.add_argument("--speed", default="2", help="seconds per build hour, or 'max' (default 2)")
    ap.add_argument("--reset", action="store_true", help="delete the feed and start the month over")
    ap.add_argument("--rework", type=int, default=0, help="test: republish N reworked units per hour")
    ap.add_argument("--bad-rows", type=int, default=0, help="test: publish N malformed rows per hour")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    if not os.path.exists(args.input):
        print(f"input not found: {args.input}", file=sys.stderr)
        return 1
    try:
        run(args.input, args.feed, args.speed, args.reset, args.rework, args.bad_rows, args.seed)
    except KeyboardInterrupt:
        print("\n[factory] stopped; run again to resume", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
