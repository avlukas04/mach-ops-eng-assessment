"""
Localhost telemetry viewer for assessment playback.

Serves a web UI that shows:
- 3D flight path + moving vehicle marker + waypoints
- Playback controls (play/pause, timeline, speed)
- Telemetry panel with key data fields

Usage:
  python telemetry-simulation.py
  python telemetry-simulation.py --telemetry telemetry.csv --flight-plan flight_plan.csv --stride 10 --port 8765
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse


REQUIRED_TELEMETRY_COLUMNS = ["time_s", "pos_n_m", "pos_e_m", "alt_agl_m"]
FLIGHT_PLAN_REQUIRED_COLUMNS = [
    "waypoint_id",
    "target_pos_n_m",
    "target_pos_e_m",
    "target_alt_agl_m",
]


def fail(msg: str) -> None:
    raise SystemExit(f"error: {msg}")


def parse_float(value: str) -> Optional[float]:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(value: str) -> Optional[int]:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def load_telemetry(csv_path: Path, stride: int, max_points: int) -> List[Dict[str, object]]:
    if stride < 1:
        fail("--stride must be >= 1")
    rows: List[Dict[str, object]] = []
    total_rows = 0

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            fail(f"{csv_path} has no header row")

        missing = [c for c in REQUIRED_TELEMETRY_COLUMNS if c not in reader.fieldnames]
        if missing:
            fail(f"{csv_path} missing required columns: {', '.join(missing)}")

        for raw in reader:
            total_rows += 1
            if (total_rows - 1) % stride != 0:
                continue

            time_s = parse_float(raw.get("time_s", ""))
            pos_n = parse_float(raw.get("pos_n_m", ""))
            pos_e = parse_float(raw.get("pos_e_m", ""))
            alt_agl = parse_float(raw.get("alt_agl_m", ""))
            if time_s is None or pos_n is None or pos_e is None or alt_agl is None:
                continue

            row = {
                "flight_id": (raw.get("flight_id", "") or "").strip(),
                "vehicle_id": (raw.get("vehicle_id", "") or "").strip(),
                "time_s": round(time_s, 3),
                "timestamp_ms": parse_int(raw.get("timestamp_ms", "")),
                "lat_deg": parse_float(raw.get("lat_deg", "")),
                "lon_deg": parse_float(raw.get("lon_deg", "")),
                "alt_msl_m": parse_float(raw.get("alt_msl_m", "")),
                "alt_agl_m": alt_agl,
                "pos_n_m": pos_n,
                "pos_e_m": pos_e,
                "roll_deg": parse_float(raw.get("roll_deg", "")),
                "roll_setpoint_deg": parse_float(raw.get("roll_setpoint_deg", "")),
                "pitch_deg": parse_float(raw.get("pitch_deg", "")),
                "yaw_deg": parse_float(raw.get("yaw_deg", "")),
                "yaw_rate_dps": parse_float(raw.get("yaw_rate_dps", "")),
                "vehicle_state": (raw.get("vehicle_state", "") or "").strip(),
                "control_mode": (raw.get("control_mode", "") or "").strip(),
                "event_marker": (raw.get("event_marker", "") or "").strip(),
                "aileron_left_cmd_deg": parse_float(raw.get("aileron_left_cmd_deg", "")),
                "aileron_left_actual_deg": parse_float(raw.get("aileron_left_actual_deg", "")),
                "aileron_right_cmd_deg": parse_float(raw.get("aileron_right_cmd_deg", "")),
                "aileron_right_actual_deg": parse_float(raw.get("aileron_right_actual_deg", "")),
                "elevon_left_cmd_deg": parse_float(raw.get("elevon_left_cmd_deg", "")),
                "elevon_left_actual_deg": parse_float(raw.get("elevon_left_actual_deg", "")),
                "elevon_right_cmd_deg": parse_float(raw.get("elevon_right_cmd_deg", "")),
                "elevon_right_actual_deg": parse_float(raw.get("elevon_right_actual_deg", "")),
                "rudder_cmd_deg": parse_float(raw.get("rudder_cmd_deg", "")),
                "rudder_actual_deg": parse_float(raw.get("rudder_actual_deg", "")),
                "gps_groundspeed_mps": parse_float(raw.get("gps_groundspeed_mps", "")),
                "airspeed_mps": parse_float(raw.get("airspeed_mps", "")),
                "battery_voltage_v": parse_float(raw.get("battery_voltage_v", "")),
                "battery_current_a": parse_float(raw.get("battery_current_a", "")),
                "servo_bus_current_a": parse_float(raw.get("servo_bus_current_a", "")),
                "servo_aileron_left_current_a": parse_float(raw.get("servo_aileron_left_current_a", "")),
                "servo_aileron_right_current_a": parse_float(raw.get("servo_aileron_right_current_a", "")),
                "controller_saturation_roll": parse_float(raw.get("controller_saturation_roll", "")),
                "controller_saturation_pitch": parse_float(raw.get("controller_saturation_pitch", "")),
                "active_waypoint_id": (raw.get("active_waypoint_id", "") or "").strip(),
                "active_waypoint_dist_m": parse_float(raw.get("active_waypoint_dist_m", "")),
            }
            rows.append(row)
            if max_points > 0 and len(rows) >= max_points:
                break

    if not rows:
        fail("no usable telemetry rows found after parsing/downsampling")

    print(
        f"Loaded {len(rows)} telemetry points "
        f"(from {total_rows} CSV rows, stride={stride}, max_points={max_points or 'none'})"
    )
    return rows


def load_flight_plan(csv_path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            fail(f"{csv_path} has no header row")

        missing = [c for c in FLIGHT_PLAN_REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            fail(f"{csv_path} missing required columns: {', '.join(missing)}")

        for raw in reader:
            wp_id = (raw.get("waypoint_id", "") or "").strip()
            x = parse_float(raw.get("target_pos_n_m", ""))
            y = parse_float(raw.get("target_pos_e_m", ""))
            z = parse_float(raw.get("target_alt_agl_m", ""))
            if not wp_id or x is None or y is None or z is None:
                continue
            rows.append(
                {
                    "waypoint_id": wp_id,
                    "x": x,
                    "y": y,
                    "z": z,
                    "target_time_s": parse_float(raw.get("target_time_s", "")),
                    "hit_actual_flag": parse_int(raw.get("hit_actual_flag", "")),
                }
            )

    print(f"Loaded {len(rows)} waypoints from {csv_path.name}")
    return rows


def build_html(title: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{
      --bg: #0b1220;
      --panel: #111a2e;
      --panel-alt: #16233e;
      --text: #e5ecff;
      --muted: #9fb0d7;
      --line: #2a3a5f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "SF Pro Text", "Segoe UI", sans-serif;
      background: radial-gradient(1200px 700px at 10% -10%, #17305c 0%, var(--bg) 55%);
      color: var(--text);
    }}
    .page-bar {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 14px 6px 14px;
    }}
    .page-title {{
      margin: 0;
      font-size: 16px;
      letter-spacing: 0.02em;
      color: #d8e6ff;
      white-space: nowrap;
    }}
    .page-controls {{
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex: 1;
      justify-content: flex-end;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 14px;
      height: calc(100vh - 54px);
      padding: 14px;
    }}
    .card {{
      background: linear-gradient(180deg, var(--panel) 0%, #0e172b 100%);
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: hidden;
      min-height: 0;
    }}
    .plots {{
      width: 100%;
      height: 100%;
      display: grid;
      grid-template-rows: 64% 36%;
      gap: 8px;
      padding: 8px;
    }}
    .metric-panel {{
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 6px;
      min-height: 0;
    }}
    .metric-header {{
      display: flex;
      align-items: center;
      justify-content: flex-start;
      gap: 8px;
      padding: 0 2px;
      color: var(--muted);
      font-size: 12px;
    }}
    #flight3d, #metricPlot {{
      width: 100%;
      height: 100%;
      border: 1px solid #213356;
      border-radius: 10px;
      overflow: hidden;
    }}
    .row {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .row label {{
      color: var(--muted);
      font-size: 12px;
      min-width: 66px;
    }}
    button, select, input[type="range"] {{
      background: var(--panel-alt);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 8px;
    }}
    input[type="range"] {{
      flex: 1;
      padding: 0;
    }}
    .telemetry {{
      padding: 12px;
      display: grid;
      gap: 10px;
      height: 100%;
      overflow: auto;
    }}
    .telemetry h2 {{
      margin: 0;
      font-size: 15px;
      letter-spacing: 0.02em;
      color: #d8e6ff;
    }}
    .attitude-wrap {{
      display: grid;
      gap: 8px;
      justify-items: center;
      padding: 4px 0 2px 0;
    }}
    .attitude {{
      width: 170px;
      height: 170px;
      border: 2px solid #2a3a5f;
      border-radius: 50%;
      overflow: hidden;
      position: relative;
      background: #0f1b31;
      box-shadow: inset 0 0 0 1px #1b2d4b;
    }}
    .horizon {{
      position: absolute;
      inset: -35%;
      transform-origin: 50% 50%;
    }}
    .sky {{
      height: 50%;
      background: linear-gradient(180deg, #4e8bc9 0%, #386ea5 100%);
    }}
    .ground {{
      height: 50%;
      background: linear-gradient(180deg, #6a4a2f 0%, #4f3826 100%);
    }}
    .horizon-line {{
      position: absolute;
      left: 0;
      right: 0;
      top: 50%;
      height: 2px;
      background: rgba(245, 247, 255, 0.8);
    }}
    .wings {{
      position: absolute;
      left: 50%;
      top: 50%;
      width: 84px;
      height: 3px;
      background: #f8fafc;
      transform: translate(-50%, -50%);
      box-shadow: 0 0 0 1px rgba(0,0,0,0.25);
    }}
    .wings::before, .wings::after {{
      content: "";
      position: absolute;
      top: -7px;
      width: 3px;
      height: 14px;
      background: #f8fafc;
    }}
    .wings::before {{ left: 0; }}
    .wings::after {{ right: 0; }}
    .nose {{
      position: absolute;
      left: 50%;
      top: calc(50% + 4px);
      width: 0;
      height: 0;
      border-left: 7px solid transparent;
      border-right: 7px solid transparent;
      border-top: 10px solid #f8fafc;
      transform: translateX(-50%);
    }}
    .heading-tag {{
      color: #d6e5ff;
      font-size: 12px;
      font-variant-numeric: tabular-nums;
      letter-spacing: 0.06em;
      background: #16233e;
      border: 1px solid #2a3a5f;
      border-radius: 8px;
      padding: 3px 8px;
    }}
    .telemetry-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px 10px;
      font-size: 13px;
    }}
    .k {{
      color: var(--muted);
      font-size: 12px;
    }}
    .v {{
      color: #eef4ff;
      font-variant-numeric: tabular-nums;
    }}
    @media (max-width: 1050px) {{
      .page-bar {{
        flex-direction: column;
        align-items: stretch;
      }}
      .page-controls {{
        justify-content: flex-start;
        flex-wrap: wrap;
      }}
      .layout {{
        grid-template-columns: 1fr;
        grid-template-rows: 62vh 38vh;
        height: calc(100vh - 84px);
      }}
    }}
  </style>
</head>
<body>
  <div class="page-bar">
    <h1 class="page-title" id="pageTitle">Telemetry Viewer</h1>
    <div class="page-controls">
      <button id="playBtn">Play</button>
      <label for="speed">Speed</label>
      <select id="speed">
        <option value="0.5">0.5x</option>
        <option value="1" selected>1x</option>
        <option value="2">2x</option>
        <option value="4">4x</option>
        <option value="8">8x</option>
        <option value="16">16x</option>
        <option value="32">32x</option>
        <option value="64">64x</option>
        <option value="128">128x</option>
      </select>
      <label for="timeline">Timeline</label>
      <input id="timeline" type="range" min="0" max="0" value="0" />
      <span id="timeLabel" style="margin-left:8px; color:var(--muted); font-size:12px;">t=0.0s</span>
    </div>
  </div>
  <div class="layout">
    <section class="card">
      <div class="plots">
        <div id="flight3d"></div>
        <div class="metric-panel">
          <div class="metric-header">
            <label for="metricSelect">Lower Plot</label>
            <select id="metricSelect">
              <option value="altitude" selected>Altitude</option>
              <option value="speed">Speed</option>
              <option value="battery">Battery</option>
              <option value="waypoint_distance">Waypoint Distance</option>
              <option value="roll_tracking">Roll Tracking</option>
              <option value="yaw_turn_response">Yaw/Turn Response</option>
              <option value="control_saturation">Control Saturation</option>
              <option value="servo_current_comparison">Servo Current Comparison</option>
              <option value="battery_servo_load">Battery + Servo Load</option>
              <option value="aileron_left">Left Aileron</option>
              <option value="aileron_right">Right Aileron</option>
              <option value="elevon_left">Left Elevon</option>
              <option value="elevon_right">Right Elevon</option>
              <option value="rudder">Rudder</option>
            </select>
          </div>
          <div id="metricPlot"></div>
        </div>
      </div>
    </section>
    <aside class="card telemetry">
      <h2>Telemetry Window</h2>
      <div class="attitude-wrap">
        <div class="attitude">
          <div class="horizon" id="horizon">
            <div class="sky"></div>
            <div class="ground"></div>
            <div class="horizon-line"></div>
          </div>
          <div class="wings"></div>
          <div class="nose"></div>
        </div>
        <div class="heading-tag" id="headingTag">HDG ---</div>
      </div>
      <div class="telemetry-grid">
        <div><div class="k">Latitude</div><div class="v" id="lat">-</div></div>
        <div><div class="k">Longitude</div><div class="v" id="lon">-</div></div>
        <div><div class="k">Alt MSL (m)</div><div class="v" id="altMsl">-</div></div>
        <div><div class="k">Alt AGL (m)</div><div class="v" id="altAgl">-</div></div>
        <div><div class="k">Roll (deg)</div><div class="v" id="roll">-</div></div>
        <div><div class="k">Pitch (deg)</div><div class="v" id="pitch">-</div></div>
        <div><div class="k">Yaw (deg)</div><div class="v" id="yaw">-</div></div>
        <div><div class="k">State</div><div class="v" id="state">-</div></div>
        <div><div class="k">Control Mode</div><div class="v" id="mode">-</div></div>
        <div><div class="k">Event</div><div class="v" id="event">-</div></div>
        <div><div class="k">Groundspeed (m/s)</div><div class="v" id="gs">-</div></div>
        <div><div class="k">Airspeed (m/s)</div><div class="v" id="as">-</div></div>
        <div><div class="k">Battery (V)</div><div class="v" id="battV">-</div></div>
      </div>
    </aside>
  </div>

  <script>
    const fmt = (v, digits=2) => (v === null || v === undefined || Number.isNaN(v)) ? "-" : Number(v).toFixed(digits);
    const text = (v) => (v === null || v === undefined || v === "") ? "-" : String(v);
    const asNum = (v) => (v === null || v === undefined || Number.isNaN(v)) ? null : Number(v);

    const ids = {{
      lat: document.getElementById("lat"),
      lon: document.getElementById("lon"),
      altMsl: document.getElementById("altMsl"),
      altAgl: document.getElementById("altAgl"),
      roll: document.getElementById("roll"),
      pitch: document.getElementById("pitch"),
      yaw: document.getElementById("yaw"),
      state: document.getElementById("state"),
      mode: document.getElementById("mode"),
      event: document.getElementById("event"),
      gs: document.getElementById("gs"),
      as: document.getElementById("as"),
      battV: document.getElementById("battV"),
      horizon: document.getElementById("horizon"),
      headingTag: document.getElementById("headingTag"),
    }};

    const playBtn = document.getElementById("playBtn");
    const speedSel = document.getElementById("speed");
    const metricSel = document.getElementById("metricSelect");
    const timeline = document.getElementById("timeline");
    const timeLabel = document.getElementById("timeLabel");
    const pageTitleEl = document.getElementById("pageTitle");

    let telemetry = [];
    let waypoints = [];
    let frame = 0;
    let playing = false;
    let lastTick = 0;
    let speed = 1.0;
    let currentMetric = "altitude";
    let currentMetricSeries = 1;

    const metricConfigs = {{
      altitude: {{
        title: "Altitude AGL vs Time",
        yTitle: "Altitude AGL (m)",
        series: [
          {{ key: "alt_agl_m", label: "Altitude AGL", color: "#9ca3af" }},
        ],
      }},
      speed: {{
        title: "Speed vs Time",
        yTitle: "Speed (m/s)",
        series: [
          {{ key: "gps_groundspeed_mps", label: "Groundspeed", color: "#38bdf8" }},
          {{ key: "airspeed_mps", label: "Airspeed", color: "#f59e0b" }},
        ],
      }},
      battery: {{
        title: "Battery vs Time",
        yTitle: "Battery Voltage (V)",
        series: [
          {{ key: "battery_voltage_v", label: "Battery Voltage", color: "#f43f5e" }},
          {{ key: "battery_current_a", label: "Battery Current", color: "#22c55e" }},
        ],
      }},
      waypoint_distance: {{
        title: "Waypoint Distance vs Time",
        yTitle: "Distance (m)",
        series: [
          {{ key: "active_waypoint_dist_m", label: "Active Waypoint Distance", color: "#f59e0b" }},
        ],
      }},
      roll_tracking: {{
        title: "Roll Tracking",
        yTitle: "Roll (deg)",
        series: [
          {{ key: "roll_setpoint_deg", label: "Roll Setpoint", color: "#60a5fa" }},
          {{ key: "roll_deg", label: "Roll Actual", color: "#f97316" }},
        ],
      }},
      yaw_turn_response: {{
        title: "Yaw/Turn Response",
        yTitle: "Degrees",
        series: [
          {{ key: "yaw_rate_dps", label: "Yaw Rate (deg/s)", color: "#38bdf8" }},
          {{ key: "yaw_deg", label: "Yaw Angle (deg)", color: "#a78bfa" }},
        ],
      }},
      control_saturation: {{
        title: "Control Saturation Flags",
        yTitle: "Flag",
        series: [
          {{ key: "controller_saturation_roll", label: "Roll Saturation", color: "#ef4444" }},
          {{ key: "controller_saturation_pitch", label: "Pitch Saturation", color: "#eab308" }},
        ],
      }},
      servo_current_comparison: {{
        title: "Aileron Servo Current Comparison",
        yTitle: "Current (A)",
        series: [
          {{ key: "servo_aileron_left_current_a", label: "Left Aileron Current", color: "#22c55e" }},
          {{ key: "servo_aileron_right_current_a", label: "Right Aileron Current", color: "#60a5fa" }},
        ],
      }},
      battery_servo_load: {{
        title: "Battery + Servo Load",
        yTitle: "Mixed Units (V / A)",
        series: [
          {{ key: "battery_voltage_v", label: "Battery Voltage (V)", color: "#f43f5e" }},
          {{ key: "servo_bus_current_a", label: "Servo Bus Current (A)", color: "#22c55e" }},
        ],
      }},
      aileron_left: {{
        title: "Left Aileron Command vs Actual",
        yTitle: "Deflection (deg)",
        series: [
          {{ key: "aileron_left_cmd_deg", label: "Cmd", color: "#60a5fa" }},
          {{ key: "aileron_left_actual_deg", label: "Actual", color: "#f97316" }},
        ],
      }},
      aileron_right: {{
        title: "Right Aileron Command vs Actual",
        yTitle: "Deflection (deg)",
        series: [
          {{ key: "aileron_right_cmd_deg", label: "Cmd", color: "#60a5fa" }},
          {{ key: "aileron_right_actual_deg", label: "Actual", color: "#f97316" }},
        ],
      }},
      elevon_left: {{
        title: "Left Elevon Command vs Actual",
        yTitle: "Deflection (deg)",
        series: [
          {{ key: "elevon_left_cmd_deg", label: "Cmd", color: "#60a5fa" }},
          {{ key: "elevon_left_actual_deg", label: "Actual", color: "#f97316" }},
        ],
      }},
      elevon_right: {{
        title: "Right Elevon Command vs Actual",
        yTitle: "Deflection (deg)",
        series: [
          {{ key: "elevon_right_cmd_deg", label: "Cmd", color: "#60a5fa" }},
          {{ key: "elevon_right_actual_deg", label: "Actual", color: "#f97316" }},
        ],
      }},
      rudder: {{
        title: "Rudder Command vs Actual",
        yTitle: "Deflection (deg)",
        series: [
          {{ key: "rudder_cmd_deg", label: "Cmd", color: "#60a5fa" }},
          {{ key: "rudder_actual_deg", label: "Actual", color: "#f97316" }},
        ],
      }},
    }};

    function updateAttitudeIndicator(rollDeg, pitchDeg, yawDeg) {{
      const roll = Number.isFinite(rollDeg) ? rollDeg : 0;
      const pitch = Number.isFinite(pitchDeg) ? pitchDeg : 0;
      const yaw = Number.isFinite(yawDeg) ? yawDeg : 0;
      const pitchPx = Math.max(-45, Math.min(45, pitch)) * 1.5;
      ids.horizon.style.transform = `translateY(${{pitchPx}}px) rotate(${{-roll}}deg)`;
      const hdg = ((yaw % 360) + 360) % 360;
      ids.headingTag.textContent = `HDG ${{String(Math.round(hdg)).padStart(3, "0")}}`;
    }}

    function updateTelemetryPanel(r) {{
      ids.lat.textContent = fmt(r.lat_deg, 6);
      ids.lon.textContent = fmt(r.lon_deg, 6);
      ids.altMsl.textContent = fmt(r.alt_msl_m, 2);
      ids.altAgl.textContent = fmt(r.alt_agl_m, 2);
      ids.roll.textContent = fmt(r.roll_deg, 2);
      ids.pitch.textContent = fmt(r.pitch_deg, 2);
      ids.yaw.textContent = fmt(r.yaw_deg, 2);
      ids.state.textContent = text(r.vehicle_state);
      ids.mode.textContent = text(r.control_mode);
      ids.event.textContent = text(r.event_marker);
      ids.gs.textContent = fmt(r.gps_groundspeed_mps, 2);
      ids.as.textContent = fmt(r.airspeed_mps, 2);
      ids.battV.textContent = fmt(r.battery_voltage_v, 3);
      updateAttitudeIndicator(r.roll_deg, r.pitch_deg, r.yaw_deg);
      timeLabel.textContent = `t=${{fmt(r.time_s, 1)}}s`;
    }}

    function buildMetricPlot(metricKey) {{
      currentMetric = metricKey;
      const cfg = metricConfigs[metricKey];
      const t = telemetry.map(r => r.time_s);
      currentMetricSeries = cfg.series.length;

      const traces = [];
      for (const s of cfg.series) {{
        traces.push({{
          type: "scattergl",
          mode: "lines",
          name: s.label,
          x: t,
          y: telemetry.map(r => asNum(r[s.key])),
          line: {{ color: s.color, width: 2 }},
        }});
      }}
      for (const s of cfg.series) {{
        const r0 = telemetry[Math.min(frame, telemetry.length - 1)];
        traces.push({{
          type: "scatter",
          mode: "markers",
          name: `${{s.label}} current`,
          x: [r0.time_s],
          y: [asNum(r0[s.key])],
          marker: {{ color: s.color, size: 8 }},
          showlegend: false,
          hoverinfo: "skip",
        }});
      }}

      const layout = {{
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        margin: {{ l: 55, r: 22, t: 26, b: 40 }},
        title: {{ text: cfg.title, font: {{ color: "#d8e6ff", size: 13 }} }},
        xaxis: {{ title: "Time (s)", color: "#cdd9f5", gridcolor: "#2a3a5f" }},
        yaxis: {{ title: cfg.yTitle, color: "#cdd9f5", gridcolor: "#2a3a5f" }},
        legend: {{ orientation: "h", x: 0.01, y: 0.99, font: {{ color: "#cbd6f6" }} }},
      }};

      Plotly.react("metricPlot", traces, layout, {{ responsive: true, displaylogo: false }});
    }}

    function renderFrame(i) {{
      frame = Math.max(0, Math.min(i, telemetry.length - 1));
      const path = telemetry.slice(0, frame + 1);
      const r = telemetry[frame];

      Plotly.restyle("flight3d", {{
        x: [path.map(p => p.pos_n_m)],
        y: [path.map(p => p.pos_e_m)],
        z: [path.map(p => p.alt_agl_m)],
      }}, [2]);

      Plotly.restyle("flight3d", {{
        x: [[r.pos_n_m]],
        y: [[r.pos_e_m]],
        z: [[r.alt_agl_m]],
      }}, [3]);

      const markerIndices = [];
      const xUpdates = [];
      const yUpdates = [];
      for (let s = 0; s < currentMetricSeries; s += 1) {{
        const traceIndex = currentMetricSeries + s;
        const key = metricConfigs[currentMetric].series[s].key;
        markerIndices.push(traceIndex);
        xUpdates.push([r.time_s]);
        yUpdates.push([asNum(r[key])]);
      }}
      Plotly.restyle("metricPlot", {{ x: xUpdates, y: yUpdates }}, markerIndices);

      timeline.value = String(frame);
      updateTelemetryPanel(r);
    }}

    function animate(ts) {{
      if (!playing) return;
      if (!lastTick) lastTick = ts;
      const dt = (ts - lastTick) / 1000.0;
      lastTick = ts;
      const step = Math.max(1, Math.floor(speed * dt * 40));
      let next = frame + step;
      if (next >= telemetry.length) {{
        next = telemetry.length - 1;
        playing = false;
        playBtn.textContent = "Play";
      }}
      renderFrame(next);
      if (playing) requestAnimationFrame(animate);
    }}

    function setup3dPlot() {{
      const fullX = telemetry.map(r => r.pos_n_m);
      const fullY = telemetry.map(r => r.pos_e_m);
      const fullZ = telemetry.map(r => r.alt_agl_m);

      const wpHit = waypoints.filter(w => w.hit_actual_flag === 1);
      const wpMiss = waypoints.filter(w => w.hit_actual_flag === 0);

      const data = [
        {{
          type: "scatter3d",
          mode: "markers",
          name: "Waypoint Hit",
          x: wpHit.map(w => w.x), y: wpHit.map(w => w.y), z: wpHit.map(w => w.z),
          customdata: wpHit.map(w => w.waypoint_id),
          marker: {{
            size: 5,
            color: "#22c55e",
            symbol: "diamond",
          }},
          hovertemplate: "Waypoint %{{customdata}}<br>Status Hit<br>N %{{x:.1f}}m<br>E %{{y:.1f}}m<br>Alt %{{z:.1f}}m<extra></extra>",
        }},
        {{
          type: "scatter3d",
          mode: "markers",
          name: "Waypoint Missed",
          x: wpMiss.map(w => w.x), y: wpMiss.map(w => w.y), z: wpMiss.map(w => w.z),
          customdata: wpMiss.map(w => w.waypoint_id),
          marker: {{
            size: 5,
            color: "#ef4444",
            symbol: "diamond",
          }},
          hovertemplate: "Waypoint %{{customdata}}<br>Status Missed<br>N %{{x:.1f}}m<br>E %{{y:.1f}}m<br>Alt %{{z:.1f}}m<extra></extra>",
        }},
        {{
          type: "scatter3d",
          mode: "lines",
          name: "Path",
          x: [fullX[0]], y: [fullY[0]], z: [fullZ[0]],
          line: {{ color: "#4cc9f0", width: 5 }},
        }},
        {{
          type: "scatter3d",
          mode: "markers",
          name: "vehicle",
          x: [fullX[0]], y: [fullY[0]], z: [fullZ[0]],
          marker: {{ color: "#facc15", size: 5 }},
        }},
      ];

      const layout = {{
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        margin: {{ l: 0, r: 0, t: 6, b: 0 }},
        scene: {{
          xaxis: {{ title: "North (m)", color: "#cdd9f5", gridcolor: "#2a3a5f" }},
          yaxis: {{ title: "East (m)", color: "#cdd9f5", gridcolor: "#2a3a5f" }},
          zaxis: {{ title: "Altitude AGL (m)", color: "#cdd9f5", gridcolor: "#2a3a5f" }},
          bgcolor: "rgba(0,0,0,0)",
          aspectmode: "data",
          camera: {{ eye: {{ x: 1.4, y: -1.6, z: 0.9 }} }},
        }},
        legend: {{ orientation: "h", x: 0.01, y: 0.99, font: {{ color: "#cbd6f6" }} }},
      }};

      Plotly.newPlot("flight3d", data, layout, {{ responsive: true, displaylogo: false }});
    }}

    async function init() {{
      const [telemRes, planRes] = await Promise.all([
        fetch("/api/telemetry"),
        fetch("/api/flight-plan"),
      ]);
      telemetry = await telemRes.json();
      waypoints = await planRes.json();
      if (!Array.isArray(telemetry) || telemetry.length === 0) {{
        throw new Error("No telemetry data loaded");
      }}
      const headerFlight = telemetry[0].flight_id || "UNKNOWN_FLIGHT";
      const headerVehicle = telemetry[0].vehicle_id || "UNKNOWN_VEHICLE";
      pageTitleEl.textContent = `${{headerFlight}} | ${{headerVehicle}}`;

      timeline.max = String(telemetry.length - 1);
      setup3dPlot();
      buildMetricPlot(currentMetric);
      renderFrame(0);

      playBtn.onclick = () => {{
        playing = !playing;
        playBtn.textContent = playing ? "Pause" : "Play";
        lastTick = 0;
        if (playing) requestAnimationFrame(animate);
      }};

      speedSel.onchange = () => {{
        speed = Number(speedSel.value || "1");
      }};

      metricSel.onchange = () => {{
        buildMetricPlot(metricSel.value || "altitude");
        renderFrame(frame);
      }};

      timeline.oninput = () => {{
        renderFrame(Number(timeline.value || "0"));
      }};
    }}

    init().catch((err) => {{
      console.error(err);
      alert("Failed to load telemetry viewer data: " + err.message);
    }});
  </script>
</body>
</html>
"""


def make_handler(telemetry_data: List[Dict[str, object]], flight_plan_data: List[Dict[str, object]], title: str):
    html = build_html(title)
    telemetry_json = json.dumps(telemetry_data, separators=(",", ":"))
    flight_plan_json = json.dumps(flight_plan_data, separators=(",", ":"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                body = html.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/telemetry":
                body = telemetry_json.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/flight-plan":
                body = flight_plan_json.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run a localhost telemetry web viewer")
    ap.add_argument("--telemetry", default="telemetry.csv", help="Path to telemetry CSV")
    ap.add_argument("--flight-plan", default="flight_plan.csv", help="Path to flight_plan CSV")
    ap.add_argument("--stride", type=int, default=50, help="Keep every Nth telemetry row")
    ap.add_argument("--max-points", type=int, default=60000, help="Cap loaded telemetry rows (0 = no cap)")
    ap.add_argument("--host", default="127.0.0.1", help="Bind host")
    ap.add_argument("--port", type=int, default=8765, help="Bind port")
    ap.add_argument("--title", default="Glide Telemetry Viewer", help="Page title")
    ap.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    return ap


def resolve_input_path(path_arg: str, script_dir: Path) -> Path:
    p = Path(path_arg).expanduser()
    if p.is_absolute():
        return p.resolve()
    cwd_candidate = p.resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (script_dir / p).resolve()


def main() -> None:
    args = build_parser().parse_args()
    script_dir = Path(__file__).resolve().parent
    telemetry_path = resolve_input_path(args.telemetry, script_dir)
    flight_plan_path = resolve_input_path(args.flight_plan, script_dir)
    if not telemetry_path.exists():
        fail(f"telemetry CSV not found: {telemetry_path}")
    if not flight_plan_path.exists():
        fail(f"flight plan CSV not found: {flight_plan_path}")

    telemetry_data = load_telemetry(telemetry_path, stride=args.stride, max_points=args.max_points)
    flight_plan_data = load_flight_plan(flight_plan_path)

    handler_cls = make_handler(telemetry_data, flight_plan_data, args.title)
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    except OSError as exc:
        fail(f"could not bind {args.host}:{args.port} ({exc.strerror or str(exc)}). Try --port 8766")
    url = f"http://{args.host}:{args.port}/"
    print(f"Serving telemetry viewer at {url}")
    print(f"Telemetry source: {telemetry_path}")
    print(f"Flight plan source: {flight_plan_path}")

    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Server stopped.")


if __name__ == "__main__":
    main()
