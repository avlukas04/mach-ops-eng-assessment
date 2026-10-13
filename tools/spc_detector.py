"""
SPC Detector — two-tier servo bench gate (Part 2)
=================================================
Scores every vehicle's servo bench data against a frozen baseline and
returns PASS / FLAG / QUARANTINE with reason codes. This is the automated
check that would have stopped GLDR-0421 before flight (REPORT §6.1).

Why this design
---------------
* A stiff (high-friction) servo shows up on the bench as several readings
  on the SAME servo running high together: more sweep current, more peak
  current, slower sweep, more position error, stiffer backdrive. On healthy
  units these five readings are uncorrelated (|r| <= 0.01), so several high
  at once is very unlikely by chance. The combined score D measures that.
* Robust baseline (median, IQR / 1.349) instead of mean / std, because every
  reading is clipped at an instrument floor, which shrinks the std and
  inflates z exactly where the limits sit.
* High side only. Every friction symptom is a high reading, and the low end
  is cut off by the instrument floor so it can't be measured reliably.
* Frozen baseline. Limits come from a version-controlled JSON file, never
  from the live data, so a slow drift can't quietly widen them.
* Fails closed. A vehicle with a missing reading can't be certified, so it
  is quarantined (INCOMPLETE), not passed.

Scoring
-------
For each servo s (5) and metric m (5):
    z[s,m] = max(0, (x - median) / sigma_hat)
    D[s]   = sqrt(sum over m of z[s,m]^2)

Tiers and reason codes:
    QUARANTINE  SIGNATURE   D >= 6.0 on any servo
                EXTREME     any single z >= 5.5
                INCOMPLETE  a required reading is missing
    FLAG        HIGH        any z >= 3.0
                ELEVATED    D >= 4.5 on any servo
                SATURATED   a reading is at the instrument ceiling
    PASS        everything else

Both QUARANTINE limits are set to about 0.1 expected false alarms per
200,000 healthy vehicles: P(D >= 6) = 1.0e-7 per servo x 1M servos, and
P(z >= 5.5) = 1.9e-8 per reading x 5M readings. FLAG is close to the chance
rate (about 3.3% of vehicles), so it feeds lot / line / shift p-charts
rather than per-unit holds.

Usage (from the repository root)
--------------------------------
  # 1. Freeze a baseline (once; commit the JSON; redo only after review)
  python3 tools/spc_detector.py baseline --input manufacturing.csv

  # 2. Score the fleet -> outputs/spc/spc_results.csv + spc_summary.json
  python3 tools/spc_detector.py score --input manufacturing.csv

  # 3. Explain one vehicle (flight-test go/no-go)
  python3 tools/spc_detector.py score --input manufacturing.csv --vehicle GLDR-0421

Exit codes for `score`: 0 = no QUARANTINE, 2 = at least one QUARANTINE,
1 = input or baseline error. A bench station or CI job can block on 2.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_INPUT = os.path.join(PROJECT_ROOT, "manufacturing.csv")
DEFAULT_BASELINE = os.path.join(SCRIPT_DIR, "spc_baseline.json")
DEFAULT_OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "spc")

SERVOS = ["elevon_l", "elevon_r", "aileron_l", "aileron_r", "rudder"]
METRICS = ["sweep_current_ma", "peak_current_ma", "sweep_time_ms",
           "position_error_rms_deg", "backdrive_torque_nmm"]
METRIC_LABELS = {"sweep_current_ma": "sweep current", "peak_current_ma": "peak current",
                 "sweep_time_ms": "sweep time", "position_error_rms_deg": "position error",
                 "backdrive_torque_nmm": "backdrive torque"}

RULES = {
    "quarantine_d": 6.0,
    "quarantine_z": 5.5,
    "flag_z": 3.0,
    "flag_d": 4.5,
}

# A value is treated as an instrument ceiling when at least this many
# readings pile up exactly on it at the top of the range (a real tail has
# one unit per value) and fewer than this many readings lie above it
CEILING_MIN_REPEATS = 10

META_COLS = ["vehicle_id", "build_date_utc", "build_shift", "factory_site", "line_id"]
LOT_COLS = [f"{s}_servo_lot_id" for s in SERVOS]
SERIAL_COLS = [f"{s}_servo_serial" for s in SERVOS]


def col(servo, metric):
    return f"{servo}_servo_{metric}"


MEASURE_COLS = [col(s, m) for s in SERVOS for m in METRICS]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_units(path):
    """Read only the columns the gate needs. Fails with a clear message if
    the file or a required column is missing."""
    if not os.path.exists(path):
        raise SystemExit(f"error: input file not found: {path}")
    header = pd.read_csv(path, nrows=0).columns
    missing = [c for c in META_COLS + MEASURE_COLS if c not in header]
    if missing:
        raise SystemExit(f"error: input is missing required columns: {', '.join(missing)}")
    optional = [c for c in LOT_COLS + SERIAL_COLS if c in header]
    return pd.read_csv(path, usecols=META_COLS + MEASURE_COLS + optional,
                       dtype={c: str for c in META_COLS + optional})


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
def find_ceiling(v):
    """
    Highest value that at least CEILING_MIN_REPEATS readings sit on, if
    almost nothing lies above it. Peak current, for example, clips at
    380 mA on every servo, but one reading in the fleet (GLDR-0421's
    defective servo, 400.32 mA) lies above it, so the ceiling is not
    simply the maximum.
    """
    values, counts = np.unique(v, return_counts=True)
    piled = values[counts >= CEILING_MIN_REPEATS]
    if piled.size == 0:
        return None
    top = piled.max()
    if top < np.percentile(v, 99) or (v > top).sum() >= CEILING_MIN_REPEATS:
        return None
    return float(top)


def build_baseline(df, source_path, built_before=None):
    """
    Compute the frozen reference: median, robust sigma and instrument
    ceiling for each of the 25 measurements. Median / IQR tolerate a few
    defective units in the reference set (up to 25% before they move).
    """
    ref = df
    if built_before:
        ref = df[df["build_date_utc"] < built_before]
        if ref.empty:
            raise SystemExit(f"error: no units built before {built_before}")

    measures = {}
    for c in MEASURE_COLS:
        v = ref[c].dropna().to_numpy(dtype=float)
        q1, med, q3 = np.percentile(v, [25, 50, 75])
        sigma = (q3 - q1) / 1.349
        ceiling = find_ceiling(v)
        measures[c] = {"median": round(float(med), 6), "sigma": round(float(sigma), 6),
                       "ceiling": ceiling}

    return {
        "version": dt.date.today().isoformat(),
        "description": "Frozen SPC baseline for the servo bench gate. Review before changing.",
        "reference": {
            "source_file": os.path.basename(source_path),
            "source_sha256": file_sha256(source_path),
            "built_before": built_before,
            "n_units": int(len(ref)),
        },
        "rules": RULES,
        "measures": measures,
    }


def load_baseline(path):
    if not os.path.exists(path):
        raise SystemExit(f"error: baseline not found: {path}\n"
                         f"       create it with: python3 tools/spc_detector.py baseline")
    with open(path) as f:
        base = json.load(f)
    missing = [c for c in MEASURE_COLS if c not in base.get("measures", {})]
    if missing:
        raise SystemExit(f"error: baseline is missing measures: {', '.join(missing)}")
    return base


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def compute_scores(df, base):
    """
    Returns arrays shaped (vehicles, servos, metrics) for z and saturation,
    (vehicles, servos) for D, and a per-vehicle missing-data mask.
    """
    n = len(df)
    X = df[MEASURE_COLS].to_numpy(dtype=float).reshape(n, len(SERVOS), len(METRICS))
    med = np.array([base["measures"][c]["median"] for c in MEASURE_COLS]).reshape(len(SERVOS), len(METRICS))
    sig = np.array([base["measures"][c]["sigma"] for c in MEASURE_COLS]).reshape(len(SERVOS), len(METRICS))
    ceil = np.array([base["measures"][c]["ceiling"] if base["measures"][c]["ceiling"] is not None
                     else np.inf for c in MEASURE_COLS]).reshape(len(SERVOS), len(METRICS))

    missing = np.isnan(X)
    z = np.clip((np.nan_to_num(X, nan=0.0) - med) / sig, 0.0, None)
    z[missing] = 0.0
    D = np.sqrt((z ** 2).sum(axis=2))
    saturated = X >= ceil
    return z, D, saturated, missing.any(axis=(1, 2))


def reasons_for(i, z, D, saturated, missing_cols, rules):
    """Reason strings for one vehicle, split into QUARANTINE and FLAG lists."""
    quarantine, flag = [], []
    if missing_cols:
        more = "…" if len(missing_cols) > 1 else ""
        quarantine.append(f"INCOMPLETE {len(missing_cols)} missing ({missing_cols[0]}{more})")
    for s, servo in enumerate(SERVOS):
        if D[i, s] >= rules["quarantine_d"]:
            quarantine.append(f"SIGNATURE {servo} D={D[i, s]:.2f}")
        elif D[i, s] >= rules["flag_d"]:
            flag.append(f"ELEVATED {servo} D={D[i, s]:.2f}")
        for m, metric in enumerate(METRICS):
            zv = z[i, s, m]
            if zv >= rules["quarantine_z"]:
                quarantine.append(f"EXTREME {servo}.{metric} z={zv:.2f}")
            elif zv >= rules["flag_z"]:
                flag.append(f"HIGH {servo}.{metric} z={zv:.2f}")
            if saturated[i, s, m]:
                flag.append(f"SATURATED {servo}.{metric}")
    return quarantine, flag


def classify(df, base):
    """Score every vehicle and return one result row per vehicle."""
    rules = base["rules"]
    z, D, saturated, incomplete = compute_scores(df, base)
    n = len(df)

    # Tier masks, vectorized over the whole fleet
    servo_flag = (D >= rules["flag_d"]) | (z.max(axis=2) >= rules["flag_z"]) | saturated.any(axis=2)
    is_quarantine = incomplete | (D >= rules["quarantine_d"]).any(axis=1) | \
        (z >= rules["quarantine_z"]).any(axis=(1, 2))
    is_flag = ~is_quarantine & servo_flag.any(axis=1)
    verdict = np.where(is_quarantine, "QUARANTINE", np.where(is_flag, "FLAG", "PASS"))

    # Reason text only for vehicles that didn't pass
    reasons = np.full(n, "", dtype=object)
    X = df[MEASURE_COLS].to_numpy(dtype=float)
    for i in np.flatnonzero(verdict != "PASS"):
        missing_cols = [MEASURE_COLS[k] for k in np.flatnonzero(np.isnan(X[i]))]
        q, f = reasons_for(i, z, D, saturated, missing_cols, rules)
        reasons[i] = "; ".join(q + f)

    ws = D.argmax(axis=1)
    wm = z[np.arange(n), ws].argmax(axis=1)
    out = pd.DataFrame({
        "vehicle_id": df["vehicle_id"].to_numpy(),
        "verdict": verdict,
        "reasons": reasons,
        "worst_servo": np.array(SERVOS)[ws],
        "worst_d": D[np.arange(n), ws].round(3),
        "worst_metric": np.array(METRICS)[wm],
        "worst_z": z[np.arange(n), ws, wm].round(3),
        "saturated": saturated.any(axis=(1, 2)),
    })
    for c in META_COLS[1:]:
        out[c] = df[c].to_numpy()
    for s, servo in enumerate(SERVOS):
        out[f"d_{servo}"] = D[:, s].round(3)
        out[f"flag_{servo}"] = servo_flag[:, s].astype(int)
        lot_col = f"{servo}_servo_lot_id"
        if lot_col in df.columns:
            out[f"lot_{servo}"] = df[lot_col].to_numpy()
    return out, z, D


def summarize(results, base, input_path, top_n=25):
    counts = results["verdict"].value_counts()
    reason_counts = {}
    for code in ["SIGNATURE", "EXTREME", "INCOMPLETE", "HIGH", "ELEVATED", "SATURATED"]:
        reason_counts[code] = int(results["reasons"].str.contains(rf"\b{code}\b", regex=True).sum())
    q = results[results["verdict"] == "QUARANTINE"].sort_values("worst_d", ascending=False)
    f = results[results["verdict"] == "FLAG"].sort_values("worst_d", ascending=False)
    keep = ["vehicle_id", "worst_servo", "worst_d", "reasons"]
    return {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "input_file": os.path.basename(input_path),
        "baseline_version": base["version"],
        "baseline_reference_sha256": base["reference"]["source_sha256"],
        "rules": base["rules"],
        "n_vehicles": int(len(results)),
        "counts": {t: int(counts.get(t, 0)) for t in ["QUARANTINE", "FLAG", "PASS"]},
        "flag_rate": round(float(counts.get("FLAG", 0)) / max(len(results), 1), 5),
        "vehicles_with_reason": reason_counts,
        "quarantine": q[keep].to_dict(orient="records"),
        "flag_retest_priority": f[keep].head(top_n).to_dict(orient="records"),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def explain_vehicle(df, results, z, D, base, vid):
    idx = np.flatnonzero(df["vehicle_id"].to_numpy() == vid)
    if idx.size == 0:
        print(f"    {vid}: not found in input")
        return
    i = int(idx[0])
    r = results.iloc[i]
    rules = base["rules"]
    print(f"\n  {vid}  →  {r['verdict']}")
    print(f"  Built {r['build_date_utc']}, {r['factory_site']} {r['line_id']}, shift {r['build_shift']}")
    head = "".join(f"{METRIC_LABELS[m]:>17}" for m in METRICS)
    print(f"\n  {'Servo':<10}{head}{'D':>8}")
    for s, servo in enumerate(SERVOS):
        cells = ""
        for m in range(len(METRICS)):
            zv = z[i, s, m]
            mark = "!!" if zv >= rules["quarantine_z"] else "! " if zv >= rules["flag_z"] else "  "
            cells += f"{zv:>15.2f}{mark}"
        d = D[i, s]
        dmark = "!!" if d >= rules["quarantine_d"] else "! " if d >= rules["flag_d"] else "  "
        print(f"  {servo:<10}{cells}{d:>6.2f}{dmark}")
    print(f"\n  z = robust high-side score; '!' = FLAG level, '!!' = QUARANTINE level")
    print(f"  Reasons: {r['reasons'] or 'none'}")


def cmd_baseline(args):
    df = load_units(args.input)
    base = build_baseline(df, args.input, args.built_before)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(base, f, indent=2)
    ceilings = {c: m["ceiling"] for c, m in base["measures"].items() if m["ceiling"] is not None}
    print(f"Baseline written to {args.out}")
    print(f"  Reference: {base['reference']['n_units']:,} units from {base['reference']['source_file']}")
    print(f"  Instrument ceilings detected: {len(ceilings)} measures")
    for c, v in ceilings.items():
        print(f"    {c} = {v:g}")
    return 0


def cmd_score(args):
    base = load_baseline(args.baseline)
    df = load_units(args.input)
    if df["vehicle_id"].duplicated().any():
        dups = df.loc[df["vehicle_id"].duplicated(), "vehicle_id"].unique()[:5]
        raise SystemExit(f"error: duplicate vehicle_id in input, e.g. {', '.join(dups)}")

    results, z, D = classify(df, base)
    summary = summarize(results, base, args.input)

    print("=" * 70)
    print("  SPC DETECTOR — servo bench gate")
    print("=" * 70)
    print(f"  Input:    {os.path.basename(args.input)} ({len(df):,} vehicles)")
    print(f"  Baseline: {os.path.basename(args.baseline)} (version {base['version']}, "
          f"{base['reference']['n_units']:,} reference units)")
    c = summary["counts"]
    print(f"\n  QUARANTINE {c['QUARANTINE']:>8,}   block shipment, retest servo, engineer sign-off")
    print(f"  FLAG       {c['FLAG']:>8,}   ships; feeds lot / line / shift p-charts ({summary['flag_rate']:.2%})")
    print(f"  PASS       {c['PASS']:>8,}")
    print("\n  Vehicles by reason: " + ", ".join(f"{k} {v:,}" for k, v in summary["vehicles_with_reason"].items()))
    if summary["quarantine"]:
        print("\n  QUARANTINE list:")
        for q in summary["quarantine"]:
            print(f"    {q['vehicle_id']:<13} {q['worst_servo']:<10} D={q['worst_d']:.2f}  {q['reasons']}")

    for vid in args.vehicle or []:
        explain_vehicle(df, results, z, D, base, vid)

    if not args.no_write:
        os.makedirs(args.out_dir, exist_ok=True)
        csv_path = os.path.join(args.out_dir, "spc_results.csv")
        json_path = os.path.join(args.out_dir, "spc_summary.json")
        if args.format in ("csv", "both"):
            results.to_csv(csv_path, index=False)
            print(f"\n  Wrote {csv_path}")
        if args.format in ("json", "both"):
            with open(json_path, "w") as f:
                json.dump(summary, f, indent=2, default=str)
            print(f"  Wrote {json_path}")
    print("=" * 70)
    return 2 if c["QUARANTINE"] else 0


def build_parser():
    ap = argparse.ArgumentParser(description="Two-tier SPC gate for servo bench data.")
    sub = ap.add_subparsers(dest="command", required=True)

    b = sub.add_parser("baseline", help="Compute and freeze the reference baseline (JSON).")
    b.add_argument("--input", default=DEFAULT_INPUT, help="Manufacturing CSV used as the reference set")
    b.add_argument("--out", default=DEFAULT_BASELINE, help="Where to write the baseline JSON")
    b.add_argument("--built-before", help="Only use units built before this date (YYYY-MM-DD)")
    b.set_defaults(func=cmd_baseline)

    s = sub.add_parser("score", help="Score vehicles against the frozen baseline.")
    s.add_argument("--input", default=DEFAULT_INPUT, help="Manufacturing CSV to score")
    s.add_argument("--baseline", default=DEFAULT_BASELINE, help="Frozen baseline JSON")
    s.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Directory for results")
    s.add_argument("--format", choices=["csv", "json", "both"], default="both")
    s.add_argument("--vehicle", action="append", help="Print a z / D breakdown for this vehicle (repeatable)")
    s.add_argument("--no-write", action="store_true", help="Print only; don't write result files")
    s.set_defaults(func=cmd_score)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
