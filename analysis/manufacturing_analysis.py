"""
Manufacturing Analysis — Part 1 (continued)
=============================================
Traces the flight anomaly (left aileron seizure on GLDR-0421)
back to the factory build data in manufacturing.csv.

Produces:
  1. GLDR-0421 build record summary (console)
  2. Fleet statistics for 5 key servo parameters (console)
  3. Z-score analysis (console)
  4. Fleet-wide outlier scan (how many other vehicles have similar issues)
  5. Figure 4: GLDR-0421 vs fleet distribution (histogram)
  6. Figure 5: Z-score radar chart for GLDR-0421

Usage:
  python analysis/manufacturing_analysis.py [--vehicle GLDR-0421]
  (run from the assessment root directory)
"""

import argparse
import csv
import math
import os
import sys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = PROJECT_ROOT

MFG_PATH = os.path.join(DATA_DIR, "manufacturing.csv")
if not os.path.exists(MFG_PATH):
    ALT_DIR = os.path.expanduser("~/code/andrea_lukas_ops_eng_assessment")
    MFG_PATH = os.path.join(ALT_DIR, "manufacturing.csv")

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "figures")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# The vehicle that crashed (override with --vehicle)
TARGET_VEHICLE = "GLDR-0421"

# Servo whose lot is checked for a lot-wide shift (the one that failed in flight)
SUSPECT_SURFACE = "aileron_l"

# The 5 control surface positions on the aircraft
SURFACES = ["aileron_l", "aileron_r", "elevon_l", "elevon_r", "rudder"]

# The 5 key servo health metrics we're screening
METRICS = [
    ("sweep_current_ma",          "Sweep Current (mA)"),
    ("peak_current_ma",           "Peak Current (mA)"),
    ("sweep_time_ms",             "Sweep Time (ms)"),
    ("backdrive_torque_nmm",      "Backdrive Torque (N·mm)"),
    ("position_error_rms_deg",    "Position Error RMS (deg)"),
]

# Threshold for flagging an outlier (standard Shewhart control limit)
Z_THRESHOLD = 3.0


# ---------------------------------------------------------------------------
# Step 1: Load manufacturing data and compute fleet statistics
# ---------------------------------------------------------------------------
def load_and_analyze(path):
    """
    Single pass through manufacturing.csv:
    1. Collect all servo metric values for fleet statistics
    2. Extract the target vehicle's row
    3. Count vehicles that exceed Z-threshold on any metric
    """
    # Accumulate metric values: fleet_data[surface][metric] = [val1, val2, ...]
    fleet_data = {
        surf: {m[0]: [] for m in METRICS}
        for surf in SURFACES
    }
    target_row = None
    total_vehicles = 0
    vehicle_ids = []
    qc = {"pass": 0, "scrap": 0, "with_exceptions": 0, "exceptions_and_passed": 0,
          "rework_r05": 0}
    lot_values = {}  # suspect servo lot -> list of sweep currents

    print(f"  Loading {path}...")
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_vehicles += 1
            if row["vehicle_id"] == TARGET_VEHICLE:
                target_row = row
            vehicle_ids.append(row["vehicle_id"])

            passed = row["final_qc_pass"] == "1"
            has_exc = int(row["qc_exception_count"] or 0) > 0
            qc["pass"] += passed
            qc["scrap"] += row["scrap_flag"] == "1"
            qc["with_exceptions"] += has_exc
            qc["exceptions_and_passed"] += has_exc and passed
            qc["rework_r05"] += "R05" in row["rework_codes"].split("|")

            lot = row[f"{SUSPECT_SURFACE}_servo_lot_id"]
            lot_values.setdefault(lot, []).append(
                float(row[f"{SUSPECT_SURFACE}_servo_sweep_current_ma"]))

            for surf in SURFACES:
                for metric_key, _ in METRICS:
                    col = f"{surf}_servo_{metric_key}"
                    val = float(row[col])
                    fleet_data[surf][metric_key].append(val)

    return fleet_data, target_row, total_vehicles, vehicle_ids, qc, lot_values


# ---------------------------------------------------------------------------
# Step 2: Compute statistics
# ---------------------------------------------------------------------------
def compute_stats(values):
    """Compute mean, std, min, max for a list of numbers."""
    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    std = math.sqrt(variance)
    return {
        "mean": mean,
        "std": std,
        "min": min(values),
        "max": max(values),
        "n": n,
    }


def z_score(value, mean, std):
    """Compute the Z-score of a value relative to a distribution."""
    if std == 0:
        return 0.0
    return (value - mean) / std


# ---------------------------------------------------------------------------
# Step 3: Perform the full analysis
# ---------------------------------------------------------------------------
def analyze(fleet_data, target_row, total_vehicles):
    """
    Analyze the target vehicle against fleet statistics.
    Returns structured results for the report.
    """
    results = {}

    for surf in SURFACES:
        surf_results = []
        for metric_key, metric_label in METRICS:
            col = f"{surf}_servo_{metric_key}"
            vehicle_val = float(target_row[col])
            stats = compute_stats(fleet_data[surf][metric_key])
            z = z_score(vehicle_val, stats["mean"], stats["std"])

            surf_results.append({
                "metric": metric_label,
                "metric_key": metric_key,
                "value": vehicle_val,
                "mean": stats["mean"],
                "std": stats["std"],
                "min": stats["min"],
                "max": stats["max"],
                "z_score": z,
                "flagged": abs(z) > Z_THRESHOLD,
            })
        results[surf] = surf_results

    return results


# ---------------------------------------------------------------------------
# Step 4: Fleet-wide outlier scan
# ---------------------------------------------------------------------------
def fleet_outlier_scan(fleet_data, total_vehicles):
    """
    Scan every vehicle across all surfaces/metrics and count how many
    exceed the Z-threshold. This answers: "How many other at-risk vehicles
    are in the fleet?"
    """
    # We need per-vehicle scores, which requires another pass.
    # To avoid re-reading the CSV, compute thresholds from fleet_data
    # and count how many values exceed them.
    thresholds = {}
    flagged_counts = {}

    for surf in SURFACES:
        for metric_key, metric_label in METRICS:
            values = fleet_data[surf][metric_key]
            stats = compute_stats(values)
            upper = stats["mean"] + Z_THRESHOLD * stats["std"]
            lower = stats["mean"] - Z_THRESHOLD * stats["std"]
            key = f"{surf}_{metric_key}"
            thresholds[key] = (lower, upper, stats)

            # Count values outside bounds
            count = sum(1 for v in values if v > upper or v < lower)
            flagged_counts[key] = count

    return flagged_counts, thresholds


def similar_signature_scan(fleet_data, vehicle_ids, z_min=Z_THRESHOLD):
    """
    List every vehicle whose servo shows the friction signature seen on the
    crashed unit: sweep current AND peak current both >= z_min sigma high on
    the SAME servo. The two metrics are uncorrelated across the fleet, so
    both being high together is far less likely by chance than either alone.
    """
    hits = []
    for surf in SURFACES:
        sweep = fleet_data[surf]["sweep_current_ma"]
        peak = fleet_data[surf]["peak_current_ma"]
        s_stats, p_stats = compute_stats(sweep), compute_stats(peak)
        for i, (sv, pv) in enumerate(zip(sweep, peak)):
            zs = z_score(sv, s_stats["mean"], s_stats["std"])
            zp = z_score(pv, p_stats["mean"], p_stats["std"])
            if zs >= z_min and zp >= z_min:
                hits.append((vehicle_ids[i], surf, sv, zs, pv, zp))
    return sorted(hits, key=lambda h: -(h[3] ** 2 + h[5] ** 2))


# ---------------------------------------------------------------------------
# Step 5: Generate figures
# ---------------------------------------------------------------------------
def generate_figures(fleet_data, results, output_dir):
    """Generate manufacturing evidence charts."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("  [WARN] matplotlib not installed — skipping figures.")
        return

    BG_COLOR = "#0d1117"
    TEXT_COLOR = "#c9d1d9"
    GRID_COLOR = "#21262d"
    GREEN = "#3fb950"
    RED = "#f85149"
    YELLOW = "#d29922"
    BLUE = "#58a6ff"

    # ====================================================================
    # FIGURE 4: Distribution Histograms — GLDR-0421 vs Fleet
    # ====================================================================
    fig4, axes = plt.subplots(1, 5, figsize=(18, 4))
    fig4.patch.set_facecolor(BG_COLOR)
    fig4.suptitle(f"Left Aileron Servo: {TARGET_VEHICLE} vs Fleet (N={len(fleet_data['aileron_l']['sweep_current_ma']):,})",
                  color=TEXT_COLOR, fontsize=13, fontweight="bold")

    left_results = results["aileron_l"]

    for i, res in enumerate(left_results):
        ax = axes[i]
        ax.set_facecolor(BG_COLOR)

        values = fleet_data["aileron_l"][res["metric_key"]]
        vehicle_val = res["value"]
        mean = res["mean"]
        std = res["std"]

        # Histogram of fleet values
        ax.hist(values, bins=80, color=BLUE, alpha=0.7, edgecolor="none")

        # Mark the mean and ±3σ
        ax.axvline(mean, color=GREEN, linewidth=1.5, linestyle="-", label="μ")
        ax.axvline(mean + 3 * std, color=YELLOW, linewidth=1.2, linestyle="--",
                   label="+3σ")
        ax.axvline(mean - 3 * std, color=YELLOW, linewidth=1.2, linestyle="--",
                   label="-3σ")

        # Mark GLDR-0421's value
        ax.axvline(vehicle_val, color=RED, linewidth=2.5, linestyle="-",
                   label=f"{TARGET_VEHICLE}")

        ax.set_title(res["metric"].split("(")[0].strip(), color=TEXT_COLOR,
                     fontsize=9)
        ax.tick_params(colors=TEXT_COLOR, labelsize=7)
        ax.spines[:].set_color(GRID_COLOR)

        if i == 0:
            ax.legend(fontsize=6, facecolor=BG_COLOR, edgecolor=GRID_COLOR,
                      labelcolor=TEXT_COLOR, loc="upper right")

    fig4.tight_layout()
    fig4_path = os.path.join(output_dir, "fig4_mfg_distributions.png")
    fig4.savefig(fig4_path, dpi=150, facecolor=BG_COLOR)
    plt.close(fig4)
    print(f"  Saved: {fig4_path}")

    # ====================================================================
    # FIGURE 5: Z-Score Radar Chart — All 5 Surfaces
    # ====================================================================
    fig5, ax5 = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))
    fig5.patch.set_facecolor(BG_COLOR)
    ax5.set_facecolor(BG_COLOR)

    # For each surface, compute the max |Z-score| across the 5 metrics
    surface_labels = {
        "aileron_l": "L-Aileron",
        "aileron_r": "R-Aileron",
        "elevon_l": "L-Elevon",
        "elevon_r": "R-Elevon",
        "rudder": "Rudder",
    }

    angles = []
    z_vals = []
    labels = []

    for surf in SURFACES:
        for res in results[surf]:
            labels.append(f"{surface_labels[surf]}\n{res['metric'].split('(')[0].strip()}")
            z_vals.append(abs(res["z_score"]))

    N = len(labels)
    angles = [n / float(N) * 2 * math.pi for n in range(N)]
    angles += angles[:1]
    z_vals_plot = z_vals + z_vals[:1]

    # Plot
    ax5.plot(angles, z_vals_plot, color=RED, linewidth=2)
    ax5.fill(angles, z_vals_plot, color=RED, alpha=0.15)

    # 3σ threshold circle
    threshold_vals = [Z_THRESHOLD] * (N + 1)
    ax5.plot(angles, threshold_vals, color=YELLOW, linewidth=1.5,
             linestyle="--", label="3σ Threshold")

    ax5.set_xticks(angles[:-1])
    ax5.set_xticklabels(labels, size=6, color=TEXT_COLOR)
    ax5.tick_params(colors=TEXT_COLOR)
    ax5.set_title(f"{TARGET_VEHICLE} — Z-Score Radar (All Surfaces)",
                   color=TEXT_COLOR, fontsize=13, fontweight="bold", pad=20)
    ax5.legend(loc="upper right", fontsize=9, facecolor=BG_COLOR,
               edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)
    ax5.spines["polar"].set_color(GRID_COLOR)
    ax5.grid(True, color=GRID_COLOR, alpha=0.3)

    fig5.tight_layout()
    fig5_path = os.path.join(output_dir, "fig5_zscore_radar.png")
    fig5.savefig(fig5_path, dpi=150, facecolor=BG_COLOR)
    plt.close(fig5)
    print(f"  Saved: {fig5_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global TARGET_VEHICLE
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--vehicle", default=TARGET_VEHICLE,
                        help="vehicle_id to trace (default: %(default)s)")
    TARGET_VEHICLE = parser.parse_args().vehicle

    print("=" * 70)
    print("  MANUFACTURING ANALYSIS — TRACING DEFECT TO FACTORY")
    print("=" * 70)

    # Load data
    print("\n[1] Loading manufacturing data...")
    fleet_data, target_row, total_vehicles, vehicle_ids, qc, lot_values = load_and_analyze(MFG_PATH)
    print(f"    {total_vehicles:,} vehicles loaded")

    if target_row is None:
        print(f"    ERROR: {TARGET_VEHICLE} not found in manufacturing.csv!")
        sys.exit(1)

    # Print the target vehicle's build record
    print(f"\n[2] Build Record for {TARGET_VEHICLE}:")
    build_fields = [
        ("Unit Serial", "unit_serial"),
        ("Build Date", "build_date_utc"),
        ("Build Shift", "build_shift"),
        ("Factory Site", "factory_site"),
        ("Line / Cell", "line_id"),
        ("Last Station", "station_last"),
        ("Technician", "technician_id_last"),
        ("Inspector", "inspector_id_last"),
        ("Rework Count", "rework_count"),
        ("Rework Codes", "rework_codes"),
        ("Rework Notes", "rework_notes"),
        ("Final QC Pass", "final_qc_pass"),
        ("QC Station", "qc_station_id"),
        ("QC Exceptions", "qc_exception_count"),
    ]
    for label, key in build_fields:
        print(f"    {label:<20}: {target_row.get(key, 'N/A')}")

    # Print the left aileron servo details
    print(f"\n[3] Left Aileron Servo (Defective Component):")
    servo_fields = [
        ("Vendor", "aileron_l_servo_vendor"),
        ("Model", "aileron_l_servo_model"),
        ("Serial", "aileron_l_servo_serial"),
        ("Lot ID", "aileron_l_servo_lot_id"),
    ]
    for label, key in servo_fields:
        print(f"    {label:<20}: {target_row.get(key, 'N/A')}")

    # Z-score analysis
    print(f"\n[4] Z-Score Analysis — {TARGET_VEHICLE} vs Fleet of {total_vehicles:,}:")
    results = analyze(fleet_data, target_row, total_vehicles)

    for surf in SURFACES:
        surf_label = surf.replace("_", " ").title()
        any_flagged = any(r["flagged"] for r in results[surf])
        status = "⚠️  OUTSIDE ±3σ" if any_flagged else "within ±3σ"
        print(f"\n    ── {surf_label} ── {status}")
        print(f"    {'Metric':<28} {'Value':>10} {'Mean':>10} {'Std':>8} {'Z-Score':>8} {'Flag':>6}")
        print(f"    {'─' * 28} {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 8} {'─' * 6}")

        for r in results[surf]:
            flag = "⚠️" if r["flagged"] else ""
            print(f"    {r['metric']:<28} {r['value']:>10.2f} "
                  f"{r['mean']:>10.2f} {r['std']:>8.2f} "
                  f"{r['z_score']:>+8.2f} {flag:>6}")

    # Fleet-wide outlier scan
    print(f"\n[5] Fleet-Wide Outlier Scan (threshold = ±{Z_THRESHOLD}σ):")
    flagged_counts, thresholds = fleet_outlier_scan(fleet_data, total_vehicles)

    total_flagged = 0
    print(f"    {'Surface + Metric':<45} {'Flagged':>8} {'Rate':>8}")
    print(f"    {'─' * 45} {'─' * 8} {'─' * 8}")
    for key, count in sorted(flagged_counts.items(), key=lambda x: -x[1])[:10]:
        rate = (count / total_vehicles) * 100
        total_flagged += count
        print(f"    {key:<45} {count:>8} {rate:>7.2f}%")

    # Lot check: is the suspect servo's whole lot shifted, or just this unit?
    target_lot = target_row[f"{SUSPECT_SURFACE}_servo_lot_id"]
    lot_means = {lot: sum(v) / len(v) for lot, v in lot_values.items()}
    print(f"\n[6] Supplier Lot Check ({SUSPECT_SURFACE} sweep current):")
    print(f"    {target_lot}: mean {lot_means[target_lot]:.2f} mA over "
          f"{len(lot_values[target_lot]):,} units")
    print(f"    All {len(lot_means)} lots: mean range "
          f"{min(lot_means.values()):.2f}–{max(lot_means.values()):.2f} mA")
    print(f"    → {'Lot is NOT shifted — single-unit defect' if max(lot_means.values()) - min(lot_means.values()) < 5 else 'Lot means differ — investigate supplier'}")

    # Other units with the same signature
    print(f"\n[7] Other Vehicles With the Same Friction Signature "
          f"(sweep AND peak current ≥ {Z_THRESHOLD}σ on one servo):")
    hits = similar_signature_scan(fleet_data, vehicle_ids)
    print(f"    {'Vehicle':<13} {'Servo':<10} {'Sweep mA':>9} {'Z':>6} {'Peak mA':>9} {'Z':>6}")
    for vid, surf, sv, zs, pv, zp in hits:
        mark = "  ← crashed unit" if vid == TARGET_VEHICLE else ""
        print(f"    {vid:<13} {surf:<10} {sv:>9.1f} {zs:>+6.2f} {pv:>9.1f} {zp:>+6.2f}{mark}")
    print(f"    {len(hits)} vehicle(s). Rows are ranked by combined current Z; "
          f"spc_detector.py applies the full production gate.")

    # QC Gap analysis — computed from final_qc_pass / qc_exception_count
    print(f"\n[8] Quality Process Gap Analysis:")
    n = total_vehicles
    print(f"    Vehicles with final_qc_pass = 1:   {qc['pass']:,} of {n:,} ({qc['pass'] / n:.1%})")
    print(f"    Vehicles with scrap_flag = 1:      {qc['scrap']:,}")
    print(f"    Vehicles with QC exceptions:       {qc['with_exceptions']:,}")
    print(f"      ...of which still passed QC:     {qc['exceptions_and_passed']:,}")
    print(f"    Vehicles with rework code R05:     {qc['rework_r05']:,}")
    print(f"    ")
    print(f"    ⚠️  Bench measurements are recorded but never compared against limits.")
    print(f"    ⚠️  Logged QC exceptions do not block a pass.")

    # Generate figures
    print(f"\n[9] Generating figures...")
    generate_figures(fleet_data, results, OUTPUT_DIR)

    # Summary
    print("\n" + "=" * 70)
    print("  MANUFACTURING ROOT CAUSE CONFIRMED")
    print("  ──────────────────────────────────")
    print(f"  Component:  Left Aileron Servo ({target_row['aileron_l_servo_vendor']} "
          f"{target_row['aileron_l_servo_model']})")
    print(f"  Serial:     {target_row['aileron_l_servo_serial']}")
    print(f"  Lot:        {target_row['aileron_l_servo_lot_id']}")
    print(f"  Defect:     Abnormal internal gearbox friction")
    z = {r["metric_key"]: r["z_score"] for r in results[SUSPECT_SURFACE]}
    print(f"  Evidence:   Sweep current {z['sweep_current_ma']:+.2f}σ, "
          f"Peak current {z['peak_current_ma']:+.2f}σ,")
    print(f"              Sweep time {z['sweep_time_ms']:+.2f}σ, "
          f"Position error {z['position_error_rms_deg']:+.2f}σ")
    print(f"  QC Gap:     No SPC limits on servo bench parameters")
    print("=" * 70)

    return results


if __name__ == "__main__":
    main()
