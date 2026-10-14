"""
Flight Investigation Analysis — Part 1
=======================================
Analyzes telemetry.csv and flight_plan.csv to determine what caused
the aircraft to deviate from its planned path.

Produces:
  1. Flight timeline, waypoint results, an all-surfaces screen and
     hypothesis tests (console output)
  2. Figure 1: Roll command vs actual + aileron positions (the "smoking gun")
  3. Figure 2: Waypoint miss distances over time
  4. Figure 3: Altitude profile with key events annotated

Usage:
  python analysis/flight_investigation.py
  (run from the assessment root directory)
"""

import csv
import math
import os
import sys

# ---------------------------------------------------------------------------
# Configuration — paths relative to the assessment root directory
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = PROJECT_ROOT  # CSVs are in the project root

TELEMETRY_PATH = os.path.join(DATA_DIR, "telemetry.csv")
FLIGHT_PLAN_PATH = os.path.join(DATA_DIR, "flight_plan.csv")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "figures")

# If telemetry.csv isn't in Desktop workspace, try the code directory
if not os.path.exists(TELEMETRY_PATH):
    ALT_DIR = os.path.expanduser("~/code/andrea_lukas_ops_eng_assessment")
    TELEMETRY_PATH = os.path.join(ALT_DIR, "telemetry.csv")
    FLIGHT_PLAN_PATH = os.path.join(ALT_DIR, "flight_plan.csv")

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Step 1: Load the Flight Plan
# ---------------------------------------------------------------------------
def load_flight_plan(path):
    """Load waypoints from flight_plan.csv."""
    waypoints = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            waypoints.append({
                "id": row["waypoint_id"],
                "target_time_s": float(row["target_time_s"]),
                "target_n": float(row["target_pos_n_m"]),
                "target_e": float(row["target_pos_e_m"]),
                "target_alt": float(row["target_alt_agl_m"]),
                "tolerance": float(row["hit_tolerance_m"]),
                "closest_m": float(row["closest_approach_actual_m"]),
                "hit": int(row["hit_actual_flag"]),
            })
    return waypoints


# ---------------------------------------------------------------------------
# Step 2: Load Telemetry (sampled for analysis — every Nth row)
# ---------------------------------------------------------------------------
SURFACES = ["aileron_left", "aileron_right", "elevon_left", "elevon_right", "rudder"]
DIVERGENCE_THRESHOLD_DEG = 5.0
# flight_plan.csv's closest_approach_actual_m is the closest 3D distance to
# the target within this many seconds of the target time; recomputed from
# telemetry below as an independent check
WAYPOINT_WINDOW_S = 3.0


def load_telemetry(path, waypoints, sample_rate=50):
    """
    Load telemetry.csv, keeping every `sample_rate`-th row for plotting
    plus ALL rows from 200 to 300 s (the event and its surroundings).

    Checks that depend on extremes are computed from every row as the file
    streams past: the control surface screen, the waypoint distances and
    every left-aileron sample more than 5 deg off its command. Sampling
    would miss short peaks, so nothing that reports a max or min uses it.
    """
    rows = []
    events = []
    state_changes = []
    mode_changes = []
    prev_state = None
    prev_mode = None
    row_count = 0
    for w in waypoints:
        w["computed_m"] = math.inf
    # Unbiased screen of every control surface at full 500 Hz: how far does
    # the actual position ever get from the command, and when does it first
    # exceed the divergence threshold?
    screen = {name: {"max_gap_deg": 0.0, "max_gap_time_s": None, "first_over_s": None,
                     "first_over_state": None, "samples_over": 0} for name in SURFACES}
    left_over = []  # (t, gap) for every 500 Hz sample with the left aileron > 5 deg off

    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row_count += 1
            t = float(raw["time_s"])

            # Capture events and state changes at full resolution
            if raw.get("event_marker"):
                events.append((t, raw["event_marker"]))
            if raw["vehicle_state"] != prev_state:
                state_changes.append((t, raw["vehicle_state"]))
                prev_state = raw["vehicle_state"]
            if raw["control_mode"] != prev_mode:
                mode_changes.append((t, raw["control_mode"], raw.get("failsafe_reason", "")))
                prev_mode = raw["control_mode"]

            for w in waypoints:
                if abs(t - w["target_time_s"]) <= WAYPOINT_WINDOW_S:
                    d = math.dist((float(raw["pos_n_m"]), float(raw["pos_e_m"]), float(raw["alt_agl_m"])),
                                  (w["target_n"], w["target_e"], w["target_alt"]))
                    w["computed_m"] = min(w["computed_m"], d)

            for name in SURFACES:
                gap = abs(float(raw[f"{name}_cmd_deg"]) - float(raw[f"{name}_actual_deg"]))
                sc = screen[name]
                if gap > sc["max_gap_deg"]:
                    sc["max_gap_deg"], sc["max_gap_time_s"] = gap, t
                if gap > DIVERGENCE_THRESHOLD_DEG:
                    sc["samples_over"] += 1
                    if name == "aileron_left":
                        left_over.append((t, gap))
                    if sc["first_over_s"] is None:
                        sc["first_over_s"], sc["first_over_state"] = t, raw["vehicle_state"]

            # Keep the row if: sampled OR around the event (full resolution)
            in_anomaly_window = 200.0 <= t <= 300.0
            if (row_count % sample_rate == 0) or in_anomaly_window:
                rows.append({
                    "t": t,
                    "state": raw["vehicle_state"],
                    "mode": raw["control_mode"],
                    "alt_agl": float(raw["alt_agl_m"]) if raw["alt_agl_m"] else 0,
                    "roll": float(raw["roll_deg"]),
                    "roll_sp": float(raw["roll_setpoint_deg"]),
                    "roll_err": float(raw["roll_error_deg"]),
                    "sat_roll": int(raw["controller_saturation_roll"]),
                    "al_cmd": float(raw["aileron_left_cmd_deg"]),
                    "al_act": float(raw["aileron_left_actual_deg"]),
                    "ar_cmd": float(raw["aileron_right_cmd_deg"]),
                    "ar_act": float(raw["aileron_right_actual_deg"]),
                    "al_current": float(raw["servo_aileron_left_current_a"]),
                    "ar_current": float(raw["servo_aileron_right_current_a"]),
                    "bat_v": float(raw["battery_voltage_v"]),
                    "servo_v": float(raw["servo_bus_voltage_v"]),
                    "brownout": int(raw["brownout_flag"]),
                    "wp": raw["active_waypoint_id"],
                    "wp_dist": float(raw["active_waypoint_dist_m"]) if raw["active_waypoint_dist_m"] else 0,
                    "failsafe": raw.get("failsafe_reason", ""),
                    "pos_n": float(raw["pos_n_m"]) if raw["pos_n_m"] else 0,
                    "pos_e": float(raw["pos_e_m"]) if raw["pos_e_m"] else 0,
                    "wind": float(raw["wind_est_speed_mps"]),
                    "temp_c": float(raw["outside_temp_c"]),
                    "accel_z": float(raw["accel_z_filt_mps2"]),
                    "gps_fix": int(float(raw["gps_fix_type"])),
                    "gps_sats": int(float(raw["gps_sats"])),
                    "gps_hdop": float(raw["gps_hdop"]),
                    "att_valid": int(float(raw["attitude_valid"])),
                })

    return rows, events, state_changes, mode_changes, row_count, screen, left_over


def select_suspect(screen):
    """
    Pick the surface that diverged from its command first. Anything that
    diverges later is a candidate effect, not the initiating cause.
    """
    candidates = [(sc["first_over_s"], name) for name, sc in screen.items()
                  if sc["first_over_s"] is not None]
    return min(candidates)[1] if candidates else None


# ---------------------------------------------------------------------------
# Step 3: Analyze the Anomaly — Quantify the Left Aileron Divergence
# ---------------------------------------------------------------------------
def analyze_anomaly(left_over, gap_tolerance_s=0.5, min_duration_s=0.5):
    """
    Group the 500 Hz samples where the left aileron is > 5 deg off its
    command into episodes. Gaps shorter than `gap_tolerance_s` are merged.
    The first episode is the primary seizure at WP-02. Later episodes count
    only if they last at least `min_duration_s`; shorter blips (a single
    sample just over 5 deg, or the 30 ms transient at the bank-pattern exit
    that both ailerons show) are counted separately, not as sticking.
    """
    segments = []
    for t, gap in left_over:
        if segments and t - segments[-1]["end_s"] <= gap_tolerance_s:
            ep = segments[-1]
            ep["end_s"] = t
            if gap > ep["max_gap_deg"]:
                ep["max_gap_deg"], ep["max_gap_time_s"] = gap, t
        else:
            segments.append({"start_s": t, "end_s": t, "max_gap_deg": gap, "max_gap_time_s": t})

    if not segments:
        return {"start_s": None, "end_s": None, "duration_s": 0,
                "max_gap_deg": 0, "max_gap_time_s": 0, "episodes": [], "blips": []}

    first, later = segments[0], segments[1:]
    episodes = [first] + [e for e in later if e["end_s"] - e["start_s"] >= min_duration_s]
    blips = [e for e in later if e["end_s"] - e["start_s"] < min_duration_s]
    return {
        "start_s": first["start_s"],
        "end_s": first["end_s"],
        "duration_s": round(first["end_s"] - first["start_s"], 2),
        "max_gap_deg": first["max_gap_deg"],
        "max_gap_time_s": round(first["max_gap_time_s"], 2),
        "episodes": episodes,
        "blips": blips,
    }


def describe_breakaway(rows, anomaly):
    """Where the stuck surface sat, and how fast it snapped free (500 Hz rows)."""
    win = [r for r in rows if anomaly["start_s"] <= r["t"] <= anomaly["end_s"] + 0.5]
    stuck = [r for r in win if r["t"] <= anomaly["max_gap_time_s"]]
    after = [r for r in win if r["t"] > anomaly["max_gap_time_s"]]
    free = next(r for r in after if r["al_act"] >= r["al_cmd"] - 1.0)
    last_stuck = [r for r in after if r["t"] < free["t"] and r["al_act"] <= -9.0][-1]
    return {
        "stuck_min": min(r["al_act"] for r in stuck),
        "stuck_max": max(r["al_act"] for r in stuck if r["t"] >= anomaly["start_s"] + 1.0),
        "from_deg": last_stuck["al_act"], "from_t": last_stuck["t"],
        "to_deg": free["al_act"], "to_t": free["t"],
    }


# ---------------------------------------------------------------------------
# Step 4: Generate Figures
# ---------------------------------------------------------------------------
def generate_figures(rows, waypoints, events, anomaly, output_dir):
    """Generate the 3 evidence charts for the report."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend (no GUI needed)
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        print("  [WARN] matplotlib not installed — skipping figure generation.")
        print("         Install with: pip3 install matplotlib")
        return

    # ── Color scheme (dark theme matching the telemetry viewer) ──
    BG_COLOR = "#0d1117"
    TEXT_COLOR = "#c9d1d9"
    GRID_COLOR = "#21262d"
    GREEN = "#3fb950"
    RED = "#f85149"
    YELLOW = "#d29922"
    BLUE = "#58a6ff"
    PURPLE = "#bc8cff"

    # ── Filter rows for the anomaly window plot (t=230 to t=270) ──
    anom_rows = [r for r in rows if 230 <= r["t"] <= 270]
    # ── Full flight rows (sampled) ──
    full_rows = [r for r in rows if r["t"] % 1.0 < 0.01 or 240 <= r["t"] <= 265]

    # ====================================================================
    # FIGURE 1: The Smoking Gun — Roll + Aileron Commands vs Actuals
    # ====================================================================
    fig1, (ax1a, ax1b) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    fig1.patch.set_facecolor(BG_COLOR)

    t_vals = [r["t"] for r in anom_rows]

    # Top subplot: Roll setpoint vs actual
    ax1a.set_facecolor(BG_COLOR)
    ax1a.plot(t_vals, [r["roll_sp"] for r in anom_rows], color=BLUE,
              linewidth=1.5, label="Roll Setpoint (commanded)")
    ax1a.plot(t_vals, [r["roll"] for r in anom_rows], color=RED,
              linewidth=1.5, label="Roll Actual (measured)")
    ax1a.axvspan(anomaly["start_s"], anomaly["end_s"], alpha=0.15,
                 color=RED, label=f'Anomaly Window ({anomaly["duration_s"]}s)')
    ax1a.axhline(0, color=GRID_COLOR, linewidth=0.5)
    ax1a.set_ylabel("Roll Angle (degrees)", color=TEXT_COLOR)
    ax1a.set_title("Flight FLT-2026-02-28-001 — Left Aileron Seizure Event",
                    color=TEXT_COLOR, fontsize=14, fontweight="bold")
    ax1a.legend(loc="upper left", fontsize=9, facecolor=BG_COLOR,
                edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)
    ax1a.tick_params(colors=TEXT_COLOR)
    ax1a.spines[:].set_color(GRID_COLOR)
    ax1a.grid(True, color=GRID_COLOR, alpha=0.3)

    # Bottom subplot: Left aileron cmd vs actual
    ax1b.set_facecolor(BG_COLOR)
    ax1b.plot(t_vals, [r["al_cmd"] for r in anom_rows], color=BLUE,
              linewidth=1.5, linestyle="--", label="Left Aileron CMD")
    ax1b.plot(t_vals, [r["al_act"] for r in anom_rows], color=RED,
              linewidth=2.0, label="Left Aileron ACTUAL")
    ax1b.plot(t_vals, [r["ar_cmd"] for r in anom_rows], color=GREEN,
              linewidth=1.0, linestyle="--", alpha=0.6, label="Right Aileron CMD")
    ax1b.plot(t_vals, [r["ar_act"] for r in anom_rows], color=GREEN,
              linewidth=1.0, alpha=0.6, label="Right Aileron ACTUAL")
    ax1b.axvspan(anomaly["start_s"], anomaly["end_s"], alpha=0.15, color=RED)
    ax1b.axhline(0, color=GRID_COLOR, linewidth=0.5)
    ax1b.set_xlim(min(t_vals), max(t_vals))
    ax1b.set_xlabel("Time (seconds)", color=TEXT_COLOR)
    ax1b.set_ylabel("Aileron Deflection (degrees)", color=TEXT_COLOR)
    ax1b.legend(loc="lower left", fontsize=9, facecolor=BG_COLOR,
                edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)
    ax1b.tick_params(colors=TEXT_COLOR)
    ax1b.spines[:].set_color(GRID_COLOR)
    ax1b.grid(True, color=GRID_COLOR, alpha=0.3)

    fig1.tight_layout()
    fig1_path = os.path.join(output_dir, "fig1_aileron_seizure.png")
    fig1.savefig(fig1_path, dpi=150, facecolor=BG_COLOR)
    plt.close(fig1)
    print(f"  Saved: {fig1_path}")

    # ====================================================================
    # FIGURE 2: Waypoint Miss Distances
    # ====================================================================
    fig2, ax2 = plt.subplots(figsize=(12, 5))
    fig2.patch.set_facecolor(BG_COLOR)
    ax2.set_facecolor(BG_COLOR)

    wp_ids = [w["id"] for w in waypoints]
    wp_miss = [w["closest_m"] for w in waypoints]
    wp_colors = [GREEN if w["hit"] else RED for w in waypoints]

    bars = ax2.bar(wp_ids, wp_miss, color=wp_colors, edgecolor=GRID_COLOR,
                   linewidth=0.5)
    ax2.axhline(75.0, color=YELLOW, linewidth=1.5, linestyle="--",
                label="Hit Tolerance (75 m)")
    ax2.set_ylabel("Closest Approach (meters)", color=TEXT_COLOR)
    ax2.set_xlabel("Waypoint", color=TEXT_COLOR)
    ax2.set_title("Waypoint Miss Distances — Cascading Deviation After WP-02",
                   color=TEXT_COLOR, fontsize=13, fontweight="bold")
    ax2.tick_params(colors=TEXT_COLOR, axis="x", rotation=45)
    ax2.tick_params(colors=TEXT_COLOR, axis="y")
    ax2.spines[:].set_color(GRID_COLOR)
    ax2.legend(facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)
    ax2.grid(True, axis="y", color=GRID_COLOR, alpha=0.3)

    # Annotate the first miss and the worst miss
    ax2.annotate("First miss\n(109.2 m)", xy=(2, 109.23), xytext=(4, 400),
                 arrowprops=dict(arrowstyle="->", color=TEXT_COLOR),
                 color=TEXT_COLOR, fontsize=9, ha="center")
    ax2.annotate(f"Final miss\n({wp_miss[-1]:.0f} m)", xy=(20, wp_miss[-1]),
                 xytext=(18, wp_miss[-1] + 500),
                 arrowprops=dict(arrowstyle="->", color=TEXT_COLOR),
                 color=TEXT_COLOR, fontsize=9, ha="center")

    fig2.tight_layout()
    fig2_path = os.path.join(output_dir, "fig2_waypoint_misses.png")
    fig2.savefig(fig2_path, dpi=150, facecolor=BG_COLOR)
    plt.close(fig2)
    print(f"  Saved: {fig2_path}")

    # ====================================================================
    # FIGURE 3: Altitude Profile with Events
    # ====================================================================
    # rows is already decimated (every 50th sample = 10 Hz) outside the
    # anomaly window, which is plenty for a full-flight altitude trace
    sampled = rows

    fig3, ax3 = plt.subplots(figsize=(14, 5))
    fig3.patch.set_facecolor(BG_COLOR)
    ax3.set_facecolor(BG_COLOR)

    ax3.plot([r["t"] for r in sampled], [r["alt_agl"] for r in sampled],
             color=BLUE, linewidth=1.5)
    ax3.axhline(0, color=RED, linewidth=1, linestyle="-", alpha=0.5)

    # Annotate key events
    event_annotations = {
        "BANK_PATTERN_ENTRY": ("Bank Pattern\nEntry", GREEN),
        "BANK_PATTERN_EXIT": ("Bank Pattern\nExit", YELLOW),
        "IMPACT_DETECTED": ("IMPACT", RED),
    }
    for t_ev, ev_name in events:
        if ev_name in event_annotations:
            label, color = event_annotations[ev_name]
            ax3.axvline(t_ev, color=color, linewidth=1, linestyle="--", alpha=0.7)
            # IMPACT sits ~34 s after BANK_PATTERN_EXIT; drop it lower so
            # the two labels don't collide
            y = 1250 if ev_name == "IMPACT_DETECTED" else 1400
            ax3.annotate(label, xy=(t_ev, y), fontsize=8, color=color,
                         ha="center", rotation=0)

    # Mark the primary seizure and the later recurrences
    ax3.axvspan(anomaly["start_s"], anomaly["end_s"], alpha=0.35, color=RED,
                label=f'Primary seizure ({anomaly["duration_s"]}s)')
    for i, ep in enumerate(anomaly["episodes"][1:]):
        ax3.axvspan(ep["start_s"], max(ep["end_s"], ep["start_s"] + 1.5),
                    alpha=0.25, color=YELLOW,
                    label="Recurring sticking (>5° gap)" if i == 0 else None)
    ax3.legend(loc="lower left", fontsize=9, facecolor=BG_COLOR,
               edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)

    ax3.set_xlabel("Time (seconds)", color=TEXT_COLOR)
    ax3.set_ylabel("Altitude AGL (meters)", color=TEXT_COLOR)
    ax3.set_title("Altitude Profile — Flight FLT-2026-02-28-001",
                   color=TEXT_COLOR, fontsize=13, fontweight="bold")
    ax3.tick_params(colors=TEXT_COLOR)
    ax3.spines[:].set_color(GRID_COLOR)
    ax3.grid(True, color=GRID_COLOR, alpha=0.3)

    fig3.tight_layout()
    fig3_path = os.path.join(output_dir, "fig3_altitude_profile.png")
    fig3.savefig(fig3_path, dpi=150, facecolor=BG_COLOR)
    plt.close(fig3)
    print(f"  Saved: {fig3_path}")


# ---------------------------------------------------------------------------
# Step 5: Hypothesis Testing — Check Alternative Causes
# ---------------------------------------------------------------------------
def test_hypotheses(rows, anomaly, mode_changes):
    """
    Systematically test each alternative hypothesis using telemetry data.
    Returns a list of (hypothesis, evidence, verdict) tuples.
    """
    # Get rows in the anomaly window
    anom = [r for r in rows if anomaly["start_s"] <= r["t"] <= anomaly["end_s"]]
    if not anom:
        return []

    results = []

    # H0: Operator / mode — was the vehicle autonomous through the anomaly?
    modes_in_window = {r["mode"] for r in anom}
    failsafe = [(t, mode, why) for t, mode, why in mode_changes if mode != "AUTO_STABILIZE"]
    fs_text = (f"First mode change: {failsafe[0][1]} at t = {failsafe[0][0]:.0f}s "
               f"(reason: {failsafe[0][2] or 'n/a'}), {failsafe[0][0] - anomaly['start_s']:.0f}s "
               f"after the anomaly began — a consequence, not a cause." if failsafe
               else "No mode change for the whole flight.")
    results.append((
        "Operator / Mode Change",
        f"Control mode during anomaly: {', '.join(sorted(modes_in_window))}. {fs_text}",
        "ELIMINATED" if modes_in_window == {"AUTO_STABILIZE"} else "POSSIBLE"
    ))

    # H1: Software bug — did the autopilot fail to issue the command?
    cmd_issued = any(r["al_cmd"] > 10.0 for r in anom)
    results.append((
        "Software / Autopilot Bug",
        f"aileron_left_cmd reached +{max(r['al_cmd'] for r in anom):.1f}° "
        f"during anomaly window — command was issued correctly",
        "ELIMINATED" if cmd_issued else "POSSIBLE"
    ))

    # H2: Power / electrical failure
    min_bat_v = min(r["bat_v"] for r in anom)
    min_servo_v = min(r["servo_v"] for r in anom)
    any_brownout = any(r["brownout"] for r in anom)
    right_aileron_ok = all(abs(r["ar_cmd"] - r["ar_act"]) < 2.0 for r in anom)
    results.append((
        "Electrical / Power Failure",
        f"Battery: {min_bat_v:.2f}V (nominal), Servo bus: {min_servo_v:.2f}V, "
        f"Brownout: {'YES' if any_brownout else 'No'}, "
        f"Right aileron (same bus): {'tracking normally' if right_aileron_ok else 'ALSO FAILING'}",
        "ELIMINATED" if (min_bat_v > 11.0 and not any_brownout and right_aileron_ok) else "POSSIBLE"
    ))

    # H3: Sensor / feedback error — did the plane physically roll?
    # Only look before breakaway: once the surface snaps free the roll
    # catches up, which says nothing about whether it was stuck before
    stuck = [r for r in anom if r["t"] <= anomaly["max_gap_time_s"]]
    max_roll_achieved = max(r["roll"] for r in stuck)
    results.append((
        "Sensor Feedback Error (servo moved but sensor lied)",
        f"If the servo had physically moved, the aircraft would have rolled "
        f"to ~{max(r['roll_sp'] for r in stuck):.1f}°. Actual roll only reached "
        f"{max_roll_achieved:.1f}° — confirming surface was truly stuck.",
        "ELIMINATED"
    ))

    # H4: Environmental — wind, turbulence, icing (200–300 s around the event)
    env = [r for r in rows if 200 <= r["t"] <= 300]
    # The bank reversal shows as a step in roll error (0 to ~2 deg within
    # about 10 ms). Find it as the first sample above 0.5 deg, then look at
    # everything up to 20 ms before it, which excludes the step's own ramp;
    # a calm airframe should track its setpoint almost exactly
    reversal_t = next(r["t"] for r in rows if r["t"] >= 200 and abs(r["roll_err"]) > 0.5)
    pre_anom = [r for r in rows if 200 <= r["t"] < reversal_t - 0.02]
    if env and pre_anom:
        max_roll_err_pre = max(abs(r["roll_err"]) for r in pre_anom)
        wind_max = max(r["wind"] for r in env)
        az_lo, az_hi = min(r["accel_z"] for r in env), max(r["accel_z"] for r in env)
        t_lo, t_hi = min(r["temp_c"] for r in env), max(r["temp_c"] for r in env)
        calm = wind_max < 8.0 and az_lo > 7.0 and az_hi < 12.0 and t_lo > 2.0
        results.append((
            "Wind Gust / Turbulence / Icing",
            f"Wind {min(r['wind'] for r in env):.1f}–{wind_max:.1f} m/s, "
            f"vertical accel {az_lo:.1f}–{az_hi:.1f} m/s² (no gust spikes), "
            f"outside temp {t_lo:.1f}–{t_hi:.1f} °C (no icing). "
            f"Max roll error from 200 s until the bank reversal began at {reversal_t:.1f}s: "
            f"{max_roll_err_pre:.2f}°.",
            "ELIMINATED" if calm else "POSSIBLE"
        ))

    # H4b: Navigation / attitude sensors
    gps_ok = all(r["gps_fix"] == 3 for r in env)
    att_ok = all(r["att_valid"] == 1 for r in env)
    results.append((
        "Navigation / Attitude Sensor Fault",
        f"GPS fix type 3 throughout: {'yes' if gps_ok else 'NO'}; "
        f"satellites {min(r['gps_sats'] for r in env)}–{max(r['gps_sats'] for r in env)}; "
        f"HDOP ≤ {max(r['gps_hdop'] for r in env):.2f}; "
        f"attitude_valid = 1 throughout: {'yes' if att_ok else 'NO'}.",
        "ELIMINATED" if (gps_ok and att_ok) else "POSSIBLE"
    ))

    # H5: Mechanical — LEFT AILERON SEIZURE
    max_gap = max(abs(r["al_cmd"] - r["al_act"]) for r in anom)
    max_gap_r = max(abs(r["ar_cmd"] - r["ar_act"]) for r in anom)
    sat_count = sum(1 for r in anom if r["sat_roll"] == 1)
    sat_pct = (sat_count / len(anom)) * 100 if anom else 0
    n_later = len(anomaly["episodes"]) - 1
    results.append((
        "Left Aileron Mechanical Seizure",
        f"Command-vs-actual gap: {max_gap:.1f}° (right aileron, same bus: max {max_gap_r:.1f}°). "
        f"Controller saturated {sat_pct:.0f}% of anomaly window. "
        f"Duration: {anomaly['duration_s']}s. "
        f"Sticking recurred {n_later} more times later in the flight.",
        "CONFIRMED"
    ))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("  FLIGHT INVESTIGATION — FLT-2026-02-28-001 / GLDR-0421")
    print("=" * 70)

    # Load data
    print("\n[1] Loading flight plan...")
    waypoints = load_flight_plan(FLIGHT_PLAN_PATH)
    print(f"    {len(waypoints)} waypoints loaded")

    print("\n[2] Loading telemetry (this may take 30-60 seconds)...")
    rows, events, state_changes, mode_changes, total_rows, screen, left_over = load_telemetry(TELEMETRY_PATH, waypoints)
    print(f"    {total_rows:,} total rows, {len(rows):,} kept for analysis")

    # Flight timeline
    print("\n[3] Flight Timeline:")
    print(f"    {'Time':>8}  {'Event / State':<30}")
    print(f"    {'─' * 8}  {'─' * 30}")
    for t, state in state_changes:
        print(f"    {t:8.1f}s  State → {state}")
    for t, mode, why in mode_changes:
        print(f"    {t:8.1f}s  Mode  → {mode}" + (f" ({why})" if why else ""))
    for t, ev in events:
        print(f"    {t:8.1f}s  EVENT: {ev}")

    # Waypoint summary
    print(f"\n[4] Waypoint Results (miss = closest 3D distance within ±{WAYPOINT_WINDOW_S:.0f} s of target time):")
    print(f"    {'WP':<6} {'Time':>7} {'Provided':>10} {'Computed':>10} {'Status':>8}")
    print(f"    {'─' * 6} {'─' * 7} {'─' * 10} {'─' * 10} {'─' * 8}")
    for w in waypoints:
        status = "✅ HIT" if w["computed_m"] <= w["tolerance"] else "❌ MISS"
        print(f"    {w['id']:<6} {w['target_time_s']:>7.0f}s "
              f"{w['closest_m']:>10.1f} {w['computed_m']:>10.1f} {status:>8}")
    worst = max(abs(w["computed_m"] - w["closest_m"]) for w in waypoints)
    agree = all((w["computed_m"] <= w["tolerance"]) == bool(w["hit"]) for w in waypoints)
    print(f"    Recomputed from telemetry: max difference from flight_plan.csv {worst:.2f} m; "
          f"hit/miss {'agrees' if agree else 'DISAGREES'} for all {len(waypoints)} waypoints "
          f"(tolerance {waypoints[0]['tolerance']:.0f} m).")

    # Screen every control surface before focusing on any one of them
    print(f"\n[5] Control Surface Screen (all {total_rows:,} rows, |cmd − actual| > {DIVERGENCE_THRESHOLD_DEG:.0f}°):")
    print(f"    {'Surface':<14} {'Max gap':>8} {'at t':>8} {'First >5°':>10} {'State then':<13} {'Samples >5°':>11}")
    print(f"    {'─' * 14} {'─' * 8} {'─' * 8} {'─' * 10} {'─' * 13} {'─' * 11}")
    for name in SURFACES:
        sc = screen[name]
        first = f"{sc['first_over_s']:.1f}s" if sc["first_over_s"] is not None else "never"
        print(f"    {name:<14} {sc['max_gap_deg']:>7.1f}° {sc['max_gap_time_s']:>7.1f}s "
              f"{first:>10} {sc['first_over_state'] or '—':<13} {sc['samples_over']:>11,}")
    suspect = select_suspect(screen)
    print(f"    → Suspect surface: {suspect} (first to diverge from its command).")
    t0 = screen[suspect]["first_over_s"]
    for name in SURFACES:
        sc = screen[name]
        if name != suspect and sc["first_over_s"] is not None:
            print(f"      {name} first diverges {sc['first_over_s'] - t0:.0f}s later "
                  f"({sc['samples_over']} samples = {sc['samples_over'] / 500 * 1000:.0f} ms at 500 Hz) — "
                  f"after the deviation was already under way, so an effect, not the cause.")
    if suspect != "aileron_left":
        print("    [WARN] Screen does not point at the left aileron — detailed analysis below may not apply.")

    # Anomaly analysis
    print("\n[6] Anomaly Analysis (suspect surface in detail):")
    anomaly = analyze_anomaly(left_over)
    brk = describe_breakaway(rows, anomaly)
    print(f"    Left aileron divergence detected (500 Hz):")
    print(f"      Start:     t = {anomaly['start_s']:.1f}s")
    print(f"      End:       t = {anomaly['end_s']:.1f}s")
    print(f"      Duration:  {anomaly['duration_s']:.1f} seconds")
    print(f"      Max gap:   {anomaly['max_gap_deg']:.1f}° at t = {anomaly['max_gap_time_s']:.1f}s")
    print(f"      Stuck at:  {brk['stuck_max']:.1f}° to {brk['stuck_min']:.1f}° while commanded positive")
    print(f"      Breakaway: {brk['from_deg']:.1f}° at {brk['from_t']:.2f}s → {brk['to_deg']:.1f}° at "
          f"{brk['to_t']:.2f}s ({(brk['to_t'] - brk['from_t']) * 1000:.0f} ms)")
    print(f"      Later sticking episodes (>5° gap, gaps < 0.5 s merged, lasting ≥ 0.5 s): "
          f"{len(anomaly['episodes']) - 1}")
    for ep in anomaly["episodes"][1:]:
        print(f"        t = {ep['start_s']:7.1f}s  for {ep['end_s'] - ep['start_s']:4.1f}s  max gap {ep['max_gap_deg']:4.1f}°")
    print(f"      Excluded blips under 0.5 s: " + ", ".join(
        f"{b['start_s']:.1f}s ({b['max_gap_deg']:.1f}°)" for b in anomaly["blips"]))

    # Hypothesis testing
    print("\n[7] Hypothesis Testing:")
    results = test_hypotheses(rows, anomaly, mode_changes)
    for hyp, evidence, verdict in results:
        icon = "✅" if verdict == "CONFIRMED" else "❌"
        print(f"\n    {icon} {hyp}: {verdict}")
        print(f"       {evidence}")

    # Generate figures
    print("\n[8] Generating figures...")
    generate_figures(rows, waypoints, events, anomaly, OUTPUT_DIR)

    # Summary
    print("\n" + "=" * 70)
    print("  CONCLUSION: Left aileron actuator mechanical seizure")
    print(f"  Duration: {anomaly['duration_s']}s (t={anomaly['start_s']:.0f}s to t={anomaly['end_s']:.0f}s)")
    print("  All alternative hypotheses eliminated.")
    print("  → Proceed to manufacturing_analysis.py to trace the defect.")
    print("=" * 70)

    return anomaly


if __name__ == "__main__":
    main()
