"""
Health — is production, the quality system and the gate itself healthy?
=======================================================================
(Step 4 of the dashboard plan.) Reads the gate's database, read-only, and
reports five layers, each green / amber / red, plus one alert list:

  1. product     QUARANTINE / FLAG / PASS, the hold queue with each unit's
                 lead time before final QC, saturated units
  2. stability   daily FLAG-rate p-charts per plant, line, shift and QC
                 station (vehicle verdicts) and per servo lot, vendor and
                 model (that servo's own flag)
  3. quality     final-QC pass share beside the gate's verdicts; open
                 warnings by type, line and QC station
  4. throughput  units per build hour by line, shift and plant; rework
  5. gate        runner alive, feed silent, gate lag, batch latency
                 (runs.duration_ms), rejected rows, baseline drift, and the
                 correlation assumption behind D

Severities: ALARM (contain now: a held unit, a chart limit breached, the
pipeline broken, D's assumption broken), WATCH (an early trend or a
slowdown), INFO (the legacy QC comparison). A layer is red with any ALARM,
amber with any WATCH, otherwise green.

Why the p-charts use exact binomial limits
------------------------------------------
Servo flag rates are about 0.65%, roughly 1.6 flags per lot per day. At
counts that small the usual normal approximation (p ± 3 sigma) is wrong
and most days sit below the average because the distribution is skewed.
On the real month (154 groups x 28 days, no real problem) the textbook
rules raised 45 alarms; exact binomial limits at the same 0.135% tail,
with the run rule applied above the centre line only, raise 5. A rising
rate is what matters; an unusually clean group is reported as a WATCH
("check the gate"), never as an ALARM.

Why the counts are kept in memory
---------------------------------
One GROUP BY over 200,000 units takes about 570 ms in SQLite; about 15 of
them per batch would take longer than the 2 s demo cadence. So the counts
are loaded once and then updated from score_history.id (which only
increases): a re-scored unit's old contribution is subtracted and its new
one added, so rework is never counted twice. A test checks that the
running counts equal a full recount.

Limits come from a frozen health baseline (tools/health_baseline.json),
pinned at start with its SHA-256 like the scoring baseline, and kept in a
separate file so tuning an alarm never changes the scoring version.

Usage (from the repository root)
--------------------------------
  python3 -m pipeline.health                       # one summary of the live db
  python3 -m pipeline.health --watch 5             # refresh every 5 s
  python3 -m pipeline.health --json                # the full snapshot
  python3 -m pipeline.health baseline              # rebuild the health baseline
"""

import argparse
import datetime as dt
import functools
import hashlib
import json
import math
import os
import re
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import spc_detector as spc  # noqa: E402

from pipeline.store import SERVOS, Store  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "outputs", "live", "production.db")
DEFAULT_HEALTH_BASELINE = os.path.join(ROOT, "tools", "health_baseline.json")

SEVERITIES = ("ALARM", "WATCH", "INFO")
LAYERS = ("product", "stability", "quality", "throughput", "gate")
STATUS = {"ALARM": "red", "WATCH": "amber"}

DEFAULT_RULES = {
    "alpha": 0.00135,               # one-sided tail of a 3-sigma limit
    "run_length": 8,                # days in a row above the centre line
    "chart_active_days": 2,         # a chart signal drives status while its day is this recent
    "heartbeat_stale_s": 30,        # 3x the runner's 10 s heartbeat
    "feed_stale_cadences": 3,       # feed silent for more than 3 build-hour intervals
    "lag_watch_hours": 3,           # gate more than 3 build hours behind the feed
    "latency_watch_share": 0.5,     # p95 batch time over half the interval
    "latency_window_runs": 24,
    "corr_window": 20000,
    "corr_min_units": 2000,
    "corr_alarm": 0.1,
    "throughput_watch_share": 0.5,  # a line building under half its normal hourly units
}

# Group dimensions for the p-charts. Line IDs repeat across plants (LN1 is
# at both), so a line is identified by plant and line together.
VEHICLE_DIMS = {"plant": "factory_site", "line": "line_key", "shift": "build_shift",
                "qc_station": "qc_station_id"}
SERVO_DIMS = ("lot_id", "vendor", "model")

WARN_TYPES = ("qc_exception", "imu_cal_warn", "mag_cal_warn", "strain_relief_fail")

UNIT_COLS = (["vehicle_id", "build_date_utc", "factory_site", "line_id", "build_shift",
              "qc_station_id", "final_qc_pass", "final_qc_timestamp_utc", "rework_count",
              "rework_codes", "qc_exception_count", "imu_cal_status", "mag_cal_status",
              "harness_strain_relief_ok"]
             + [f"{s}_servo_{d}" for s in SERVOS for d in SERVO_DIMS]
             + list(spc.MEASURE_COLS))
SCORE_READ = ["verdict", "saturated"] + [f"flag_{s}" for s in SERVOS]

UNKNOWN = "(blank)"


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    """ISO timestamp (with or without zone) -> aware UTC datetime, or None."""
    if not value:
        return None
    t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def hours_between(a, b):
    ta, tb = parse_time(a), parse_time(b)
    if ta is None or tb is None:
        return None
    return (tb - ta).total_seconds() / 3600


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --------------------------------------------------------------- statistics

@functools.lru_cache(maxsize=1 << 16)
def binom_limits(n, p, alpha=DEFAULT_RULES["alpha"]):
    """Exact binomial control limits as counts, for n units at rate p.

    Returns (lower, upper): a count k >= upper has P(X >= k) <= alpha, and a
    count k <= lower has P(X <= k) <= alpha. lower is -1 when no count is
    that unlikely on the low side (common for small n x p).
    """
    n = int(n)
    if n <= 0:
        return -1, 1
    if p <= 0:
        return -1, 1
    if p >= 1:
        return n - 1, n + 1
    lp, lq = math.log(p), math.log1p(-p)
    logpmf = n * lq
    cdf, lower = 0.0, -1
    for k in range(n + 1):
        pmf = math.exp(logpmf)
        if cdf + pmf <= alpha:
            lower = k
        if 1.0 - cdf <= alpha:      # P(X >= k) = 1 - cdf(k - 1)
            return lower, k
        cdf += pmf
        if k < n:
            logpmf += math.log(n - k) - math.log(k + 1) + lp - lq
    return lower, n + 1


@functools.lru_cache(maxsize=1 << 16)
def _point(n, k, center, alpha):
    """(p, ucl, lcl, limit signal) for one chart point; closed days repeat."""
    lower, upper = binom_limits(n, center, alpha)
    p = k / n if n else 0.0
    signal = "above" if k >= upper else "below" if lower >= 0 and k <= lower else None
    return (round(p, 6), round(upper / n, 6) if n else None,
            round(lower / n, 6) if n and lower >= 0 else None, signal)


def chart_points(series, center, rules, active_from):
    """series: [(day, n, k)] sorted by day. Returns the points with limits
    and signals: 'above' (ALARM), 'below' (WATCH), 'run' (WATCH)."""
    points, run = [], 0
    center = round(center, 12)
    for day, n, k in series:
        p, ucl, lcl, signal = _point(int(n), int(k), center, rules["alpha"])
        run = run + 1 if k / n > center else 0
        if signal is None and run >= rules["run_length"]:
            signal = "run"
        points.append({"day": day, "n": int(n), "k": int(k), "p": p, "ucl": ucl, "lcl": lcl,
                       "signal": signal, "active": bool(signal) and day >= active_from})
    return points


# --------------------------------------------------------------- tallies

def prepare(df):
    """Raw unit + score rows -> one row per vehicle with the derived fields
    every tally needs. Columns missing from the database count as blank."""
    f = pd.DataFrame(index=pd.Index(df["vehicle_id"].to_numpy(), name="vehicle_id"))
    get = lambda c: df[c].to_numpy() if c in df.columns else np.full(len(df), None, dtype=object)  # noqa: E731
    def text(c):
        cat = pd.Categorical(get(c))
        if cat.isna().any():  # cheap on a categorical: only the codes are checked
            cat = cat.add_categories([UNKNOWN]).fillna(UNKNOWN)
        return pd.Series(cat, index=f.index)

    build = pd.Series(get("build_date_utc"), index=f.index, dtype=object).fillna("").astype(str)
    f["day"] = build.str[:10].replace("", UNKNOWN).astype("category")
    f["hour"] = build.str[:19].replace("", UNKNOWN).astype("category")
    f["hour_n"] = pd.to_datetime(build.str[:19], errors="coerce").to_numpy().astype("datetime64[h]").astype(np.int64)
    f["factory_site"] = text("factory_site")
    f["line_key"] = (f["factory_site"].astype(str) + "/" + text("line_id").astype(str)).astype("category")
    f["build_shift"] = text("build_shift")
    f["qc_station_id"] = text("qc_station_id")
    f["verdict"] = text("verdict")
    f["nonpass"] = (f["verdict"] != "PASS").astype(np.int64)
    f["saturated"] = pd.to_numeric(pd.Series(get("saturated"), index=f.index), errors="coerce").fillna(0).astype(np.int64)
    qc = pd.to_numeric(pd.Series(get("final_qc_pass"), index=f.index), errors="coerce")
    f["qc_pass"] = qc.map(lambda v: UNKNOWN if pd.isna(v) else str(int(v)))
    num = lambda c: pd.to_numeric(pd.Series(get(c), index=f.index), errors="coerce")  # noqa: E731
    f["qc_exception"] = (num("qc_exception_count") > 0).astype(np.int64)
    for c, name in (("imu_cal_status", "imu_cal_warn"), ("mag_cal_status", "mag_cal_warn")):
        s = pd.Series(get(c), index=f.index, dtype=object)
        f[name] = (s.notna() & (s.astype(str) != "PASS")).astype(np.int64)
    f["strain_relief_fail"] = (num("harness_strain_relief_ok") == 0).astype(np.int64)
    f["warn_any"] = f[list(WARN_TYPES)].max(axis=1)
    f["reworked"] = (num("rework_count").fillna(0) > 0).astype(np.int64)
    f["rework_codes"] = pd.Series(get("rework_codes"), index=f.index, dtype=object).fillna("").astype(str)
    for s in SERVOS:
        f[f"flag_{s}"] = pd.to_numeric(pd.Series(get(f"flag_{s}"), index=f.index), errors="coerce").fillna(0).astype(np.int64)
        for d in SERVO_DIMS:
            f[f"{s}_{d}"] = text(f"{s}_servo_{d}")
    for c in spc.MEASURE_COLS:
        v = pd.to_numeric(pd.Series(get(c), index=f.index), errors="coerce").astype(np.float32)
        f[c] = v
    return f


def tally(f, ceilings):
    """Every count the health layers need, as {prefix: {key: array([n, k])}}.
    Used for the first load and, with subtraction, for each change. Keys are
    grouped by prefix so each layer reads only its own counts."""
    out = {}
    if f.empty:
        return out

    def put(prefix, cols, k=None, frame=None):
        g = (f if frame is None else frame)
        if g.empty:
            return
        grouped = g.groupby(cols, sort=False, observed=True)
        n = grouped.size()
        n = n[n > 0]
        kk = grouped[k].sum() if k else n * 0
        for key, a, b in zip(n.index, n.to_numpy(), kk.reindex(n.index).to_numpy()):
            key = key if isinstance(key, tuple) else (key,)
            # Group keys as plain strings so counts from different batches line up
            out.setdefault(prefix, {})[tuple(str(x) for x in key)] = np.array([a, b], dtype=np.int64)

    f = f.assign(_all="fleet")
    put("p:fleet", ["_all", "day"], "nonpass")
    for dim, c in VEHICLE_DIMS.items():
        put(f"p:{dim}", [c, "day"], "nonpass")
    for s in SERVOS:
        for d in SERVO_DIMS:
            put(f"p:{s}.{d}", [f"{s}_{d}", "day"], f"flag_{s}")
    put("hour", ["hour"], "nonpass")
    put("units", ["line_key", "build_shift", "hour"])
    put("qc", ["verdict", "qc_pass"])
    put("saturated", ["_all"], "saturated")
    for w in WARN_TYPES + ("warn_any",):
        put(f"warn:{w}", ["line_key", "qc_station_id"], frame=f[f[w] == 1])
    put("rework", ["line_key"], "reworked")
    codes = f.loc[f["rework_codes"] != "", ["line_key", "rework_codes"]]
    if not codes.empty:
        codes = codes.assign(code=codes["rework_codes"].str.split(r"[|;]")).explode("code")
        codes["code"] = codes["code"].str.strip()
        put("rework_code", ["code", "line_key"], frame=codes[codes["code"] != ""])
    for c in spc.MEASURE_COLS:
        v = f[c].to_numpy(dtype=np.float64)
        ceil = ceilings.get(c)
        at = int((v >= ceil).sum()) if ceil is not None else 0
        out.setdefault("ceiling", {})[(c,)] = np.array([int((~np.isnan(v)).sum()), at], dtype=np.int64)
    return out


def merge(counts, delta, sign):
    for prefix, part in delta.items():
        dest = counts.setdefault(prefix, {})
        for key, v in part.items():
            cur = dest.get(key)
            cur = v * sign if cur is None else cur + v * sign
            if cur[0] == 0 and cur[1] == 0:
                dest.pop(key, None)
            else:
                dest[key] = cur


# --------------------------------------------------------------- baseline

def build_health_baseline(input_path=spc.DEFAULT_INPUT, scoring_baseline=spc.DEFAULT_BASELINE,
                          version=None):
    """Expected rates from a reference month, scored with the pinned scoring
    baseline. Rebuild only after review, like the scoring baseline."""
    base = spc.load_baseline(scoring_baseline)
    df = pd.read_csv(input_path, low_memory=False)
    res, _, _ = spc.classify(df, base)
    nonpass = res["verdict"].to_numpy() != "PASS"
    line = df["factory_site"].astype(str) + "/" + df["line_id"].astype(str)
    per_hour = df.assign(line_key=line).groupby(["line_key", "build_date_utc"]).size()
    return {
        "version": version or dt.date.today().isoformat(),
        "description": "Frozen expected rates for health alarms. Review before changing.",
        "reference": {"source_file": os.path.basename(input_path),
                      "source_sha256": spc.file_sha256(input_path),
                      "units": int(len(df)), "build_hours": int(df["build_date_utc"].nunique()),
                      "scoring_baseline_version": base["version"]},
        "rates": {"vehicle_flag": round(float(nonpass.mean()), 6),
                  "servo_flag": {s: round(float(res[f"flag_{s}"].mean()), 6) for s in SERVOS}},
        "throughput": {"line_units_per_hour_median":
                       {k: float(v) for k, v in per_hour.groupby(level=0).median().items()}},
        "rules": dict(DEFAULT_RULES),
    }


def load_health_baseline(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    hb = json.loads(raw)
    for key in ("version", "rates", "throughput", "rules"):
        if key not in hb:
            raise ValueError(f"health baseline incomplete: {path} (missing {key})")
    hb["rules"] = dict(DEFAULT_RULES, **hb["rules"])
    return hb, hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------- health

class Health:
    """In-memory health state over the gate's database (read-only)."""

    def __init__(self, db_path=DEFAULT_DB, baseline_path=DEFAULT_HEALTH_BASELINE,
                 scoring_baseline=spc.DEFAULT_BASELINE):
        self.db_path = db_path
        self.baseline_path = baseline_path
        self.hb, self.hb_sha = load_health_baseline(baseline_path)  # pinned
        self.rules = self.hb["rules"]
        scoring = spc.load_baseline(scoring_baseline)
        self.scoring_version = scoring["version"]
        self.ceilings = {c: m["ceiling"] for c, m in scoring["measures"].items()
                         if m.get("ceiling") is not None}
        self.store = None
        # Units in memory as append-only parts: each refresh appends one part
        # and marks the superseded rows dead, instead of copying 200,000 rows
        self._parts, self._alive, self._where = [], [], {}
        self.counts = {}
        self.cursor = 0
        self._seen = {}
        self.timings = {}

    def close(self):
        if self.store:
            self.store.close()
            self.store = None

    # ---- loading

    def _open(self):
        if self.store is None and os.path.exists(self.db_path):
            self.store = Store(self.db_path, readonly=True)
        return self.store

    def _read(self, ids=None):
        """Units and their current scores. The two tables are read separately
        and joined in memory: on 200,000 units a SQL join takes 4.9 s, the
        two plain reads about 2.5 s."""
        cols = [c for c in UNIT_COLS if c in self.store.unit_columns]
        tables = (("units", cols), ("scores", ["vehicle_id"] + SCORE_READ))
        frames = []
        cur = self.store.conn.cursor()
        cur.row_factory = None  # plain tuples: sqlite3.Row objects cost ~2x on 200k rows
        for table, names in tables:
            q = f"SELECT {', '.join(chr(34) + c + chr(34) for c in names)} FROM {table}"
            if ids is None:
                rows = cur.execute(q).fetchall()
            else:
                rows = []
                for i in range(0, len(ids), 900):
                    chunk = ids[i:i + 900]
                    rows += cur.execute(
                        f"{q} WHERE vehicle_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            frames.append(pd.DataFrame.from_records(rows, columns=names)
                          .set_index("vehicle_id"))
        units, scores = frames
        return units.join(scores, how="inner").reset_index()

    def refresh(self):
        """Bring the counts up to date. Returns the number of vehicles read."""
        if self._open() is None:
            return 0
        t0 = time.perf_counter()
        conn = self.store.conn
        conn.execute("BEGIN")  # one consistent snapshot across the reads (WAL)
        try:
            top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM score_history").fetchone()[0]
            if top == self.cursor:
                return 0
            if not self._parts:
                raw = self._read()
                full = True
            else:
                ids = [r[0] for r in conn.execute(
                    "SELECT DISTINCT vehicle_id FROM score_history WHERE id > ? AND id <= ?",
                    (self.cursor, top))]
                raw = self._read(ids)
                full = False
        finally:
            conn.execute("COMMIT")
        new = prepare(raw)
        if full:
            self.counts = tally(new, self.ceilings)
        else:
            old = self._take_old(new.index)
            if old is not None:
                merge(self.counts, tally(old, self.ceilings), -1)
            merge(self.counts, tally(new, self.ceilings), +1)
        self._append(new)
        if len(self._parts) > self.COMPACT_PARTS:
            self._compact()
        self.cursor = top
        self.timings["full_load_ms" if full else "update_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return len(new)

    COMPACT_PARTS = 64

    def _append(self, part):
        i = len(self._parts)
        self._parts.append(part)
        self._alive.append(np.ones(len(part), dtype=bool))
        self._where.update(zip(part.index, ((i, j) for j in range(len(part)))))

    def _take_old(self, ids):
        """The current in-memory rows for ids (to subtract), marked dead."""
        by_part = {}
        for vid in ids:
            loc = self._where.get(vid)
            if loc is not None:
                by_part.setdefault(loc[0], []).append(loc[1])
        if not by_part:
            return None
        pieces = []
        for i, pos in by_part.items():
            pieces.append(self._parts[i].iloc[pos])
            self._alive[i][pos] = False
        return pd.concat(pieces) if len(pieces) > 1 else pieces[0]

    def _compact(self):
        frame = self.frame
        self._parts, self._alive, self._where = [], [], {}
        self._append(frame)

    @property
    def frame(self):
        """All current units as one frame (built on demand)."""
        if not self._parts:
            return None
        live = [p[a] for p, a in zip(self._parts, self._alive) if a.any()]
        return pd.concat(live) if len(live) > 1 else live[0]

    def recount(self):
        """Full recount from the in-memory units (for checking the running counts)."""
        f = self.frame
        return tally(f, self.ceilings) if f is not None else {}

    # ---- helpers

    def _meta(self, key, default=None):
        return self.store.get_meta(key, default) if self.store else default

    def _alert(self, alerts, layer, severity, key, message, group=None, value=None,
               limit=None, sim=None, now=None):
        aid = f"{layer}:{key}"
        first = self._seen.setdefault(aid, {"sim": sim, "at": now.isoformat(timespec="seconds")})
        alerts.append({"id": aid, "layer": layer, "severity": severity, "group": group,
                       "value": value, "limit": limit, "first_seen_sim": first["sim"],
                       "first_seen_at": first["at"], "message": message})

    def _series(self, prefix):
        """{group: [(day, n, k), ...]} for a p-chart dimension."""
        out = {}
        for (group, day), (n, k) in self.counts.get(prefix, {}).items():
            out.setdefault(group, []).append((day, n, k))
        return {g: sorted(v) for g, v in out.items()}

    def _sum(self, prefix, idx=None):
        """Totals over this prefix's counts, grouped by key[idx] (or overall)."""
        out = {}
        for key, (n, k) in self.counts.get(prefix, {}).items():
            g = key[idx] if idx is not None else "all"
            cur = out.setdefault(g, [0, 0])
            cur[0] += int(n)
            cur[1] += int(k)
        return out

    # ---- snapshot

    def snapshot(self, now=None, refresh=True):
        """Everything the dashboard shows, plus the alert list."""
        now = now or utc_now()
        if refresh:
            self.refresh()
        t0 = time.perf_counter()
        alerts = []
        meta = {k: self._meta(k) for k in ("sim_time", "feed_clock", "feed", "runner_status",
                                           "last_poll_at", "baseline")}
        as_of = meta["feed_clock"] or meta["sim_time"]
        ctx = {"alerts": alerts, "now": now, "sim": as_of}
        out = {"generated_at": now.isoformat(timespec="seconds"), "as_of_sim": as_of,
               "gate_sim_time": meta["sim_time"],
               "health_baseline": {"version": self.hb["version"], "sha256": self.hb_sha},
               "product": self._product(ctx),
               "stability": self._stability(ctx),
               "quality": self._quality(ctx),
               "throughput": self._throughput(ctx),
               "gate": self._gate(ctx, meta)}
        order = {s: i for i, s in enumerate(SEVERITIES)}
        alerts.sort(key=lambda a: (order[a["severity"]], a["layer"], a["id"]))
        live = {a["id"] for a in alerts}
        self._seen = {k: v for k, v in self._seen.items() if k in live}  # resolved alerts reset
        layers = {}
        for layer in LAYERS:
            sev = [a["severity"] for a in alerts if a["layer"] == layer]
            layers[layer] = ("red" if "ALARM" in sev else "amber" if "WATCH" in sev else "green")
        out["layers"] = layers
        out["status"] = ("red" if "red" in layers.values() else
                         "amber" if "amber" in layers.values() else "green")
        out["alerts"] = alerts
        out["alert_counts"] = {s: sum(a["severity"] == s for a in alerts) for s in SEVERITIES}
        self.timings["snapshot_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["timings"] = dict(self.timings)
        return out

    # ---- layer 1: product risk

    def _product(self, ctx):
        verdicts = {v: n for v, (n, _) in self._sum("qc", 0).items()}
        sat = self._sum("saturated").get("all", [0, 0])
        holds = ctx["holds"] = self._holds(ctx) if self.store else []
        for h in holds:
            if h["open"]:
                lead = f", {h['lead_hours']:g} h before final QC" if h["lead_hours"] is not None else ""
                self._alert(ctx["alerts"], "product", "ALARM", f"hold:{h['vehicle_id']}",
                            f"{h['vehicle_id']} held at {h['held_at']}{lead}: {h['reasons']}",
                            group=h["vehicle_id"], value=h["worst_d"], sim=h["held_at"], now=ctx["now"])
        return {"verdicts": {v: verdicts.get(v, 0) for v in ("QUARANTINE", "FLAG", "PASS")},
                "units": int(sum(verdicts.values())),
                "saturated_units": int(sat[1]),
                "holds": holds}

    def _holds(self, ctx):
        cols = self.store.unit_columns
        extra = ", ".join(f'u."{c}"' for c in ("final_qc_timestamp_utc", "final_qc_pass",
                                                "build_date_utc") if c in cols)
        rows = self.store.conn.execute(
            f"SELECT s.vehicle_id, s.reasons, s.worst_servo, s.worst_d{', ' + extra if extra else ''} "
            "FROM scores s JOIN units u USING (vehicle_id) WHERE s.verdict = 'QUARANTINE'").fetchall()
        holds = []
        for r in rows:
            r = dict(r)
            ev = self.store.get_events(r["vehicle_id"])
            raised = [e for e in ev if e["event_type"] == "QUARANTINE_RAISED"]
            held_at = raised[-1]["at_sim"] if raised else None
            later = [e for e in ev if e["event_type"] != "QUARANTINE_RAISED"
                     and (not raised or e["id"] > raised[-1]["id"])]
            last = later[-1] if later else None
            qc_at = r.get("final_qc_timestamp_utc")
            lead = hours_between(held_at, qc_at)
            age = hours_between(held_at, ctx["sim"])
            holds.append({"vehicle_id": r["vehicle_id"], "held_at": held_at, "final_qc_at": qc_at,
                          "final_qc_pass": r.get("final_qc_pass"), "reasons": r["reasons"],
                          "worst_servo": r["worst_servo"], "worst_d": r["worst_d"],
                          "lead_hours": round(lead, 2) if lead is not None else None,
                          "age_hours": round(age, 2) if age is not None else None,
                          "last_action": ({"type": last["event_type"], "actor": last["actor"],
                                           "note": last["note"], "at": last["at_sim"] or last["created_at"]}
                                          if last else None),
                          "open": not (last and last["event_type"] in ("RELEASED", "RETEST_FAIL_REPLACED"))})
        holds.sort(key=lambda h: (not h["open"], -(h["worst_d"] or 0)))
        return holds

    # ---- layer 2: process stability

    def _active_from(self, as_of):
        if not as_of:
            return "9999"
        day = parse_time(as_of).date()
        return (day - dt.timedelta(days=self.rules["chart_active_days"] - 1)).isoformat()

    def _stability(self, ctx):
        active_from = self._active_from(ctx["sim"])
        dims = [("fleet", self.hb["rates"]["vehicle_flag"], "vehicle FLAG rate")]
        dims += [(d, self.hb["rates"]["vehicle_flag"], "vehicle FLAG rate") for d in VEHICLE_DIMS]
        dims += [(f"{s}.{d}", self.hb["rates"]["servo_flag"][s], f"{s} servo flag rate")
                 for s in SERVOS for d in SERVO_DIMS]
        charts, summary = [], {}
        for dim, center, what in dims:
            series = self._series(f"p:{dim}")
            in_control = 0
            for group, pts in sorted(series.items()):
                points = chart_points(pts, center, self.rules, active_from)
                charts.append({"dim": dim, "group": group, "center": center, "measure": what,
                               "points": points})
                if not any(p["signal"] for p in points):
                    in_control += 1
                for p in points:
                    if not p["active"]:
                        continue
                    sev = "ALARM" if p["signal"] == "above" else "WATCH"
                    words = {"above": "above its upper limit",
                             "below": "below its lower limit (unusually clean: check the gate)",
                             "run": f"above the centre line {self.rules['run_length']}+ days in a row"}
                    self._alert(ctx["alerts"], "stability", sev, f"{dim}:{group}:{p['day']}:{p['signal']}",
                                f"{dim} {group} on {p['day']}: {what} {p['p']:.2%} "
                                f"({p['k']}/{p['n']}) {words[p['signal']]}",
                                group=f"{dim} {group}", value=p["p"],
                                limit=p["ucl"] if p["signal"] != "below" else p["lcl"],
                                sim=p["day"], now=ctx["now"])
            summary[dim] = {"groups": len(series), "in_control": in_control}
        hourly = sorted((h[0], int(n), int(k)) for h, (n, k) in self.counts.get("hour", {}).items())
        return {"center_vehicle": self.hb["rates"]["vehicle_flag"],
                "center_servo": self.hb["rates"]["servo_flag"],
                "summary": summary, "charts": charts,
                "fleet_hourly": [{"hour": h, "n": n, "k": k} for h, n, k in hourly]}

    # ---- layer 3: quality-system integrity

    def _quality(self, ctx):
        qc = {}
        for (verdict, qc_pass), (n, _) in self.counts.get("qc", {}).items():
            qc.setdefault(qc_pass, {}).setdefault(verdict, 0)
            qc[qc_pass][verdict] += int(n)
        total = sum(sum(v.values()) for v in qc.values())
        passed = sum(qc.get("1", {}).values())
        gate = {v: sum(d.get(v, 0) for d in qc.values()) for v in ("QUARANTINE", "FLAG", "PASS")}
        share = passed / total if total else None
        if share is not None and share >= 0.999 and (gate["QUARANTINE"] + gate["FLAG"]):
            self._alert(ctx["alerts"], "quality", "INFO", "qc_vs_gate",
                        f"Final QC passed {share:.1%} of {total:,} units while the gate held "
                        f"{gate['QUARANTINE']:,} and flagged {gate['FLAG']:,}",
                        value=share, sim=ctx["sim"], now=ctx["now"])
        warnings = {}
        for w in WARN_TYPES + ("warn_any",):
            by_line, by_station, n = {}, {}, 0
            for (line, station), (c, _) in self.counts.get(f"warn:{w}", {}).items():
                by_line[line] = by_line.get(line, 0) + int(c)
                by_station[station] = by_station.get(station, 0) + int(c)
                n += int(c)
            warnings["any" if w == "warn_any" else w] = {"units": n, "by_line": by_line,
                                                         "by_qc_station": by_station}
        holds = ctx.get("holds", [])
        before = [h for h in holds if h["lead_hours"] is not None and h["lead_hours"] > 0]
        return {"final_qc_pass_share": share, "units": total,
                "qc_vs_gate": {qcv: dict(d) for qcv, d in qc.items()},
                "gate": gate, "held_before_final_qc": len(before), "held_total": len(holds),
                "open_warnings": warnings}

    # ---- layer 4: throughput and rework

    def _throughput(self, ctx):
        by_hour_line, by_day = {}, {}
        for (line, shift, hour), (n, _) in self.counts.get("units", {}).items():
            by_hour_line.setdefault(hour, {})[line] = by_hour_line.setdefault(hour, {}).get(line, 0) + int(n)
            d = by_day.setdefault(hour[:10], {"plant": {}, "line": {}, "shift": {}})
            plant = line.split("/")[0]
            for k, g in (("plant", plant), ("line", line), ("shift", shift)):
                d[k][g] = d[k].get(g, 0) + int(n)
        gate_hour = self._meta("sim_time")
        watch = self.throughput_watches(gate_hour, by_hour_line)
        for line, n, normal in watch:
            self._alert(ctx["alerts"], "throughput", "WATCH", f"units:{line}",
                        f"{line} built {n} units in {gate_hour} (normal {normal:g}/h)",
                        group=line, value=n, limit=normal * self.rules["throughput_watch_share"],
                        sim=gate_hour, now=ctx["now"])
        rework = {g: {"units": n, "reworked": k, "share": round(k / n, 4) if n else None}
                  for g, (n, k) in self._sum("rework", 0).items()}
        n_all = sum(v["units"] for v in rework.values())
        k_all = sum(v["reworked"] for v in rework.values())
        codes = {}
        for (code, line), (n, _) in self.counts.get("rework_code", {}).items():
            c = codes.setdefault(code, {"units": 0, "by_line": {}})
            c["units"] += int(n)
            c["by_line"][line] = int(n)
        return {"latest_hour": gate_hour, "units_latest_hour": by_hour_line.get(gate_hour, {}),
                "normal_per_hour": self.hb["throughput"]["line_units_per_hour_median"],
                "by_hour_line": by_hour_line, "by_day": by_day,
                "rework": {"units": n_all, "reworked": k_all,
                           "share": round(k_all / n_all, 4) if n_all else None,
                           "by_line": rework, "by_code": codes}}

    def throughput_watches(self, hour, by_hour_line=None):
        """Lines that built under the watch share of their normal units in `hour`."""
        if not hour:
            return []
        if by_hour_line is None:
            by_hour_line = {}
            for (line, _, h), (n, _) in self.counts.get("units", {}).items():
                d = by_hour_line.setdefault(h, {})
                d[line] = d.get(line, 0) + int(n)
        built = by_hour_line.get(hour, {})
        if not built:
            return []  # no units at all: a feed problem, reported by the gate layer
        out = []
        for line, normal in self.hb["throughput"]["line_units_per_hour_median"].items():
            n = built.get(line, 0)
            if n < normal * self.rules["throughput_watch_share"]:
                out.append((line, n, normal))
        return out

    # ---- layer 5: gate and data health

    def correlation(self):
        """Max |r| between the five metrics of each servo over the most recent
        units by build hour. D's false-alarm rate assumes they're uncorrelated."""
        w = self.rules["corr_window"]
        cols = list(spc.MEASURE_COLS)
        # Straight from the parts' live rows: no 200,000-row frame is built
        hours = [p["hour_n"].to_numpy()[a] for p, a in zip(self._parts, self._alive)]
        n = int(sum(len(h) for h in hours))
        if n < self.rules["corr_min_units"]:
            return {"units": n, "max_abs_r": None, "by_servo": {}}
        hours = np.concatenate(hours)
        M = np.concatenate([p[cols].to_numpy(dtype=np.float64)[a] for p, a in zip(self._parts, self._alive)])
        if n > w:
            M = M[np.argpartition(hours, n - w)[n - w:]]
        by_servo, worst = {}, 0.0
        for s in SERVOS:
            X = M[:, [cols.index(spc.col(s, m)) for m in spc.METRICS]]
            X = X[~np.isnan(X).any(axis=1)]
            r = np.corrcoef(X, rowvar=False)
            np.fill_diagonal(r, 0.0)
            i, j = np.unravel_index(np.abs(r).argmax(), r.shape)
            by_servo[s] = {"max_abs_r": round(float(abs(r[i, j])), 4),
                           "pair": [spc.METRICS[i], spc.METRICS[j]]}
            worst = max(worst, abs(r[i, j]))
        return {"units": int(len(M)), "max_abs_r": round(float(worst), 4), "by_servo": by_servo}

    def _gate(self, ctx, meta):
        al, now, sim, rules = ctx["alerts"], ctx["now"], ctx["sim"], self.rules
        out = {"database": bool(self.store)}
        if not self.store:
            self._alert(al, "gate", "ALARM", "no_db", "The gate has not created its database yet",
                        sim=sim, now=now)
            return out
        # Runner alive and working
        status = meta["runner_status"] or {"state": "UNKNOWN"}
        beat = parse_time(meta["last_poll_at"])
        age = (now - beat).total_seconds() if beat else None
        out["runner"] = {"status": status, "last_poll_at": meta["last_poll_at"],
                         "heartbeat_age_s": round(age, 1) if age is not None else None}
        if status.get("state") == "STALLED":
            self._alert(al, "gate", "ALARM", "stalled", f"Gate stalled: {status.get('message')}",
                        sim=sim, now=now)
        if age is None or age > rules["heartbeat_stale_s"]:
            self._alert(al, "gate", "ALARM", "heartbeat",
                        "No heartbeat from the gate" + (f" for {age:.0f} s" if age is not None else ""),
                        value=age, limit=rules["heartbeat_stale_s"], sim=sim, now=now)
        # Feed silent
        feed = meta["feed"]
        out["feed"] = feed
        if feed:
            cadence = feed.get("cadence_s")
            if feed.get("state") == "finished":
                self._alert(al, "gate", "INFO", "feed_finished",
                            f"Feed finished at {feed.get('clock')} (replay complete)", sim=sim, now=now)
            elif cadence and feed.get("silent_s", 0) > rules["feed_stale_cadences"] * cadence:
                self._alert(al, "gate", "ALARM", "feed_silent",
                            f"Feed silent for {feed['silent_s']:.0f} s (expected a build hour every "
                            f"{cadence:g} s) as of {feed.get('checked_at')}",
                            value=feed["silent_s"], limit=rules["feed_stale_cadences"] * cadence,
                            sim=sim, now=now)
        # Lag behind the feed, in build hours
        lag = hours_between(meta["sim_time"], meta["feed_clock"])
        out["lag_build_hours"] = round(lag, 2) if lag is not None else None
        if lag is not None and lag > rules["lag_watch_hours"]:
            self._alert(al, "gate", "WATCH", "lag", f"Gate is {lag:g} build hours behind the feed",
                        value=lag, limit=rules["lag_watch_hours"], sim=sim, now=now)
        # Batch latency, from the measured duration (never from timestamps)
        runs = self.store.conn.execute(
            "SELECT id, started_at, duration_ms, rejected FROM runs ORDER BY id DESC LIMIT ?",
            (rules["latency_window_runs"],)).fetchall()
        d = np.array([r["duration_ms"] for r in runs if r["duration_ms"] is not None], dtype=float)
        cadence_ms = (feed or {}).get("cadence_s") and feed["cadence_s"] * 1000
        lat = {"runs": len(d)}
        if len(d):
            lat.update(p50_ms=float(np.percentile(d, 50)), p95_ms=float(np.percentile(d, 95)),
                       max_ms=float(d.max()))
            if cadence_ms:
                lat.update(busy_share_p50=round(lat["p50_ms"] / cadence_ms, 3),
                           busy_share_p95=round(lat["p95_ms"] / cadence_ms, 3))
                if lat["p95_ms"] > rules["latency_watch_share"] * cadence_ms:
                    self._alert(al, "gate", "WATCH", "latency",
                                f"Batch time p95 {lat['p95_ms']:.0f} ms is over "
                                f"{rules['latency_watch_share']:.0%} of the {cadence_ms:.0f} ms interval",
                                value=lat["p95_ms"], limit=rules["latency_watch_share"] * cadence_ms,
                                sim=sim, now=now)
        out["latency"] = lat
        # Rejected and logged rows over the same recent runs
        errors = {}
        if runs:
            first = runs[-1]
            rows = self.store.conn.execute(
                "SELECT reason FROM ingest_errors WHERE run_id >= ? OR "
                "(run_id IS NULL AND created_at >= ?)", (first["id"], first["started_at"])).fetchall()
            for (reason,) in rows:
                key = re.sub(r"=.*", "", reason.split(":")[0]).strip()
                errors[key] = errors.get(key, 0) + 1
        out["ingest_errors_recent"] = errors
        out["rejected_recent"] = int(sum(r["rejected"] for r in runs))
        if errors:
            self._alert(al, "gate", "WATCH", "ingest_errors",
                        f"{sum(errors.values())} rows logged as bad input in the last {len(runs)} runs: "
                        + ", ".join(f"{k} ({v})" for k, v in sorted(errors.items())),
                        value=sum(errors.values()), sim=sim, now=now)
        # Scoring baseline drift (the runner checks the file every heartbeat)
        base = meta["baseline"]
        out["baseline"] = base
        if base and base.get("on_disk", {}).get("state") != "same":
            self._alert(al, "gate", "WATCH", "baseline_drift",
                        f"Scoring baseline on disk ({base['on_disk'].get('state')}) is not the one in use "
                        f"({base['in_use']['version']}); scoring unchanged until the gate restarts",
                        sim=sim, now=now)
        in_use = (base or {}).get("in_use", {}).get("version")
        ref = self.hb.get("reference", {}).get("scoring_baseline_version")
        if in_use and ref and in_use != ref:
            self._alert(al, "gate", "WATCH", "health_baseline_mismatch",
                        f"Health baseline was built for scoring baseline {ref}; the gate uses {in_use}",
                        sim=sim, now=now)
        # Health baseline drift
        try:
            disk = file_sha256(self.baseline_path)
        except OSError:
            disk = None
        out["health_baseline"] = {"version": self.hb["version"], "sha256": self.hb_sha,
                                  "on_disk": "same" if disk == self.hb_sha else "differs" if disk else "missing"}
        if disk != self.hb_sha:
            self._alert(al, "gate", "WATCH", "health_baseline_drift",
                        "Health baseline on disk differs from the one in use; limits unchanged until restart",
                        sim=sim, now=now)
        # The assumption behind D
        corr = self.correlation()
        out["correlation"] = corr
        if corr["max_abs_r"] is not None and corr["max_abs_r"] > rules["corr_alarm"]:
            bad = {s: v for s, v in corr["by_servo"].items() if v["max_abs_r"] > rules["corr_alarm"]}
            self._alert(al, "gate", "ALARM", "correlation",
                        "Bench readings are correlated (" + ", ".join(
                            f"{s} {v['pair'][0]}/{v['pair'][1]} r={v['max_abs_r']}" for s, v in bad.items())
                        + "): D's false-alarm rate no longer holds",
                        value=corr["max_abs_r"], limit=rules["corr_alarm"], sim=sim, now=now)
        # Ceiling share per reading (for fixture owners)
        ceil = self.counts.get("ceiling", {})
        out["ceilings"] = {c: {"readings": int(ceil.get((c,), [0, 0])[0]),
                               "at_ceiling": int(ceil.get((c,), [0, 0])[1]),
                               "ceiling": self.ceilings[c]}
                           for c in self.ceilings}
        return out


# --------------------------------------------------------------- CLI

def print_summary(s):
    lay = "  ".join(f"{k} {v}" for k, v in s["layers"].items())
    print(f"[health] {s['generated_at']}  as of {s['as_of_sim']}  status {s['status'].upper()}  ({lay})")
    p, q, g = s["product"], s["quality"], s["gate"]
    v = p["verdicts"]
    print(f"  units {p['units']:,}: {v['QUARANTINE']} QUARANTINE / {v['FLAG']:,} FLAG / {v['PASS']:,} PASS; "
          f"saturated {p['saturated_units']:,}")
    if q["final_qc_pass_share"] is not None:
        print(f"  final QC passed {q['final_qc_pass_share']:.2%}; open warnings "
              f"{q['open_warnings']['any']['units']:,}; held before final QC "
              f"{q['held_before_final_qc']}/{q['held_total']}")
    if g.get("runner"):
        lat = g["latency"]
        print(f"  gate {g['runner']['status'].get('state')}, heartbeat {g['runner']['heartbeat_age_s']} s ago, "
              f"lag {g['lag_build_hours']} build h, batch p50/p95 "
              f"{lat.get('p50_ms', '-')}/{lat.get('p95_ms', '-')} ms, "
              f"max |r| {g['correlation']['max_abs_r']}")
    c = s["alert_counts"]
    print(f"  alerts: {c['ALARM']} ALARM, {c['WATCH']} WATCH, {c['INFO']} INFO")
    for a in s["alerts"][:20]:
        print(f"    {a['severity']:5s} {a['layer']:10s} {a['message']}")
    if len(s["alerts"]) > 20:
        print(f"    … {len(s['alerts']) - 20} more")
    print(f"  timings {s['timings']}", flush=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["baseline"]:
        ap = argparse.ArgumentParser(prog="pipeline.health baseline",
                                     description="Build the frozen health baseline from a reference month.")
        ap.add_argument("--input", default=spc.DEFAULT_INPUT)
        ap.add_argument("--scoring-baseline", default=spc.DEFAULT_BASELINE)
        ap.add_argument("--out", default=DEFAULT_HEALTH_BASELINE)
        ap.add_argument("--version")
        a = ap.parse_args(argv[1:])
        hb = build_health_baseline(a.input, a.scoring_baseline, a.version)
        with open(a.out, "w") as fh:
            json.dump(hb, fh, indent=2)
            fh.write("\n")
        print(f"wrote {a.out}: version {hb['version']}, vehicle FLAG rate {hb['rates']['vehicle_flag']:.4%}")
        return 0
    ap = argparse.ArgumentParser(description="Production, quality-system and gate health.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--baseline", default=DEFAULT_HEALTH_BASELINE)
    ap.add_argument("--json", action="store_true", help="print the full snapshot as JSON")
    ap.add_argument("--watch", type=float, help="refresh every N seconds")
    a = ap.parse_args(argv)
    h = Health(a.db, a.baseline)
    try:
        while True:
            s = h.snapshot()
            if a.json:
                print(json.dumps(s, default=str))
            else:
                print_summary(s)
            if not a.watch:
                break
            time.sleep(a.watch)
    except KeyboardInterrupt:
        pass
    finally:
        h.close()
    return 0 if s["status"] != "red" else 2


if __name__ == "__main__":
    sys.exit(main())
