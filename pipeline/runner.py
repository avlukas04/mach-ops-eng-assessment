"""
Runner — the gate, running unattended (Step 3 of the dashboard plan)
====================================================================
The loop that keeps judging production as it is built:

    fetch -> validate -> score -> store -> report        (and, when idle, heartbeat)

* Fetch:    source.fetch_new(cursor). The cursor is saved in the same
            transaction as its batch, so a crash never loses or repeats data.
* Validate: a unit with a valid ID always gets a verdict.
              - blank vehicle_id          -> rejected, logged (it can't be held)
              - non-numeric bench reading -> logged, scored as missing
                                             -> QUARANTINE INCOMPLETE (fail closed)
              - blank bench reading       -> scored as missing -> INCOMPLETE
              - required column missing   -> whole batch refused, cursor kept,
                                             status STALLED until the feed is fixed
              - unknown extra column      -> dropped, logged once
* Score:    spc_detector.classify with the baseline pinned at start: the same
            tested code and limits as the command-line tool.
* Store:    one transaction per batch (units, scores, history, holds, errors,
            run log, cursor).
* Report:   listeners get a summary per batch (Step 4 health and Step 5 live
            push plug in here); the command line prints each new hold with its
            lead time before final QC.

Baseline control (pin, warn, promote by restart)
------------------------------------------------
* Pinned: loaded once at start; its version and SHA-256 are recorded and used
  for every batch. Every score carries the version.
* Drift:  every heartbeat the file is re-hashed. If it differs, missing or
  unreadable, meta["baseline"] says so and one warning is logged. Scoring
  does not change.
* Promotion: restarting the runner adopts the file on disk; the store's
  baselines table records the change. Re-using a version with different
  content is refused.
* --rescore: explicit; re-scores units scored under another baseline, keeping
  the old scores in score_history.

Failures are retried with backoff (1, 2, 4 ... 30 s) on the same cursor: a
batch is never skipped, so no unit passes unscored.

Usage (from the repository root)
--------------------------------
  python3 tools/factory_simulator.py --speed 2      # the factory, in one terminal
  python3 -m pipeline.runner                         # the gate, in another
  python3 -m pipeline.runner --once                  # process what's there, then exit
  python3 -m pipeline.runner --csv export.csv        # watch a CSV export instead
  python3 -m pipeline.runner --rescore               # adopt baseline for stored units
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import spc_detector as spc  # noqa: E402

from pipeline.source import CsvWatchSource, MesSource  # noqa: E402
from pipeline.store import Store, columns_from_frame, utc_now  # noqa: E402

log = logging.getLogger("gate")

DEFAULT_FEED = os.path.join(ROOT, "outputs", "live", "mes.db")
DEFAULT_DB = os.path.join(ROOT, "outputs", "live", "production.db")
REQUIRED_COLS = list(spc.META_COLS) + list(spc.MEASURE_COLS)
MAX_BACKOFF_S = 30.0


class SchemaError(Exception):
    """The feed is missing columns the gate needs: refuse the batch."""


class BaselineError(Exception):
    pass


class FeedChangedError(Exception):
    """The feed was replaced: the saved cursor no longer refers to it."""


# ------------------------------------------------------------------ baseline

def read_baseline(path):
    """(baseline dict, sha256 of the exact bytes parsed)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise BaselineError(f"baseline not readable: {path}: {e}") from e
    try:
        base = json.loads(raw)
    except ValueError as e:
        raise BaselineError(f"baseline is not valid JSON: {path}: {e}") from e
    missing = [c for c in spc.MEASURE_COLS if c not in base.get("measures", {})]
    if missing or "version" not in base or "rules" not in base:
        raise BaselineError(f"baseline incomplete: {path} (missing {missing[:3] or 'version/rules'})")
    return base, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- validation

def validate(frame):
    """Return (clean frame, errors, rejected count). Raises SchemaError.

    errors: (vehicle_id, reason, raw row) for every logged problem.
    """
    missing = [c for c in REQUIRED_COLS if c not in frame.columns]
    if missing:
        raise SchemaError(f"feed is missing required column(s): {', '.join(missing)}")

    errors = []
    vid = frame["vehicle_id"]
    blank = vid.isna() | (vid.astype(str).str.strip() == "")
    for i in frame.index[blank]:
        errors.append(("", "blank vehicle_id: row rejected (cannot be held or traced)",
                       frame.loc[i].to_dict()))
    frame = frame[~blank].copy()

    for c in spc.MEASURE_COLS:
        col = frame[c]
        num = pd.to_numeric(col, errors="coerce")
        text = col.astype(str).str.strip()
        bad = col.notna() & num.isna() & (text != "")
        for i in frame.index[bad]:
            errors.append((frame.at[i, "vehicle_id"],
                           f"non-numeric {c}={col[i]!r}: scored as missing (QUARANTINE INCOMPLETE)",
                           frame.loc[i].to_dict()))
        frame[c] = num.astype(float)
    return frame.reset_index(drop=True), errors, int(blank.sum())


# -------------------------------------------------------------------- runner

class Runner:
    def __init__(self, source, db_path, baseline_path=spc.DEFAULT_BASELINE,
                 poll_s=1.0, heartbeat_s=10.0, listeners=(), clock=time.monotonic):
        self.source = source
        self.db_path = db_path
        self.baseline_path = baseline_path
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s
        self.listeners = list(listeners)
        self._clock = clock  # monotonic: batch durations survive wall-clock changes
        self.base, self.base_sha = read_baseline(baseline_path)  # pinned
        self.store = None
        self.cursor = None
        self.status = {"state": "STARTING", "since": utc_now(), "message": ""}
        self.baseline_status = self._baseline_status(self.base_sha, self.base["version"], "same")
        self._failures = 0
        self._last_heartbeat = 0.0
        self._last_logged_problem = None
        self._warned_extra = set()
        self._warned_disk_sha = None
        self._feed = None               # factory's last reported state
        self._feed_seen_clock = None    # feed clock at the last observed advance
        self._feed_advanced_at = None   # self._clock() reading at that advance
        if os.path.exists(db_path):
            self._open_store()

    # ---- store and baseline promotion

    def _open_store(self, columns=None):
        self.store = Store(self.db_path, columns=columns)
        info = self.store.record_baseline(self.base["version"], self.base_sha)  # may raise
        if info["promoted"] and info["previous"]:
            log.warning("baseline promoted: %s (%s) -> %s (%s)", info["previous"][0],
                        info["previous"][1][:12], self.base["version"], self.base_sha[:12])
        other = self.store.count_other_baselines(self.base["version"])
        if other:
            log.warning("%d stored units were scored under another baseline; "
                        "run with --rescore to re-score them under %s", other, self.base["version"])
        self.cursor = self.store.get_meta("cursor")
        self._last_heartbeat = 0.0  # next heartbeat writes at once: health sees the gate alive

    def _create_store(self, frame):
        cols = None
        if hasattr(self.source, "columns") and callable(self.source.columns):
            cols = self.source.columns()
        cols = dict(cols or columns_from_frame(frame))
        cols.update({c: "REAL" for c in spc.MEASURE_COLS})
        cols["vehicle_id"] = "TEXT"
        self._open_store(cols)

    def _baseline_status(self, disk_sha, disk_version, state):
        return {"in_use": {"version": self.base["version"], "sha256": self.base_sha},
                "on_disk": {"state": state, "version": disk_version, "sha256": disk_sha},
                "checked_at": utc_now()}

    def check_baseline_drift(self):
        """Compare the file on disk with the pinned baseline. Never changes scoring."""
        try:
            base, sha = read_baseline(self.baseline_path)
            state = "same" if sha == self.base_sha else "differs"
            st = self._baseline_status(sha, base["version"], state)
        except BaselineError as e:
            sha, state = None, "missing" if not os.path.exists(self.baseline_path) else "unreadable"
            st = self._baseline_status(None, None, state)
            st["on_disk"]["error"] = str(e)
        if state != "same" and sha != self._warned_disk_sha:
            log.warning("baseline on disk (%s) differs from baseline in use (%s %s); scoring "
                        "unchanged until the runner is restarted", state if sha is None else
                        f"{st['on_disk']['version']} {sha[:12]}", self.base["version"], self.base_sha[:12])
            self._warned_disk_sha = sha
        if state == "same":
            self._warned_disk_sha = None
        self.baseline_status = st
        return st

    # ---- status and heartbeat

    def _set_status(self, state, message=""):
        if (state, message) != (self.status["state"], self.status["message"]):
            self.status = {"state": state, "since": utc_now(), "message": message}
            if self.store:
                self.store.set_meta({"runner_status": self.status})

    def observe_feed(self):
        """Note the factory's clock on every poll. Silence is measured on the
        gate's monotonic clock from the last time that clock advanced, so it
        doesn't depend on the factory's and the gate's wall clocks agreeing."""
        if not hasattr(self.source, "feed_status"):
            return None
        st = self.source.feed_status()
        now = self._clock()
        if self._feed_advanced_at is None or (st and st["clock"] != self._feed_seen_clock):
            self._feed_seen_clock = st["clock"] if st else None
            self._feed_advanced_at = now
        self._feed = dict(st or {"clock": None, "cadence_s": None, "state": "absent",
                                 "published_at": None},
                          silent_s=round(now - self._feed_advanced_at, 3))
        return self._feed

    def heartbeat(self, feed_clock=None, force=False):
        now = time.monotonic()
        if not force and now - self._last_heartbeat < self.heartbeat_s:
            return
        self._last_heartbeat = now
        self.check_baseline_drift()
        if self.store:
            values = {"last_poll_at": utc_now(), "runner_status": self.status,
                      "baseline": self.baseline_status}
            if self._feed is not None:
                # silent_s is as of checked_at; health reads both together
                values["feed"] = dict(self._feed, checked_at=values["last_poll_at"])
                feed_clock = self._feed["clock"] or feed_clock
            if feed_clock:
                values["feed_clock"] = feed_clock
            self.store.set_meta(values)

    def _log_problem_once(self, message):
        """Log a repeating problem (e.g. a refused batch) once, not every retry."""
        if message != self._last_logged_problem:
            self._last_logged_problem = message
            log.error(message)
            if self.store:
                self.store.log_error(None, message)

    def _check_feed_identity(self):
        """A replaced feed restarts its sequence numbers, so the saved cursor
        would point past every new row and the gate would silently see
        nothing. Stall loudly instead."""
        if not hasattr(self.source, "feed_id"):
            return None
        current = self.source.feed_id()
        saved = self.store.get_meta("feed_id") if self.store else None
        if saved and current and current != saved:
            raise FeedChangedError(
                f"feed was replaced (feed_id {saved[:8]}… -> {current[:8]}…); the saved cursor "
                "does not apply to it. Restart the gate with --reset to start over on the new feed")
        return current

    # ---- one cycle

    def run_once(self):
        """Process one batch. Returns rows read (0 when idle)."""
        started, started_mono = utc_now(), self._clock()
        feed_id = self._check_feed_identity()
        self.observe_feed()
        batch = self.source.fetch_new(self.cursor)
        if not len(batch):
            self._set_status("IDLE")
            self.heartbeat(feed_clock=batch.sim_time)
            return 0

        frame, errors, rejected = validate(batch.frame)  # SchemaError -> caller stalls
        if self.store is None:
            self._create_store(frame)

        extra = [c for c in frame.columns if c not in self.store.unit_columns]
        if extra:
            new_extra = sorted(set(extra) - self._warned_extra)
            if new_extra:
                self._warned_extra.update(new_extra)
                errors.append((None, f"unknown column(s) dropped: {', '.join(new_extra)}", None))
            frame = frame.drop(columns=extra)

        if len(frame):
            results, _, _ = spc.classify(frame, self.base)
            by_id = dict(zip(results["vehicle_id"], results.to_dict("records")))
        else:
            by_id = {}
        d = self.store.diff(frame.to_dict("records"))
        todo = d["new"] + d["changed"]
        out = self.store.write_batch(
            todo, [by_id[u["vehicle_id"]] for u in todo],
            baseline_version=self.base["version"], sim_time=batch.sim_time,
            unchanged=d["unchanged"], rows_read=len(batch.frame), errors=errors,
            rejected=rejected, started_at=started, started_mono=started_mono, mono=self._clock,
            meta={"cursor": batch.cursor, "sim_time": batch.sim_time, "feed_id": feed_id,
                  "feed_clock": (self._feed or {}).get("clock") or batch.sim_time,
                  "last_batch_at": utc_now()})
        self.cursor = batch.cursor
        self._failures = 0
        self._last_logged_problem = None
        self._set_status("RUNNING")

        holds = []
        for vid in out["holds"]:
            u = frame.loc[frame["vehicle_id"] == vid].iloc[0]
            holds.append({"vehicle_id": vid, "held_at": batch.sim_time,
                          "final_qc_at": u.get("final_qc_timestamp_utc"),
                          "final_qc_pass": u.get("final_qc_pass"),
                          "reasons": by_id[vid]["reasons"]})
        summary = dict(out, sim_time=batch.sim_time, rows_read=len(batch.frame),
                       duration_ms=int(round((self._clock() - started_mono) * 1000)),
                       errors=len(errors), holds=holds)
        for fn in self.listeners:
            try:
                fn(summary)
            except Exception:  # a broken listener must never stop the gate
                log.exception("listener failed")
        self.heartbeat(feed_clock=batch.sim_time)
        return len(batch.frame)

    # ---- loops

    def run_forever(self, stop=None, until_idle=False):
        """Poll until `stop` is set (or, with until_idle, until nothing is new).
        Failures retry the same cursor with backoff; nothing is skipped."""
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                n = self.run_once()
                if n == 0:
                    if until_idle:
                        break
                    stop.wait(self.poll_s)
            except Exception as e:  # SchemaError, a locked database, a bug: all retried
                self._failures += 1
                delay = min(MAX_BACKOFF_S, 2 ** (self._failures - 1))
                msg = f"{type(e).__name__}: {e}"
                self._set_status("STALLED", msg)
                self._log_problem_once(msg)
                if not isinstance(e, SchemaError):
                    log.debug("batch failed", exc_info=True)
                if until_idle and self._failures >= 3:
                    raise
                stop.wait(delay)
        self.heartbeat(force=True)

    def rescore(self, chunk=5000):
        """Re-score stored units scored under another baseline (explicit only)."""
        if self.store is None:
            return {"rescored": 0, "holds": []}
        version, total, holds = self.base["version"], 0, []
        sim = self.store.get_meta("sim_time")
        while True:
            df = self.store.units_needing_rescore(version, chunk)
            if df.empty:
                break
            results, _, _ = spc.classify(df, self.base)
            holds += self.store.rescore_batch(results.to_dict("records"),
                                              baseline_version=version, sim_time=sim)
            total += len(df)
        return {"rescored": total, "holds": holds}

    def close(self):
        if self.store:
            self.store.close()
        if hasattr(self.source, "close"):
            self.source.close()


# ----------------------------------------------------------------------- CLI

def print_batch(summary):
    print(f"[gate] {summary['sim_time']}  read {summary['rows_read']:4d}  "
          f"new {summary['new']:4d}  changed {summary['changed']:3d}  "
          f"errors {summary['errors']:2d}  holds {summary['quarantines_raised']}", flush=True)
    for h in summary["holds"]:
        lead = ""
        if not pd.isna(h["final_qc_at"]):
            hrs = (pd.Timestamp(h["final_qc_at"]) - pd.Timestamp(h["held_at"])).total_seconds() / 3600
            lead = f" ({hrs:g} h before final QC at {h['final_qc_at']}, QC result: {h['final_qc_pass']})"
        print(f"  HOLD {h['vehicle_id']} at {h['held_at']}{lead}\n       {h['reasons']}", flush=True)


def remove_db(path):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the quality gate on a live feed.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--feed", default=DEFAULT_FEED, help="factory feed database (default)")
    src.add_argument("--csv", help="watch a CSV export instead of the feed")
    ap.add_argument("--db", default=DEFAULT_DB, help="the gate's own database")
    ap.add_argument("--baseline", default=spc.DEFAULT_BASELINE)
    ap.add_argument("--reset", action="store_true", help="delete the gate database first")
    ap.add_argument("--once", action="store_true", help="process everything available, then exit")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score stored units under the active baseline, then continue")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between polls when idle")
    ap.add_argument("--quiet", action="store_true", help="no per-batch lines (holds still print)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.reset:
        remove_db(args.db)
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    source = CsvWatchSource(args.csv) if args.csv else MesSource(args.feed)
    printer = print_batch if not args.quiet else \
        (lambda s: s["holds"] and print_batch(s))
    try:
        runner = Runner(source, args.db, args.baseline, poll_s=args.poll, listeners=[printer])
    except (BaselineError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    log.info("gate started: baseline %s (sha256 %s), db %s, source %s", runner.base["version"],
             runner.base_sha[:12], args.db, args.csv or args.feed)
    stop = threading.Event()
    try:
        if args.rescore:
            r = runner.rescore()
            log.info("rescored %d units under %s; %d new holds", r["rescored"],
                     runner.base["version"], len(r["holds"]))
        runner.run_forever(stop, until_idle=args.once)
    except KeyboardInterrupt:
        log.info("stopping; the next start resumes from the saved cursor")
    finally:
        if runner.store:
            c = runner.store.verdict_counts()
            log.info("totals: %d QUARANTINE / %d FLAG / %d PASS", c["QUARANTINE"], c["FLAG"], c["PASS"])
        runner.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
