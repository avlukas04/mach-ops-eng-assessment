"""
Store — the gate's own record, in SQLite (Step 1 of the dashboard plan)
=======================================================================
manufacturing.csv plays the factory feed. This database is what the live
system knows and decides: the units received so far, each unit's verdict
and the baseline version that produced it, every earlier verdict, the
hold / retest / release events with who and why, and one row per ingest run.

Why this design
---------------
* WAL mode: the dashboard reads while the runner writes, without blocking.
* One transaction per batch: a crash or error never leaves half a batch.
* Row fingerprints: a unit that arrives again unchanged is ignored, so
  re-reading the source is harmless. A unit that arrives with different
  values (for example after rework) is updated and re-scored, so rework
  triggers a re-screen.
* Every score carries the baseline version, and every score ever given is
  kept in score_history, so any verdict can be reproduced and audited.
* The units table has one typed column per source column (about 1.4 KB a
  row) rather than a JSON blob (about 4.5 KB), so any column can be queried
  and indexed. Measured on the full 200,000-unit replay: 418 MB in total
  (units 278 MB, scores 51 MB, score_history 51 MB, indexes the rest).

Tables
------
  units          one row per vehicle, the facts as received + fingerprint
  scores         the current verdict per vehicle
  score_history  every verdict ever given
  events         gate events (QUARANTINE_RAISED, GATE_RETEST) and human sign-offs
  runs           one row per ingest cycle
  ingest_errors  rows the runner rejected, with the reason
  meta           small key/value state (source cursor, simulated clock)
  baselines      each baseline activation (version, file hash, the one replaced)

Holds
-----
A QUARANTINE opens a hold, and only a person closes it, with RELEASED. Release
needs a name, a note, and passing evidence as the latest retest result since
the hold opened: either RETEST_PASS (a person re-ran the bench test) or a
GATE_RETEST that scored PASS (the unit came back through the feed with new
bench readings). New readings never close a hold on their own.

  While a hold is open, new bench readings are scored and logged as
  GATE_RETEST (note "PASS", "FLAG: ...", or "QUARANTINE: ...").
  RETEST_FAIL_REPLACED means a servo was swapped. Nothing more can be
  recorded until the replacement's readings arrive and are scored.
  After a release, a QUARANTINE with changed readings opens a new hold.
  A release stays valid if the readings are unchanged (rework paperwork only).
"""

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import threading
import time

SCHEMA_VERSION = 1

VERDICTS = ("PASS", "FLAG", "QUARANTINE")
GATE_EVENT = "QUARANTINE_RAISED"
GATE_RETEST = "GATE_RETEST"
HUMAN_EVENTS = ("RETEST_PASS", "RETEST_FAIL_REPLACED", "RELEASED")
EVENT_TYPES = (GATE_EVENT, GATE_RETEST) + HUMAN_EVENTS

SERVOS = ["elevon_l", "elevon_r", "aileron_l", "aileron_r", "rudder"]
METRICS = ["sweep_current_ma", "peak_current_ma", "sweep_time_ms",
           "position_error_rms_deg", "backdrive_torque_nmm"]
READING_COLS = [f"{s}_servo_{m}" for s in SERVOS for m in METRICS]


class ActionRefused(ValueError):
    """A sign-off not allowed in the unit's current state (API: 409)."""


def hold_status(events, verdict):
    """The hold state of one unit from its events (oldest first) and its
    current verdict. Pure function: the store, health and the API all use it.

    evidence is the latest retest result since the hold opened:
      "pass"     RETEST_PASS, or a GATE_RETEST that scored PASS
      "fail"     a GATE_RETEST that scored FLAG or QUARANTINE
      "pending"  RETEST_FAIL_REPLACED, waiting for the replacement's readings
      None       nothing yet
    """
    hold, closed = None, None
    for e in events:
        t = e["event_type"]
        if t == GATE_EVENT and hold is None:
            hold = {"held_at": e["at_sim"], "raised_event": e["id"], "reasons": e["note"],
                    "evidence": None, "gate_retest": None, "last_action": None}
        elif hold is None:
            continue
        elif t == "RELEASED":
            closed = {"held_at": hold["held_at"], "reasons": hold["reasons"],
                      "released": {"actor": e["actor"], "note": e["note"],
                                   "at": e["at_sim"] or e["created_at"]}}
            hold = None
        elif t == GATE_RETEST:
            v = (e["note"] or "").split(":")[0].strip()
            hold["gate_retest"] = {"verdict": v, "note": e["note"], "at": e["at_sim"]}
            hold["evidence"] = "pass" if v == "PASS" else "fail"
        elif t == "RETEST_PASS":
            hold["evidence"] = "pass"
        elif t == "RETEST_FAIL_REPLACED":
            hold["evidence"] = "pending"
        if hold is not None and t in HUMAN_EVENTS:
            hold["last_action"] = {"type": t, "actor": e["actor"], "note": e["note"],
                                   "at": e["at_sim"] or e["created_at"]}
    if hold is not None:
        if hold["evidence"] == "pending":
            allowed = []
        else:
            allowed = ["RETEST_PASS", "RETEST_FAIL_REPLACED"]
            if hold["evidence"] == "pass":
                allowed.append("RELEASED")
        return dict(hold, open=True, awaiting_rescore=hold["evidence"] == "pending",
                    allowed=allowed)
    pending = bool(events) and events[-1]["event_type"] == "RETEST_FAIL_REPLACED"
    allowed = ["RETEST_PASS", "RETEST_FAIL_REPLACED"] if verdict == "FLAG" and not pending else []
    return {"open": False, "awaiting_rescore": pending, "allowed": allowed, "last_hold": closed}


def refusal(status, event_type, verdict):
    """Why event_type is not allowed, in words for the person at the bench."""
    if status["open"]:
        if status["awaiting_rescore"]:
            return "a replaced servo is waiting to be scored; nothing can be recorded until it is"
        return ("release needs a passing retest since the hold: record RETEST_PASS, "
                "or wait for new bench readings that score PASS")
    if status["awaiting_rescore"]:
        return "a replaced servo is waiting to be scored; nothing can be recorded until it is"
    if event_type == "RELEASED":
        return "the unit is not held"
    return f"retests are recorded for FLAG units or held units; this unit is {verdict}"

# Score columns copied from spc_detector.classify output
SCORE_COLS = (["verdict", "reasons", "worst_servo", "worst_d", "worst_metric",
               "worst_z", "saturated"]
              + [f"d_{s}" for s in SERVOS] + [f"flag_{s}" for s in SERVOS])

# Unit columns the dashboard groups or filters by
INDEXED_UNIT_COLS = ["build_date_utc", "final_qc_timestamp_utc", "factory_site",
                     "line_id", "build_shift"]

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
_TYPES = ("REAL", "INTEGER", "TEXT")


def utc_now():
    # Milliseconds so runs in the same second still sort; durations are NOT
    # derived from these (wall clocks can be adjusted), see runs.duration_ms
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _ident(name):
    """Column names go into DDL, so only plain snake_case is accepted."""
    if not _IDENT.match(name):
        raise ValueError(f"invalid column name: {name!r}")
    return f'"{name}"'


def _clean(v):
    """Database-ready scalar: NaN -> None, numpy scalars -> Python."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def fingerprint(row):
    """SHA-256 of a unit's values, independent of key order and int/float."""
    norm = {}
    for k, v in row.items():
        v = _clean(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = repr(float(v))
        norm[k] = v
    blob = json.dumps(norm, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def columns_from_frame(df):
    """{column: SQLite type} from a pandas frame's dtypes."""
    out = {}
    for c, t in df.dtypes.items():
        kind = getattr(t, "kind", "O")
        out[c] = "INTEGER" if kind in "iub" else "REAL" if kind == "f" else "TEXT"
    return out


class Store:
    """Read/write access to the live database. One writer (the runner);
    any number of readers via Store(path, readonly=True)."""

    def __init__(self, path, columns=None, readonly=False):
        self.path = path
        self.readonly = readonly
        self._lock = threading.Lock()
        if readonly:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                        check_same_thread=False)
        else:
            # isolation_level=None: transactions are opened explicitly below
            self.conn = sqlite3.connect(path, isolation_level=None,
                                        check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.row_factory = sqlite3.Row
        if not readonly:
            self._create_schema(columns)
            self.conn.execute("""CREATE TABLE IF NOT EXISTS baselines (
  id INTEGER PRIMARY KEY,
  activated_at TEXT NOT NULL,
  version TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  previous_version TEXT,
  previous_sha256 TEXT)""")
            self._migrate()
        self.unit_columns = self._load_unit_columns()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ schema

    def _create_schema(self, columns):
        c = self.conn
        has_units = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='units'").fetchone()
        if not has_units:
            if not columns or "vehicle_id" not in columns:
                raise ValueError("a new database needs the source columns, including vehicle_id")
            for t in columns.values():
                if t not in _TYPES:
                    raise ValueError(f"invalid column type: {t!r}")
            data_cols = ",\n  ".join(f"{_ident(k)} {t}" for k, t in columns.items()
                                     if k != "vehicle_id")
            score_cols = ",\n  ".join(self._score_col_ddl())
            c.executescript(f"""
BEGIN;
CREATE TABLE units (
  vehicle_id TEXT PRIMARY KEY,
  {data_cols},
  fingerprint TEXT NOT NULL,
  first_seen_sim TEXT,
  last_changed_sim TEXT,
  received_at TEXT NOT NULL
);
CREATE TABLE scores (
  vehicle_id TEXT PRIMARY KEY REFERENCES units(vehicle_id),
  {score_cols},
  baseline_version TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  scored_sim TEXT,
  scored_at TEXT NOT NULL
);
CREATE TABLE score_history (
  id INTEGER PRIMARY KEY,
  vehicle_id TEXT NOT NULL REFERENCES units(vehicle_id),
  {score_cols},
  baseline_version TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  scored_sim TEXT,
  scored_at TEXT NOT NULL
);
{self._events_ddl()};
CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  sim_time TEXT,
  rows_read INTEGER NOT NULL,
  new INTEGER NOT NULL,
  changed INTEGER NOT NULL,
  unchanged INTEGER NOT NULL,
  rejected INTEGER NOT NULL,
  quarantines_raised INTEGER NOT NULL,
  baseline_version TEXT,
  duration_ms INTEGER
);
CREATE TABLE ingest_errors (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id),
  vehicle_id TEXT,
  reason TEXT NOT NULL,
  raw TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX score_history_vid ON score_history(vehicle_id);
CREATE INDEX events_vid ON events(vehicle_id);
CREATE INDEX scores_verdict ON scores(verdict);
INSERT INTO meta VALUES ('schema_version', '{SCHEMA_VERSION}');
COMMIT;""")
            for col in INDEXED_UNIT_COLS:
                if col in columns:
                    c.execute(f"CREATE INDEX units_{col} ON units({_ident(col)})")

    def _migrate(self):
        """Bring a database created by an earlier version up to date.
        Added columns are NULL for old rows: unknown, not invented."""
        runs_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "duration_ms" not in runs_cols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN duration_ms INTEGER")
        # SQLite can't alter a CHECK: rebuild events to allow GATE_RETEST
        ddl = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
        if ddl and GATE_RETEST not in ddl[0]:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("ALTER TABLE events RENAME TO events_old")
                c.execute(self._events_ddl())
                c.execute("INSERT INTO events SELECT * FROM events_old")
                c.execute("DROP TABLE events_old")
                c.execute("CREATE INDEX IF NOT EXISTS events_vid ON events(vehicle_id)")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _events_ddl():
        types = ", ".join(f"'{e}'" for e in EVENT_TYPES)
        return f"""CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  vehicle_id TEXT NOT NULL REFERENCES units(vehicle_id),
  event_type TEXT NOT NULL CHECK (event_type IN ({types})),
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  note TEXT NOT NULL CHECK (length(trim(note)) > 0),
  at_sim TEXT,
  created_at TEXT NOT NULL
)"""

    @staticmethod
    def _score_col_ddl():
        out = []
        for col in SCORE_COLS:
            if col == "verdict":
                out.append(f"verdict TEXT NOT NULL CHECK (verdict IN {VERDICTS})")
            elif col in ("reasons", "worst_servo", "worst_metric"):
                out.append(f"{col} TEXT")
            elif col == "saturated" or col.startswith("flag_"):
                out.append(f"{col} INTEGER")
            else:
                out.append(f"{col} REAL")
        return out

    def _load_unit_columns(self):
        info = self.conn.execute("PRAGMA table_info(units)").fetchall()
        bookkeeping = {"fingerprint", "first_seen_sim", "last_changed_sim", "received_at"}
        return [r["name"] for r in info if r["name"] not in bookkeeping]

    # ------------------------------------------------------------------ writes

    def _lookup(self, select, ids):
        """{vehicle_id: value} for `select` ("SELECT vehicle_id, x FROM t"),
        restricted to ids, in chunks under SQLite's parameter limit."""
        out = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            q = f"{select} WHERE vehicle_id IN ({','.join('?' * len(chunk))})"
            out.update(self.conn.execute(q, chunk).fetchall())
        return out

    def diff(self, rows):
        """Split incoming rows into new, changed and unchanged by fingerprint.

        Returns {"new": [...], "changed": [...], "unchanged": int}. Each row
        gets a "_fingerprint" key. Duplicate vehicle_ids keep the last row.
        """
        latest = {}
        for r in rows:
            latest[r["vehicle_id"]] = r
        known = self._lookup("SELECT vehicle_id, fingerprint FROM units", list(latest))
        new, changed, unchanged = [], [], 0
        for vid, r in latest.items():
            fp = fingerprint({k: v for k, v in r.items() if not k.startswith("_")})
            r = dict(r, _fingerprint=fp)
            if vid not in known:
                new.append(r)
            elif known[vid] != fp:
                changed.append(r)
            else:
                unchanged += 1
        return {"new": new, "changed": changed, "unchanged": unchanged}

    def write_batch(self, units, scores, *, baseline_version, sim_time=None,
                    unchanged=0, rows_read=None, errors=(), rejected=None,
                    meta=None, started_at=None, started_mono=None, mono=time.monotonic):
        """Write one ingest cycle in a single transaction.

        units:  rows from diff() (new and changed), each with "_fingerprint"
        scores: one classify() result row (dict) per unit
        errors: (vehicle_id, reason, raw_row) for every logged ingest problem
        rejected: rows dropped entirely (default: len(errors)); a row can be
                  logged as an error and still be stored and scored
        meta:   key/value state to save with the batch (e.g. source cursor)
        started_mono, mono: start of the cycle on a monotonic clock, and that
                clock. duration_ms = mono() - started_mono, taken just before
                the run row is written, so it covers fetch through write and
                is immune to wall-clock adjustments.

        Raises ValueError, and writes nothing, if any step fails.
        """
        if self.readonly:
            raise RuntimeError("read-only store")
        unit_ids = [u["vehicle_id"] for u in units]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("duplicate vehicle_id in batch")
        score_by_id = {s["vehicle_id"]: s for s in scores}
        if set(score_by_id) != set(unit_ids):
            raise ValueError("scores must cover exactly the units in the batch")
        unknown = {k for u in units for k in u if not k.startswith("_")} - set(self.unit_columns)
        if unknown:
            raise ValueError(f"unknown unit columns: {sorted(unknown)}")

        now = utc_now()
        started_at = started_at or now
        data_cols = [c for c in self.unit_columns if c != "vehicle_id"]
        unit_sql = (
            f"INSERT INTO units (vehicle_id, {', '.join(_ident(c) for c in data_cols)}, "
            "fingerprint, first_seen_sim, last_changed_sim, received_at) "
            f"VALUES ({', '.join('?' * (len(data_cols) + 5))}) "
            "ON CONFLICT(vehicle_id) DO UPDATE SET "
            + ", ".join(f"{_ident(c)}=excluded.{_ident(c)}" for c in data_cols)
            + ", fingerprint=excluded.fingerprint, last_changed_sim=excluded.last_changed_sim, "
            "received_at=excluded.received_at")
        score_names = ["vehicle_id"] + SCORE_COLS + ["baseline_version", "fingerprint",
                                                     "scored_sim", "scored_at"]
        placeholders = ", ".join("?" * len(score_names))
        score_sql = f"INSERT OR REPLACE INTO scores ({', '.join(score_names)}) VALUES ({placeholders})"
        hist_sql = f"INSERT INTO score_history ({', '.join(score_names)}) VALUES ({placeholders})"

        # Build every parameter row first, then write each table with one
        # executemany (one SQL call per table instead of five per unit).
        unit_vals, score_vals, event_vals = [], [], []
        for u in units:
            vid = u["vehicle_id"]
            unit_vals.append([vid] + [_clean(u.get(k)) for k in data_cols]
                             + [u["_fingerprint"], sim_time, sim_time, now])
            s = score_by_id[vid]
            score_vals.append([vid] + [_clean(s.get(k)) for k in SCORE_COLS] + [
                baseline_version, u["_fingerprint"], sim_time, now])

        counts = {"new": 0, "changed": 0, "quarantines_raised": 0}
        with self._lock:
            c = self.conn
            try:
                c.execute("BEGIN IMMEDIATE")
                existing = set(self._lookup("SELECT vehicle_id, 1 FROM units", unit_ids))
                prev = self._lookup("SELECT vehicle_id, verdict FROM scores", unit_ids)
                counts["changed"] = len(existing)
                counts["new"] = len(units) - len(existing)
                changed_ids = [v for v in unit_ids if v in existing]
                moved = self._readings_changed(changed_ids, {u["vehicle_id"]: u for u in units})
                status = {v: hold_status(ev, prev.get(v))
                          for v, ev in self._events_for(changed_ids).items()}
                held = {v for v, st in status.items() if st["open"]}
                replaced = {v for v, st in status.items() if not st["open"] and st["awaiting_rescore"]}
                holds = []
                for vid in unit_ids:
                    s = score_by_id[vid]
                    verdict = s["verdict"]
                    if vid in held or (vid in replaced and verdict != "QUARANTINE"):
                        # New readings are the gate's retest (an open hold stays open)
                        if vid in moved:
                            note = verdict if verdict == "PASS" else f"{verdict}: {s.get('reasons') or verdict}"
                            event_vals.append((vid, GATE_RETEST, note, sim_time, now))
                    elif verdict == "QUARANTINE" and (prev.get(vid) != "QUARANTINE" or vid in moved):
                        # A new hold: newly QUARANTINE, or released and its readings changed
                        event_vals.append((vid, GATE_EVENT, s.get("reasons") or "QUARANTINE",
                                           sim_time, now))
                        holds.append(vid)
                counts["quarantines_raised"] = len(holds)
                c.executemany(unit_sql, unit_vals)
                c.executemany(score_sql, score_vals)
                c.executemany(hist_sql, score_vals)
                c.executemany(
                    "INSERT INTO events (vehicle_id, event_type, actor, note, at_sim, created_at) "
                    "VALUES (?, ?, 'gate', ?, ?, ?)", event_vals)

                n_err = len(errors) if rejected is None else rejected
                rows_default = len(units) + unchanged + n_err
                duration_ms = (None if started_mono is None
                               else int(round((mono() - started_mono) * 1000)))
                cur = c.execute(
                    "INSERT INTO runs (started_at, finished_at, sim_time, rows_read, new, changed, "
                    "unchanged, rejected, quarantines_raised, baseline_version, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (started_at, utc_now(), sim_time,
                     rows_read if rows_read is not None else rows_default,
                     counts["new"], counts["changed"], unchanged, n_err,
                     counts["quarantines_raised"], baseline_version, duration_ms))
                run_id = cur.lastrowid
                for vid, reason, raw in errors:
                    c.execute("INSERT INTO ingest_errors (run_id, vehicle_id, reason, raw, created_at) "
                              "VALUES (?, ?, ?, ?, ?)",
                              (run_id, vid, reason, json.dumps(raw, default=str), now))
                for k, v in (meta or {}).items():
                    c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, json.dumps(v)))
                c.execute("COMMIT")
            except BaseException as e:  # including Ctrl-C: never half a batch
                c.execute("ROLLBACK")
                if isinstance(e, sqlite3.Error):
                    raise ValueError(f"batch rejected, nothing written: {e}") from e
                raise
        return dict(counts, unchanged=unchanged, rejected=n_err, run_id=run_id, holds=holds)

    def rescore_batch(self, scores, *, baseline_version, sim_time=None):
        """Replace current scores (unit facts untouched) in one transaction.
        Old scores stay in score_history; a hold is raised only when a unit
        newly becomes QUARANTINE. Returns the vehicle_ids newly held."""
        if self.readonly:
            raise RuntimeError("read-only store")
        now = utc_now()
        ids = [s["vehicle_id"] for s in scores]
        names = ["vehicle_id"] + SCORE_COLS + ["baseline_version", "fingerprint",
                                               "scored_sim", "scored_at"]
        ph = ", ".join("?" * len(names))
        with self._lock:
            c = self.conn
            try:
                c.execute("BEGIN IMMEDIATE")
                fps = self._lookup("SELECT vehicle_id, fingerprint FROM units", ids)
                if set(fps) != set(ids):
                    raise ValueError("rescore: unknown vehicle_id")
                prev = self._lookup("SELECT vehicle_id, verdict FROM scores", ids)
                held = self._open_holds(ids, prev)
                vals = [[s["vehicle_id"]] + [_clean(s.get(k)) for k in SCORE_COLS]
                        + [baseline_version, fps[s["vehicle_id"]], sim_time, now] for s in scores]
                holds = [s["vehicle_id"] for s in scores
                         if s["verdict"] == "QUARANTINE" and prev.get(s["vehicle_id"]) != "QUARANTINE"
                         and s["vehicle_id"] not in held]
                c.executemany(f"INSERT OR REPLACE INTO scores ({', '.join(names)}) VALUES ({ph})", vals)
                c.executemany(f"INSERT INTO score_history ({', '.join(names)}) VALUES ({ph})", vals)
                c.executemany(
                    "INSERT INTO events (vehicle_id, event_type, actor, note, at_sim, created_at) "
                    "VALUES (?, ?, 'gate', ?, ?, ?)",
                    [(v, GATE_EVENT, "rescore under baseline " + baseline_version, sim_time, now)
                     for v in holds])
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return holds

    def _readings_changed(self, ids, incoming):
        """ids (already stored) whose bench readings differ from `incoming`.
        Called inside the batch transaction, before the units are updated."""
        cols = [c for c in READING_COLS if c in self.unit_columns]
        if not ids or not cols:
            return set()
        out = set()
        sel = ", ".join(_ident(c) for c in cols)
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in self.conn.execute(f"SELECT vehicle_id, {sel} FROM units WHERE vehicle_id IN "
                                       f"({','.join('?' * len(chunk))})", chunk):
                new = incoming[r[0]]
                if any(r[k + 1] != _clean(new.get(c)) for k, c in enumerate(cols)):
                    out.add(r[0])
        return out

    def _events_for(self, ids):
        """{vehicle_id: [event, ...]} (oldest first) for units with any event."""
        out = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in self.conn.execute(f"SELECT * FROM events WHERE vehicle_id IN "
                                       f"({','.join('?' * len(chunk))}) ORDER BY id", chunk):
                out.setdefault(r["vehicle_id"], []).append(dict(r))
        return out

    def _open_holds(self, ids, verdicts):
        """The ids among `ids` with an open hold."""
        return {v for v, ev in self._events_for(ids).items()
                if hold_status(ev, verdicts.get(v))["open"]}

    def units_needing_rescore(self, baseline_version, limit=5000):
        """Up to `limit` units (source columns) scored under another baseline."""
        import pandas as pd  # only needed here
        cols = ", ".join(f"u.{_ident(c)}" for c in self.unit_columns)
        return pd.read_sql_query(
            f"SELECT {cols} FROM units u JOIN scores s USING (vehicle_id) "
            "WHERE s.baseline_version != ? LIMIT ?", self.conn, params=(baseline_version, limit))

    def count_other_baselines(self, baseline_version):
        return self.conn.execute("SELECT COUNT(*) FROM scores WHERE baseline_version != ?",
                                 (baseline_version,)).fetchone()[0]

    def record_baseline(self, version, sha256):
        """Record the baseline this runner pinned at start (the promotion step).

        Raises ValueError if this version was recorded before with different
        content: one version must always mean one exact file.
        Returns {"promoted": bool, "previous": (version, sha256) or None}.
        """
        with self._lock:
            c = self.conn
            clash = c.execute("SELECT sha256 FROM baselines WHERE version=? AND sha256!=? LIMIT 1",
                              (version, sha256)).fetchone()
            if clash:
                raise ValueError(
                    f"baseline version {version!r} was used before with different content "
                    f"(sha256 {clash[0][:12]}…); give the new baseline a new version")
            last = c.execute("SELECT version, sha256 FROM baselines ORDER BY id DESC LIMIT 1").fetchone()
            prev = (last[0], last[1]) if last else None
            if prev and prev[1] == sha256:
                return {"promoted": False, "previous": prev}
            c.execute("INSERT INTO baselines (activated_at, version, sha256, previous_version, "
                      "previous_sha256) VALUES (?, ?, ?, ?, ?)",
                      (utc_now(), version, sha256, prev[0] if prev else None,
                       prev[1] if prev else None))
        return {"promoted": True, "previous": prev}

    def log_error(self, vehicle_id, reason, raw=None):
        """Log an ingest problem outside a batch (e.g. a refused batch)."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO ingest_errors (run_id, vehicle_id, reason, raw, created_at) "
                "VALUES (NULL, ?, ?, ?, ?)", (vehicle_id, reason, json.dumps(raw, default=str), utc_now()))

    def set_meta(self, values):
        """Save small state (heartbeat, status) in its own transaction."""
        with self._lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                              [(k, json.dumps(v)) for k, v in values.items()])
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def add_event(self, vehicle_id, event_type, actor, note, at_sim=None):
        """Record a human sign-off. Who and why are required, and the action
        must be allowed in the unit's current state (see Holds above):
        ActionRefused says why not. The check and the insert are one
        transaction, so a batch landing in between can't slip past the rules."""
        if self.readonly:
            raise RuntimeError("read-only store")
        if event_type not in HUMAN_EVENTS:
            raise ValueError(f"event_type must be one of {HUMAN_EVENTS}")
        if not (actor or "").strip() or not (note or "").strip():
            raise ValueError("actor and note are required")
        with self._lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT s.verdict FROM units u LEFT JOIN scores s USING (vehicle_id) "
                              "WHERE u.vehicle_id=?", (vehicle_id,)).fetchone()
                if r is None:
                    raise KeyError(vehicle_id)
                status = hold_status(self._events_for([vehicle_id]).get(vehicle_id, []), r[0])
                if event_type not in status["allowed"]:
                    raise ActionRefused(refusal(status, event_type, r[0]))
                cur = c.execute(
                    "INSERT INTO events (vehicle_id, event_type, actor, note, at_sim, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (vehicle_id, event_type, actor.strip(), note.strip(), at_sim, utc_now()))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return cur.lastrowid

    def hold_status(self, vehicle_id):
        """hold_status() for one unit; None if the unit is unknown."""
        r = self.conn.execute("SELECT s.verdict FROM units u LEFT JOIN scores s USING (vehicle_id) "
                              "WHERE u.vehicle_id=?", (vehicle_id,)).fetchone()
        if r is None:
            return None
        return hold_status(self.get_events(vehicle_id), r[0])

    def held_units(self):
        """{vehicle_id: (events, verdict)} for every unit ever held."""
        ids = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT vehicle_id FROM events WHERE event_type=?", (GATE_EVENT,))]
        ev = self._events_for(ids)
        verdicts = self._lookup("SELECT vehicle_id, verdict FROM scores", ids)
        return {v: (ev.get(v, []), verdicts.get(v)) for v in ids}

    # ------------------------------------------------------------------- reads

    def get_meta(self, key, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def get_unit(self, vehicle_id):
        r = self.conn.execute("SELECT * FROM units WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        return dict(r) if r else None

    def get_score(self, vehicle_id):
        r = self.conn.execute("SELECT * FROM scores WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        return dict(r) if r else None

    def get_score_history(self, vehicle_id):
        rows = self.conn.execute("SELECT * FROM score_history WHERE vehicle_id=? ORDER BY id",
                                 (vehicle_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_events(self, vehicle_id):
        rows = self.conn.execute("SELECT * FROM events WHERE vehicle_id=? ORDER BY id",
                                 (vehicle_id,)).fetchall()
        return [dict(r) for r in rows]

    def verdict_counts(self):
        rows = self.conn.execute("SELECT verdict, COUNT(*) n FROM scores GROUP BY verdict").fetchall()
        out = {v: 0 for v in VERDICTS}
        out.update({r["verdict"]: r["n"] for r in rows})
        return out

    def table_count(self, table):
        if table not in ("units", "scores", "score_history", "events", "runs", "ingest_errors",
                         "baselines"):
            raise ValueError(table)
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
