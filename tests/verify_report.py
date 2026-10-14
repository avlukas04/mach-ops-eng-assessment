"""
Independent verification of REPORT.md
=====================================
Recomputes every key number the report states, directly from the raw CSVs,
with code written separately from the analysis scripts (pandas, full 500 Hz
telemetry, no shared functions). Each claim is printed with the report's
value, the recomputed value, and OK / DIFF.

This is the "second pair of eyes": if an analysis script has a bug, this
script is unlikely to share it.

Run from the repository root (about 1 min):
  python3 tests/verify_report.py
Exit code 1 if any claim differs from the data.
"""

import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = []


def check(section, claim, reported, actual, tol=0.0):
    """Record one claim. Numbers match if |reported - actual| <= tol;
    anything else must be equal."""
    if isinstance(reported, (int, float)) and isinstance(actual, (int, float, np.number)):
        ok = abs(reported - float(actual)) <= tol + 1e-9
    else:
        ok = reported == actual
    CHECKS.append((section, claim, reported, actual, ok))


def fmt(v):
    if isinstance(v, (float, np.floating)):
        return f"{v:,.4g}" if abs(v) < 1e4 else f"{v:,.0f}"
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    return str(v)


# ---------------------------------------------------------------------------
# Flight (telemetry.csv at full 500 Hz, flight_plan.csv)
# ---------------------------------------------------------------------------
def verify_flight():
    cols = ["time_s", "alt_agl_m", "pos_n_m", "pos_e_m", "vehicle_state", "control_mode",
            "failsafe_reason", "event_marker", "roll_deg", "roll_setpoint_deg",
            "controller_saturation_roll", "battery_voltage_v", "battery_soc_pct",
            "servo_bus_voltage_v", "brownout_flag", "airspeed_mps", "wind_est_speed_mps",
            "outside_temp_c", "accel_z_filt_mps2", "gps_fix_type", "gps_sats", "gps_hdop",
            "attitude_valid", "servo_aileron_left_current_a", "flight_id"]
    surfaces = ["aileron_left", "aileron_right", "elevon_left", "elevon_right", "rudder"]
    for s in surfaces:
        cols += [f"{s}_cmd_deg", f"{s}_actual_deg"]
    t = pd.read_csv(os.path.join(ROOT, "telemetry.csv"), usecols=cols, low_memory=False)
    fp = pd.read_csv(os.path.join(ROOT, "flight_plan.csv"))
    T = t["time_s"].to_numpy()

    check("Data", "one flight in telemetry", 1, t["flight_id"].nunique())
    check("Data", "telemetry rows", 600_000, len(t))

    # Timeline
    check("3.1", "release altitude AGL ≈ 1,510 m", 1510, t["alt_agl_m"].iloc[0], tol=10)
    states = t["vehicle_state"]
    changes = t.loc[states.ne(states.shift()), ["time_s", "vehicle_state"]]
    seq = list(zip(changes["time_s"].round(0), changes["vehicle_state"]))
    check("3.1", "state sequence", [(0, "DROP"), (12, "GLIDE"), (154, "BANK_PATTERN"),
                                    (1165, "GLIDE"), (1180, "EMERGENCY")], seq)
    fs = t.loc[t["control_mode"] != "AUTO_STABILIZE"]
    check("3.1", "FAILSAFE start (s)", 1182, fs["time_s"].iloc[0], tol=0.5)
    check("3.1", "FAILSAFE reason", "NAV_PATH_DEVIATION", fs["failsafe_reason"].iloc[0])
    impact = t.loc[t["event_marker"] == "IMPACT_DETECTED", "time_s"]
    check("2", "impact time, IMPACT_DETECTED marker (s)", 1198.9, impact.iloc[0], tol=0.05)

    # Waypoints: closest 3D distance within ±3 s of target time
    P = t[["pos_n_m", "pos_e_m", "alt_agl_m"]].to_numpy()
    miss = []
    for _, w in fp.iterrows():
        m = np.abs(T - w["target_time_s"]) <= 3.0
        tgt = np.array([w["target_pos_n_m"], w["target_pos_e_m"], w["target_alt_agl_m"]])
        miss.append(np.linalg.norm(P[m] - tgt, axis=1).min())
    miss = np.array(miss)
    check("2", "WP-01 miss (m)", 1.21, miss[0], tol=0.05)
    check("2", "WP-02 miss (m)", 1.30, miss[1], tol=0.05)
    check("2", "WP-03 miss (m)", 109.2, miss[2], tol=0.2)
    check("2", "WP-21 miss (m)", 5146, miss[-1], tol=1)
    check("2", "waypoints hit (≤ 75 m)", 2, int((miss <= 75).sum()))
    check("2", "matches flight_plan.csv (max diff, m)", 0.0,
          np.abs(miss - fp["closest_approach_actual_m"].to_numpy()).max(), tol=0.05)

    # Control surface screen
    gaps = {s: (t[f"{s}_cmd_deg"] - t[f"{s}_actual_deg"]).abs().to_numpy() for s in surfaces}
    al = gaps["aileron_left"]
    check("3.2", "left aileron max gap (°)", 27.8, al.max(), tol=0.05)
    check("3.2", "left aileron first >5° (s)", 246.1, T[np.argmax(al > 5)], tol=0.05)
    check("3.2", "left aileron samples >5°", 24_592, int((al > 5).sum()))
    check("3.2", "right aileron samples >5°", 13, int((gaps["aileron_right"] > 5).sum()))
    check("3.2", "right aileron first >5° (s)", 1165.0, T[np.argmax(gaps["aileron_right"] > 5)], tol=0.05)
    for s in ["elevon_left", "elevon_right", "rudder"]:
        check("3.2", f"{s} never >5°", 0, int((gaps[s] > 5).sum()))

    # Primary seizure window, from 500 Hz data, merging gaps < 0.5 s
    over = np.flatnonzero(al > 5)
    breaks = np.flatnonzero(np.diff(T[over]) > 0.5)
    first_end = over[breaks[0]] if breaks.size else over[-1]
    start, end = T[over[0]], T[first_end]
    check("3.1", "primary seizure start (s)", 246.1, start, tol=0.05)
    check("3.1", "primary seizure end (s)", 255.1, end, tol=0.1)
    check("3.1", "primary seizure duration (s)", 9.0, end - start, tol=0.1)
    win = (T >= start) & (T <= end)
    check("3.1", "roll saturated share of window (%)", 64,
          100 * t.loc[win, "controller_saturation_roll"].mean(), tol=1.5)
    check("3.4", "right aileron max gap in window (°)", 1.3, gaps["aileron_right"][win].max(), tol=0.05)
    stuck = win & (T <= T[np.argmax(al)])
    check("3.3", "max roll before breakaway (°)", 6.8, t.loc[stuck, "roll_deg"].max(), tol=0.15)
    check("3.3", "max roll setpoint before breakaway (°)", 18.3,
          t.loc[stuck, "roll_setpoint_deg"].max(), tol=0.15)
    act = t["aileron_left_actual_deg"].to_numpy()
    cmd = t["aileron_left_cmd_deg"].to_numpy()
    i = np.argmax(al)
    before = win & (T <= T[i])
    check("3.1", "stuck: most negative position (°)", -11, act[before].min(), tol=0.5)
    check("3.1", "stuck: most positive after first 1 s (°)", 0, act[before & (T >= start + 1)].max(), tol=1.0)
    free = np.flatnonzero((T > T[i]) & (act >= cmd - 1.0))[0]
    last = np.flatnonzero((T > T[i]) & (T < T[free]) & (act <= -9.0))[-1]
    check("3.1", "breakaway from (°)", -9.6, act[last], tol=0.1)
    check("3.1", "breakaway to (°)", 16.6, act[free], tol=0.1)
    check("3.1", "breakaway duration (s)", 0.1, T[free] - T[last], tol=0.02)
    starts = np.r_[over[0], over[breaks + 1]]
    ends = np.r_[over[breaks], over[-1]]
    durs = T[ends] - T[starts]
    later = durs[1:] >= 0.5
    check("3.1", "later episodes lasting ≥ 0.5 s", 11, int(later.sum()))
    check("8", "later segments before the 0.5 s rule", 14, int(len(durs) - 1))
    peaks = [al[a:b + 1].max() for a, b in zip(starts[1:][later], ends[1:][later])]
    check("3.1", "later episodes: largest gap (°)", 18, max(peaks), tol=0.5)
    check("3.1", "later episodes: longest (s)", 8, durs[1:][later].max(), tol=0.25)

    # Rule-outs (anomaly window and 200–300 s environment)
    check("3.4", "servo bus min in window (V)", 12.26, t.loc[win, "servo_bus_voltage_v"].min(), tol=0.01)
    check("3.4", "brownout in window", 0, int(t.loc[win, "brownout_flag"].max()))
    check("RCA", "battery min during event (V)", 12.21, t.loc[win, "battery_voltage_v"].min(), tol=0.01)
    check("RCA", "battery max during event (V)", 12.48, t.loc[win, "battery_voltage_v"].max(), tol=0.01)
    check("RCA", "SOC min during event (%)", 94, t.loc[win, "battery_soc_pct"].min(), tol=0.5)
    env = (T >= 200) & (T <= 300)
    check("3.4", "wind min 200–300 s (m/s)", 0.9, t.loc[env, "wind_est_speed_mps"].min(), tol=0.05)
    check("3.4", "wind max 200–300 s (m/s)", 3.7, t.loc[env, "wind_est_speed_mps"].max(), tol=0.05)
    check("3.4", "temp min 200–300 s (°C)", 7.6, t.loc[env, "outside_temp_c"].min(), tol=0.05)
    check("3.4", "temp max 200–300 s (°C)", 12.0, t.loc[env, "outside_temp_c"].max(), tol=0.05)
    check("3.4", "vertical accel min (m/s²)", 8.6, t.loc[env, "accel_z_filt_mps2"].min(), tol=0.05)
    check("3.4", "vertical accel max (m/s²)", 10.3, t.loc[env, "accel_z_filt_mps2"].max(), tol=0.05)
    check("3.4", "GPS fix 3 throughout 200–300 s", 3, int(t.loc[env, "gps_fix_type"].min()))
    check("3.4", "GPS sats min", 6, int(t.loc[env, "gps_sats"].min()))
    check("3.4", "GPS sats max", 15, int(t.loc[env, "gps_sats"].max()))
    check("3.4", "HDOP max", 1.01, t.loc[env, "gps_hdop"].max(), tol=0.005)
    check("3.4", "attitude_valid = 1 throughout 200–300 s", 1, int(t.loc[env, "attitude_valid"].min()))
    check("3.4", "mode in window is AUTO_STABILIZE", ["AUTO_STABILIZE"],
          sorted(t.loc[win, "control_mode"].unique()))
    pre = (T >= 200) & (T < start)
    check("3.4", "left servo current before event (A)", 0.34,
          t.loc[pre, "servo_aileron_left_current_a"].mean(), tol=0.01)
    check("3.4", "left servo current during event (A)", 0.39,
          t.loc[win, "servo_aileron_left_current_a"].mean(), tol=0.01)
    check("RCA", "airspeed min during event (m/s)", 25, t.loc[win, "airspeed_mps"].min(), tol=0.5)
    check("RCA", "airspeed max during event (m/s)", 29, t.loc[win, "airspeed_mps"].max(), tol=0.5)
    err = (t["roll_deg"] - t["roll_setpoint_deg"]).abs().to_numpy()
    reversal = T[np.flatnonzero((T >= 200) & (err > 0.5))[0]]
    check("3.1", "bank reversal begins (s)", 245.7, reversal, tol=0.05)
    check("3.4", "roll error 200 s → reversal below 0.1°", True,
          bool(err[(T >= 200) & (T < reversal - 0.02)].max() < 0.1))
    check("3.1", "left aileron gap at 246.0 s (°)", 4, al[np.abs(T - 246.0).argmin()], tol=0.5)


# ---------------------------------------------------------------------------
# Manufacturing (manufacturing.csv)
# ---------------------------------------------------------------------------
def verify_manufacturing():
    df = pd.read_csv(os.path.join(ROOT, "manufacturing.csv"), low_memory=False)
    servos = ["elevon_l", "elevon_r", "aileron_l", "aileron_r", "rudder"]
    metrics = ["sweep_current_ma", "peak_current_ma", "sweep_time_ms",
               "position_error_rms_deg", "backdrive_torque_nmm"]
    v = df.set_index("vehicle_id")
    t421 = v.loc["GLDR-0421"]

    check("4.1", "vehicles", 200_000, len(df))
    check("4.1", "GLDR-0421 serial", "SN-12661455", t421["unit_serial"])
    check("4.1", "GLDR-0421 site / line / shift", ("HB-PLANT-1", "LN3", "B"),
          (t421["factory_site"], t421["line_id"], t421["build_shift"]))
    check("4.1", "left aileron servo serial", "SV-65031081", t421["aileron_l_servo_serial"])
    check("4.1", "left aileron servo lot", "L2026-07B", t421["aileron_l_servo_lot_id"])
    check("4.1", "left aileron servo model", "ActuMax SM-14",
          f"{t421['aileron_l_servo_vendor']} {t421['aileron_l_servo_model']}")
    check("4.1", "rework codes", "R05", t421["rework_codes"])

    # Classical z (mean / std) and rank
    expect = {"sweep_current_ma": (5.93, 1), "peak_current_ma": (4.05, 1), "sweep_time_ms": (2.92, 340),
              "position_error_rms_deg": (2.98, 396), "backdrive_torque_nmm": (2.35, 2068)}
    for m, (z_rep, rank_rep) in expect.items():
        c = df[f"aileron_l_servo_{m}"]
        z = (t421[f"aileron_l_servo_{m}"] - c.mean()) / c.std()
        check("4.3", f"left aileron {m} z", z_rep, z, tol=0.005)
        check("4.3", f"left aileron {m} rank", rank_rep, int((c > t421[f"aileron_l_servo_{m}"]).sum()) + 1)
    c = df["aileron_l_servo_sweep_current_ma"]
    check("1", "sweep current fleet mean (mA)", 185.1, c.mean(), tol=0.05)
    p = 0.5 * math.erfc(((t421["aileron_l_servo_sweep_current_ma"] - c.mean()) / c.std()) / math.sqrt(2))
    check("1", "1 in N odds (millions)", 650, 1 / p / 1e6, tol=25)

    other_max = max(abs((t421[f"{s}_servo_{m}"] - df[f"{s}_servo_{m}"].mean()) / df[f"{s}_servo_{m}"].std())
                    for s in servos if s != "aileron_l" for m in metrics)
    check("4.3", "other 4 servos max |z| < 2.6", True, bool(other_max < 2.6))

    lots = df.groupby("aileron_l_servo_lot_id")["aileron_l_servo_sweep_current_ma"].mean()
    check("4.4", "servo lots", 21, len(lots))
    check("4.4", "lot mean min (mA)", 184.87, lots.min(), tol=0.01)
    check("4.4", "lot mean max (mA)", 185.39, lots.max(), tol=0.01)
    check("4.4", "L2026-07B mean (mA)", 185.26, lots["L2026-07B"], tol=0.01)
    over300 = sorted(df.loc[(df[[f"{s}_servo_sweep_current_ma" for s in servos]] > 300).any(axis=1), "vehicle_id"])
    check("4.4", "vehicles with any servo > 300 mA", ["GLDR-0421", "GLDR-200000"], over300)
    g2 = v.loc["GLDR-200000"]
    check("4.4", "GLDR-200000 sweep current (mA)", 304.2, g2["aileron_l_servo_sweep_current_ma"], tol=0.05)
    check("4.4", "GLDR-200000 peak current (mA)", 367.7, g2["peak_current_ma" if False else "aileron_l_servo_peak_current_ma"], tol=0.05)
    check("4.4", "GLDR-200000 site / lot", ("OC-PLANT-2", "L2026-02B"),
          (g2["factory_site"], g2["aileron_l_servo_lot_id"]))

    # Same-signature scan: sweep and peak both >= 3 sigma on one servo
    hits = set()
    for s in servos:
        zs = (df[f"{s}_servo_sweep_current_ma"] - df[f"{s}_servo_sweep_current_ma"].mean()) / df[f"{s}_servo_sweep_current_ma"].std()
        zp = (df[f"{s}_servo_peak_current_ma"] - df[f"{s}_servo_peak_current_ma"].mean()) / df[f"{s}_servo_peak_current_ma"].std()
        hits |= set(df.loc[(zs >= 3) & (zp >= 3), "vehicle_id"])
    check("4.4", "same-signature vehicles", 10, len(hits))

    # QC gap
    check("5", "final_qc_pass = 1 share", 1.0, df["final_qc_pass"].mean())
    check("5", "scrapped", 0, int(df["scrap_flag"].sum()))
    check("5", "vehicles with QC exceptions", 32_296, int((df["qc_exception_count"] > 0).sum()))
    check("5", "R05 rework vehicles", 9_571, int(df["rework_codes"].fillna("").str.contains("R05").sum()))
    check("4.4", "hinge torque (N·mm)", 37.07, t421["hinge_torque_nmm"], tol=0.005)
    check("4.4", "linkage freeplay (mm)", 0.276, t421["control_linkage_freeplay_mm"], tol=0.0005)
    check("4.4", "mount screw torque (N·m)", 0.692, t421["servo_mount_screw_torque_nm"], tol=0.0005)
    check("6", "IMU cal WARN", 15_876, int((df["imu_cal_status"] == "WARN").sum()))
    check("6", "mag cal WARN", 12_045, int((df["mag_cal_status"] == "WARN").sum()))
    check("6", "strain relief not OK", 5_987, int((df["harness_strain_relief_ok"] == 0).sum()))

    # Independence assumption behind D
    X = df[[f"{s}_servo_{m}" for s in servos for m in metrics]]
    r = 0.0
    for s in servos:
        cm = X[[f"{s}_servo_{m}" for m in metrics]].corr().to_numpy()
        r = max(r, np.abs(cm[~np.eye(len(metrics), dtype=bool)]).max())
    check("6.1", "max |r| between a servo's 5 metrics", 0.01, r, tol=0.005)

    # Two-tier gate, recomputed from scratch (robust high-side z, D)
    Z = np.zeros((len(df), 5, 5))
    sat = np.zeros(len(df), bool)
    for a, s in enumerate(servos):
        for b, m in enumerate(metrics):
            x = df[f"{s}_servo_{m}"].to_numpy()
            q1, med, q3 = np.percentile(x, [25, 50, 75])
            Z[:, a, b] = np.maximum(0, (x - med) / ((q3 - q1) / 1.349))
            vals, counts = np.unique(x, return_counts=True)
            piled = vals[counts >= 10]
            if piled.size and piled.max() >= np.percentile(x, 99) and (x > piled.max()).sum() < 10:
                sat |= x >= piled.max()
    D = np.sqrt((Z ** 2).sum(axis=2))
    quarantine = (D >= 6.0).any(axis=1) | (Z >= 5.5).any(axis=(1, 2))
    flag = ~quarantine & ((Z >= 3).any(axis=(1, 2)) | (D >= 4.5).any(axis=1) | sat)
    ids = df["vehicle_id"].to_numpy()
    check("6.1", "QUARANTINE vehicles", ["GLDR-0421", "GLDR-200000"], sorted(ids[quarantine]))
    check("6.1", "FLAG vehicles", 6_498, int(flag.sum()))
    check("6.1", "PASS vehicles", 193_500, int((~quarantine & ~flag).sum()))
    check("6.1", "vehicles with any z ≥ 3", 6_484, int((Z >= 3).any(axis=(1, 2)).sum()))
    check("6.1", "saturated vehicles", 1_028, int(sat.sum()))
    i421, i2 = np.flatnonzero(ids == "GLDR-0421")[0], np.flatnonzero(ids == "GLDR-200000")[0]
    check("6.1", "GLDR-0421 D", 8.57, D[i421].max(), tol=0.005)
    check("6.1", "GLDR-200000 D", 7.31, D[i2].max(), tol=0.005)
    rest = np.sort(D[~quarantine].ravel())
    check("6.1", "next-highest servo D", 5.80, rest[-1], tol=0.005)
    check("6.1", "chance FLAG count for 25 tests", 6640,
          len(df) * (1 - (1 - 0.5 * math.erfc(3 / math.sqrt(2))) ** 25), tol=25)


def main():
    print("Verifying REPORT.md against raw data (about 1 min)...\n")
    verify_flight()
    verify_manufacturing()
    width = max(len(c[1]) for c in CHECKS)
    fails = 0
    for section, claim, rep, act, ok in CHECKS:
        fails += not ok
        print(f"  {'OK  ' if ok else 'DIFF'}  §{section:<4} {claim:<{width}}  report {fmt(rep):>14}   data {fmt(act)}")
    print(f"\n{len(CHECKS) - fails} of {len(CHECKS)} claims match the data; {fails} differ.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
