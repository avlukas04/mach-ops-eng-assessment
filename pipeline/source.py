"""
Source — where units come from (Step 2 of the dashboard plan)
==============================================================
Every source has the same interface, so scoring, storage and the dashboard
never depend on where data comes from:

    batch = source.fetch_new(cursor)
    batch.frame     units in this batch (pandas DataFrame, source columns only,
                    at most one row per vehicle: the newest)
    batch.cursor    where to resume; the runner saves it in the same
                    transaction as the batch, so a restart loses or repeats nothing
    batch.sim_time  the batch's clock ("YYYY-MM-DDTHH:MM:SS", UTC)

Adapters
--------
MesSource       The gate's live connection. Pulls rows from a factory feed
                table by an increasing sequence number (seq > cursor), the
                standard way to read new records from an MES or warehouse.
                Here the feed is written by tools/factory_simulator.py; against
                a real MES only the connection and column names change.
CsvWatchSource  For plants that drop a file export. Polls the file and returns
                rows that are new or changed since the last read.
ReplaySource    Reads manufacturing.csv in build order, one build hour at a
                time. Used by the factory simulator to publish the feed, and
                by tests as a fast loader.
Pacer           Real-time pacing for the simulator: one build hour every
                `speed` seconds, or no waiting with speed "max".
"""

import datetime as dt
import logging
import os
import sqlite3
import time
from dataclasses import dataclass

import pandas as pd

log = logging.getLogger(__name__)

REQUIRED_COLS = ("vehicle_id", "build_date_utc")
TIME_FMT = "%Y-%m-%dT%H:%M:%S"
FEED_TABLE = "production_units"
FEED_COLS = ("seq", "published_sim")  # feed bookkeeping, not unit data


@dataclass
class Batch:
    frame: pd.DataFrame
    cursor: object
    sim_time: str | None

    def __len__(self):
        return len(self.frame)


def utc_now_str():
    return dt.datetime.now(dt.timezone.utc).strftime(TIME_FMT)


def check_columns(columns, where):
    missing = [c for c in REQUIRED_COLS if c not in columns]
    if missing:
        raise ValueError(f"{where}: missing required column(s) {missing}")


def newest_per_vehicle(frame):
    """One row per vehicle; the last (newest) wins, order otherwise kept.
    Rows with a blank vehicle_id are all kept, so the runner can log each
    one as an ingest error instead of silently merging them."""
    vid = frame["vehicle_id"]
    blank = vid.isna() | (vid.astype(str).str.strip() == "")
    dup = vid.duplicated(keep="last") & ~blank
    return frame[~dup].reset_index(drop=True)


# --------------------------------------------------------------------- pacing

def parse_speed(value):
    """'max' -> None (no waiting); otherwise seconds per build hour (> 0)."""
    if value is None or str(value).lower() == "max":
        return None
    s = float(value)
    if s <= 0:
        raise ValueError("speed must be > 0 seconds per build hour, or 'max'")
    return s


class Pacer:
    """Waits so tick k happens at start + k * speed seconds.

    Scheduling against the start time (not "sleep speed after each tick")
    means processing time is absorbed and the replay never drifts slower
    than the chosen speed. sleep and clock are injectable for tests.
    """

    def __init__(self, speed, sleep=time.sleep, clock=time.monotonic):
        self.speed = parse_speed(speed)
        self._sleep, self._clock = sleep, clock
        self._start, self._ticks = None, 0

    def wait(self):
        if self.speed is None:
            return 0.0
        now = self._clock()
        if self._start is None:
            self._start = now
        self._ticks += 1
        delay = max(0.0, self._start + self._ticks * self.speed - now)
        if delay:
            self._sleep(delay)
        return delay


# --------------------------------------------------------------------- replay

class ReplaySource:
    """manufacturing.csv in build order, one build hour per batch.

    The file is shuffled, so it is sorted once by build hour (then vehicle
    ID, for a stable order) and split into hour groups up front. The cursor
    is the index of the next hour. The data has hourly resolution, so every
    unit in an hour arrives together and sim_time is that hour.
    """

    def __init__(self, path=None, frame=None):
        df = pd.read_csv(path) if frame is None else frame.copy()
        check_columns(df.columns, path or "frame")
        df = df.sort_values(["build_date_utc", "vehicle_id"], kind="stable").reset_index(drop=True)
        self.frame = df
        groups = df.groupby("build_date_utc", sort=True).indices
        self.hours = list(groups)
        self._slices = [groups[h] for h in self.hours]
        self._starts = [0]
        for sl in self._slices:
            self._starts.append(self._starts[-1] + len(sl))

    def rows_before(self, hour):
        """Number of units built before hour index `hour` (sorted frame)."""
        return self._starts[min(hour, len(self.hours))]

    @property
    def columns(self):
        return list(self.frame.columns)

    def exhausted(self, cursor):
        return (cursor or 0) >= len(self.hours)

    def fetch_new(self, cursor=0):
        i = cursor or 0
        if i >= len(self.hours):
            last = self.hours[-1] if self.hours else None
            return Batch(self.frame.iloc[0:0], i, last)
        return Batch(self.frame.iloc[self._slices[i]].reset_index(drop=True), i + 1,
                     self.hours[i])


# ------------------------------------------------------------------------ MES

class MesSource:
    """Pulls new rows from the factory feed: SELECT ... WHERE seq > cursor.

    * Read-only connection: the gate can never alter the factory's data.
    * At most `limit` rows per batch, and never more than one published
      hour, so a gate that starts late catches up in bounded transactions
      and every unit keeps its true arrival time.
    * If the feed does not exist yet (gate started before the factory), an
      empty batch is returned and the next poll tries again.
    * sim_time is the newest published time in the batch; on an empty batch
      it is the feed's own clock, so the gate still knows the factory is
      alive (or not) when no units are being built.
    """

    def __init__(self, feed_path, limit=5000):
        self.feed_path = feed_path
        self.limit = limit
        self._conn = None

    def _connect(self):
        if self._conn is None:
            if not os.path.exists(self.feed_path):
                return None
            self._conn = sqlite3.connect(f"file:{self.feed_path}?mode=ro", uri=True,
                                         check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def columns(self):
        """{column: SQLite type} of unit data in the feed (for Store schema)."""
        conn = self._connect()
        if conn is None:
            return None
        info = conn.execute(f"PRAGMA table_info({FEED_TABLE})").fetchall()
        return {name: typ for _, name, typ, *_ in info if name not in FEED_COLS} or None

    def feed_id(self):
        """Unique ID of this feed; changes if the feed is deleted and recreated."""
        conn = self._connect()
        if conn is None:
            return None
        try:
            r = conn.execute("SELECT value FROM feed_meta WHERE key='feed_id'").fetchone()
        except sqlite3.OperationalError:
            return None
        return r[0] if r else None

    def feed_clock(self):
        conn = self._connect()
        if conn is None:
            return None
        try:
            r = conn.execute("SELECT value FROM feed_meta WHERE key='sim_time'").fetchone()
        except sqlite3.OperationalError:
            return None
        return r[0] if r else None

    def fetch_new(self, cursor=0):
        cursor = cursor or 0
        conn = self._connect()
        if conn is None:
            return Batch(pd.DataFrame(), cursor, None)
        try:
            # Never cross a published hour: every unit in the batch then truly
            # arrived at sim_time. Find the next hour, then read only its rows
            # (indexed on published_sim, seq).
            nxt = conn.execute(f"SELECT published_sim FROM {FEED_TABLE} WHERE seq > ? "
                               "ORDER BY seq LIMIT 1", (cursor,)).fetchone()
            if nxt is None:
                return Batch(pd.DataFrame(), cursor, self.feed_clock())
            df = pd.read_sql_query(
                f"SELECT * FROM {FEED_TABLE} WHERE published_sim = ? AND seq > ? "
                "ORDER BY seq LIMIT ?", conn, params=(nxt[0], cursor, self.limit))
        except (sqlite3.OperationalError, pd.errors.DatabaseError) as e:
            # Table not created yet, or feed briefly locked: try next poll
            log.warning("feed not readable yet: %s", e)
            return Batch(pd.DataFrame(), cursor, self.feed_clock())
        new_cursor = int(df["seq"].max())
        sim_time = str(nxt[0])
        frame = newest_per_vehicle(df.drop(columns=list(FEED_COLS)))
        return Batch(frame, new_cursor, sim_time)


# ------------------------------------------------------------------ CSV watch

class CsvWatchSource:
    """Polls a CSV export and returns rows that are new or changed.

    * Waits for the file to settle: it is read only when its size and
      modification time are the same on two polls in a row and it ends with
      a newline. A file caught mid-write is never parsed, because a
      truncated last line would parse as a unit with missing readings and be
      quarantined as INCOMPLETE.
    * Change detection uses pandas' row hashes kept in memory. After a
      restart memory is empty, so the first read returns every row and the
      store's fingerprints filter out what it already has.
    * The cursor is the (mtime_ns, size) last processed.
    * sim_time is the real current time: this is a live source.
    """

    def __init__(self, path):
        self.path = path
        self._pending = None
        self._hashes = {}

    def _stat(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return [st.st_mtime_ns, st.st_size]

    def _ends_with_newline(self):
        with open(self.path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"

    def fetch_new(self, cursor=None):
        empty = Batch(pd.DataFrame(), cursor, utc_now_str())
        stat = self._stat()
        if stat is None or stat == cursor or stat[1] == 0:
            return empty
        if stat != self._pending:  # changed since last poll: wait for it to settle
            self._pending = stat
            return empty
        if not self._ends_with_newline():
            return empty
        try:
            df = pd.read_csv(self.path)
        except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as e:
            log.warning("CSV not readable yet, will retry: %s", e)
            return empty
        check_columns(df.columns, self.path)
        df = newest_per_vehicle(df)
        hashes = pd.util.hash_pandas_object(df, index=False).to_numpy()
        ids = df["vehicle_id"].to_numpy()
        keep = [i for i, (vid, h) in enumerate(zip(ids, hashes)) if self._hashes.get(vid) != h]
        self._hashes.update(zip(ids, hashes))
        return Batch(df.iloc[keep].reset_index(drop=True), stat, utc_now_str())
