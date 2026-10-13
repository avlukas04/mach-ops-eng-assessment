# Assessment Deliverable Plan & Part 2 Build Strategy

## 1. Deliverable Structure — Organizing Everything

The README asks for three deliverables. Here's how to organize your project directory:

```
andrea_lukas_ops_eng_assessment/
│
├── README.md                          # (Provided — the assignment)
├── telemetry.csv                      # (Provided — 600K rows flight data)
├── flight_plan.csv                    # (Provided — 21 waypoints)
├── manufacturing.csv                  # (Provided — 200K vehicles)
├── telemetry-simulation.py            # (Provided — 3D viewer)
│
├── REPORT.md                          # ← DELIVERABLE 1: Your written findings
│                                      #    (concise, answer-first, data-backed)
│
├── analysis/                          # ← Your Part 1 investigation work
│   ├── flight_investigation.py        #    Telemetry analysis scripts
│   ├── manufacturing_analysis.py      #    Mfg data analysis scripts
│   └── figures/                       #    Generated plots/charts
│
├── detector/                          # ← DELIVERABLE 2a: Automated defect detector
│   ├── spc_detector.py                #    SPC anomaly detection engine
│   ├── config.py                      #    Thresholds, constants, feature lists
│   └── run_detector.py                #    CLI entry point
│
├── dashboard/                         # ← DELIVERABLE 2b: Live monitoring dashboard
│   ├── server.py                      #    Python backend (FastAPI or Flask)
│   ├── static/                        #    Frontend assets
│   │   ├── index.html
│   │   ├── style.css
│   │   └── app.js
│   └── data/                          #    Processed data / cache
│
└── INSTRUCTIONS.md                    # ← DELIVERABLE 3: How to run everything
```

---

## 2. REPORT.md — What to Write (and How Much)

Andrew specifically asked for **concise**. The Minto Pyramid / McKinsey communication style is:

> **Answer first.** Then supporting evidence. Then methodology. Not the reverse.

### Recommended Structure

```markdown
# Flight Anomaly Investigation & Manufacturing Quality Report

## Executive Summary (5 sentences max)
- Root cause: Left aileron servo mechanical seizure, manufacturing defect
- One line on the evidence
- One line on why QC missed it
- One line on your automated solution
- One line on the dashboard

## Part 1: Flight Investigation
### Finding
### Evidence (with 2-3 key charts)
### Failure Chain (timeline)

## Part 2: Manufacturing Detection
### Relevant Signals Identified
### Why QC Failed to Detect
### Automated Detection Method
### Dashboard Overview

## Appendix
### How to Run the Tools
### Technical Notes
```

> [!TIP]
> **Target length:** 3-5 pages. Andrew will skim the executive summary and then jump to the tools. The report exists to prove rigor, not to impress with volume.

---

## 3. Part 1 → Part 2 Bridge: What Your Investigation Already Proved

Your Part 1 root cause analysis directly hands you the Part 2 requirements:

| Part 1 Finding | Part 2 Action |
|:---|:---|
| Left aileron servo drew **332.7 mA** sweep current (+5.86σ) | → Build SPC detector that flags any servo with sweep current > μ + 3σ |
| Peak current was **400.3 mA** (+4.06σ) | → Include peak current in multi-parameter screening |
| Sweep time was **940.9 ms** (+2.92σ) | → Include sweep time as anomaly signal |
| Position error RMS was **1.128°** (+3.05σ) | → Include position error as anomaly signal |
| Backdrive torque was **133.7 N·mm** (+2.37σ) | → Include backdrive torque as anomaly signal |
| QC had **zero rejections** across 200K vehicles | → Dashboard must show rejection/flag rates to make the gap visible |
| The **data already existed** in manufacturing.csv | → The fix is software (rules + alerting), not hardware |

### The 5 Key Signals in `manufacturing.csv`

For each of the 5 control surface positions (`aileron_l`, `aileron_r`, `elevon_l`, `elevon_r`, `rudder`), these columns contain the defect signal:

1. **`*_servo_sweep_current_ma`** — Total current during full sweep (HIGH = friction)
2. **`*_servo_peak_current_ma`** — Max transient current spike (HIGH = stall risk)
3. **`*_servo_sweep_time_ms`** — Time to complete full sweep (HIGH = sluggish/binding)
4. **`*_servo_backdrive_torque_nmm`** — Force to push the servo backwards (HIGH = stiff gears)
5. **`*_servo_position_error_rms_deg`** — Accuracy of reaching commanded angles (HIGH = imprecise)

These 5 metrics × 5 surfaces = **25 parametric health signals** per vehicle.

---

## 4. What to Build — The Two Tools

### Tool A: Automated SPC Defect Detector (Python CLI)

**What it does:** Takes a vehicle ID (or batch of vehicle IDs) from manufacturing.csv and returns a Go / No-Go / Quarantine verdict with Z-scores and rationale.

**Why this is the right 80/20 move:**
- It's the tool the QC station should run on every vehicle before stamping `final_qc_pass = 1`
- It requires zero new hardware — it reads the data the factory already collects
- It catches the exact defect that caused the crash

**Core algorithm (Statistical Process Control):**
```
For each vehicle:
  For each of 5 control surfaces:
    For each of 5 servo metrics:
      z_score = (vehicle_value - fleet_mean) / fleet_std
      if |z_score| > 3.0:  → FLAG as outlier
    
    composite_score = weighted combination of z-scores
    if composite_score > threshold:  → QUARANTINE this surface

  Overall verdict: PASS / FLAG / QUARANTINE
```

**Output example:**
```
═══════════════════════════════════════════════
  VEHICLE HEALTH REPORT: GLDR-0421
═══════════════════════════════════════════════
  Build Date: 2026-02-06   Site: HB-PLANT-1
  Line: LN3   Tech: T182   Inspector: Q30
───────────────────────────────────────────────
  LEFT AILERON  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  QUARANTINE
    Sweep Current:   332.7 mA  [Z = +5.86]  ⚠️
    Peak Current:    400.3 mA  [Z = +4.06]  ⚠️
    Sweep Time:      940.9 ms  [Z = +2.92]  ⚠️
    Backdrive Torque:133.7 Nmm [Z = +2.37]  ⚠️
    Position Error:  1.128 deg [Z = +3.05]  ⚠️
  
  RIGHT AILERON ████████████████  PASS
  LEFT ELEVON   ████████████████  PASS
  RIGHT ELEVON  ████████████████  PASS
  RUDDER        ████████████████  PASS
───────────────────────────────────────────────
  OVERALL VERDICT:  ⛔ QUARANTINE
  REASON: Left aileron servo exceeds 3σ on
          5/5 monitored parameters
═══════════════════════════════════════════════
```

---

### Tool B: Live Fleet Health Dashboard (Web App)

This is where your CS/DS skills really shine. The README says:

> *"build a live, monitorable dashboard... that surfaces production health across the fleet, flags at-risk units before shipment, provides clear visibility to production, quality, and engineering teams... as if it will operate continuously in a real production environment with a live connection to data."*

---

## 5. Dashboard Design — Inspiration & Research

### Real-World Models to Study

#### 1. NASA Mission Control Displays
- **Why it's relevant:** Multiple stakeholders (flight directors, systems engineers, range safety) all watching different aspects of the same system in real time
- **Key design principle:** **Status-at-a-glance with drill-down.** The top-level view shows green/yellow/red. You click to see the numbers underneath.
- **What to borrow:** Color-coded status tiles, time-series trend charts, critical-value callouts

#### 2. Toyota Production System "Andon Boards"
- **Why it's relevant:** Factory floor displays that show the health of every production line in real time. When a worker pulls the Andon cord (flags a defect), the board lights up and everyone knows which station has a problem.
- **Key design principle:** **Anyone walking past should understand the status in 2 seconds.** No training required.
- **What to borrow:** Big bold pass/fail/warning counts at the top. Line-by-line and shift-by-shift breakdowns.

#### 3. Grafana / Datadog Operational Dashboards
- **Why it's relevant:** This is what modern DevOps/SRE teams use to monitor production systems — your CS background maps directly to this.
- **Key design principle:** **Time-series SPC charts** (control charts with upper/lower bounds), alert panels, filterable drill-downs.
- **What to borrow:** Dark theme, metric cards with sparklines, histogram distributions, threshold lines on charts.

#### 4. Tesla Gigafactory Quality Dashboards (public talks / patents)
- **Why it's relevant:** High-volume manufacturing with per-unit parametric testing, statistical anomaly detection, and supplier lot tracing. Almost exactly this assessment's scenario.
- **Key design principle:** **Traceability.** Click a flagged vehicle → see its build record → see which lot/vendor/tech was involved → see if other vehicles from that lot are also flagged.
- **What to borrow:** Lot-level heatmaps, vendor comparison charts, technician yield comparisons.

---

## 6. Dashboard Architecture — Playing to Your CS/DS Strengths

### Recommended Tech Stack

| Layer | Technology | Why |
|:---|:---|:---|
| **Backend** | Python (FastAPI) | You know Python; FastAPI is fast, modern, auto-generates API docs. Lightweight enough to run on any laptop. |
| **Frontend** | HTML + Vanilla CSS + JavaScript | The assessment says deliver in "whatever form is easiest." A single-page app with vanilla JS is zero-dependency, runs anywhere, and you control every pixel. |
| **Charting** | Chart.js or Plotly.js | Interactive, beautiful charts. Chart.js is lighter; Plotly has better scientific/statistical chart types (box plots, histograms). |
| **Data** | manufacturing.csv loaded into memory | For the assessment, serve data from the CSV directly. In production you'd point at a database, but the architecture should make that swap trivial (just change the data source). |

### Simulating "Live" Data

The README says *"as if it will operate continuously in a real production environment."* You don't have a real database feed, but you can simulate one:

- **Option A (Recommended):** Your FastAPI backend reads `manufacturing.csv` on startup, computes all SPC metrics, and serves them via REST API. The frontend polls the API every N seconds. Add a note in your documentation: *"In production, replace the CSV reader with a database query or message queue consumer."*
- **Option B:** Simulate a data stream by having the backend "drip-feed" rows from the CSV as if vehicles are being built in real time, updating the dashboard live.

> [!TIP]
> **Option B is more impressive** for a demo because the dashboard visibly updates, but Option A is more practical and faster to build. Andrew's "80/20" instinct would favor Option A with a clear note about production architecture.

---

## 7. Dashboard Layout — The Three Views

Design for three personas (manufacturing floor lead, quality engineer, flight ops engineer):

### View 1: Fleet Overview (The "Andon Board")

```
┌─────────────────────────────────────────────────────────────────┐
│                    FLEET PRODUCTION HEALTH                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │  200,000 │  │  198,740 │  │    1,258 │  │        2 │       │
│  │  TOTAL   │  │  ✅ PASS  │  │  ⚠️ FLAG  │  │  ⛔ QUAR │       │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
│                                                                 │
│  ┌─── By Plant ──────────────────────────────────────────────┐ │
│  │  HB-PLANT-1:  ████████████████░░  98.7% pass   21 flags  │ │
│  │  OC-PLANT-2:  ███████████████░░░  97.9% pass   34 flags  │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                 │
│  ┌─── By Shift ──────────────────────────────────────────────┐ │
│  │  Shift A: 99.1%  │  Shift B: 98.8%  │  Shift C: 99.0%   │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                 │
│  ┌─── Recent Builds (last 100) ──────────────────────────────┐ │
│  │  ● ● ● ● ● ● ● ● ⚠ ● ● ● ● ● ● ● ● ● ● ● ● ● ⛔ ● ● │ │
│  │  (each dot = 1 vehicle, color = status)                   │ │
│  └───────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

**Audience:** Factory floor lead, VP of Manufacturing, Andrew
**Purpose:** "Is the factory healthy right now?" Answerable in 2 seconds.

---

### View 2: SPC Control Charts (The "Quality Engineer" View)

```
┌─────────────────────────────────────────────────────────────────┐
│  SERVO SWEEP CURRENT — LEFT AILERON (ALL VEHICLES)              │
│                                                                 │
│  350 ┤                                            ⛔ GLDR-0421 │
│      │                                            ●             │
│  300 ┤ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ USL (μ+3σ = 260 mA) │
│      │                                                          │
│  250 ┤           ·                                              │
│      │     · ·  · ·  · · ·   ·                                 │
│  200 ┤ ·····················●·●···●····●···· μ = 185 mA        │
│      │  ··· · ··· · ·· ···· ·  ··  ··· ···                     │
│  150 ┤       ·          ·                                       │
│      │                                                          │
│  100 ┤ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ LSL (μ-3σ = 110 mA) │
│      └──────────────────────────────────────────────────────    │
│        Build Date →                                             │
│                                                                 │
│  ┌─── Distribution ─────┐  ┌─── By Vendor ──────────────────┐ │
│  │   ▁▃▇█████▇▃▁        │  │ ActuMax:  185.0 ± 25.2 mA     │ │
│  │   Normal (σ=25.2)    │  │ Servodyne: 185.1 ± 25.0 mA    │ │
│  │   GLDR-0421 →|       │  │ AeroMotion: 185.0 ± 25.3 mA   │ │
│  └───────────────────────┘  └────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

**Audience:** Quality engineer, process engineer
**Purpose:** "Are my processes in control? Where are the outliers? Is a specific vendor or lot drifting?"

**Key SPC chart types to include:**
- **X-bar chart** (individual values over time with μ ± 3σ lines)
- **Histogram** (distribution of each metric across the fleet)
- **Pareto chart** (which metrics contribute most to flags)

---

### View 3: Vehicle Lookup (The "Flight Readiness" View)

```
┌─────────────────────────────────────────────────────────────────┐
│  VEHICLE HEALTH LOOKUP                                          │
│  ┌──────────────────────────────────────┐                       │
│  │  Enter Vehicle ID: [GLDR-0421    ] 🔍│                       │
│  └──────────────────────────────────────┘                       │
│                                                                 │
│  ┌─── GLDR-0421 ── VERDICT: ⛔ QUARANTINE ──────────────────┐  │
│  │                                                           │  │
│  │  Build: 2026-02-06  Site: HB-PLANT-1  Line: LN3          │  │
│  │  Tech: T182   Inspector: Q30   Rework: R05               │  │
│  │                                                           │  │
│  │  ┌─── Radar Chart ─────────┐  ┌─── Z-Scores ──────────┐ │  │
│  │  │      Sweep I            │  │                        │ │  │
│  │  │     ╱  ●  ╲             │  │  L-AIL Sweep I: +5.86σ │ │  │
│  │  │   ╱ ╱    ╲  ╲          │  │  L-AIL Peak I:  +4.06σ │ │  │
│  │  │ Err●───────●Peak       │  │  L-AIL Sweep T: +2.92σ │ │  │
│  │  │   ╲ ╲    ╱  ╱          │  │  L-AIL BkDrv:  +2.37σ │ │  │
│  │  │     ╲  ●  ╱             │  │  L-AIL PosErr: +3.05σ │ │  │
│  │  │     Sweep T             │  │                        │ │  │
│  │  │  ── Fleet Avg           │  │  All other surfaces:   │ │  │
│  │  │  ── This Vehicle        │  │  Within ±2σ (NOMINAL)  │ │  │
│  │  └─────────────────────────┘  └────────────────────────┘ │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**Audience:** Flight test engineer, program manager
**Purpose:** "Should I put this vehicle on the flight line? Show me exactly why or why not."

---

## 8. Build Priority — What to Code First

Follow Andrew's 80/20 rule. Ship value fast, iterate later:

| Priority | What | Time Estimate | Impact |
|:---|:---|:---|:---|
| **P0** | `REPORT.md` — Written findings | 2-3 hours | The thing they'll read first |
| **P1** | `spc_detector.py` — Batch fleet screening | 3-4 hours | Proves you can solve the problem programmatically |
| **P2** | Dashboard View 1 (Fleet Overview) | 3-4 hours | Visual proof of operational awareness |
| **P3** | Dashboard View 3 (Vehicle Lookup) | 2-3 hours | Interactive, impressive, directly tied to the crash |
| **P4** | Dashboard View 2 (SPC Charts) | 2-3 hours | Deepest technical value for quality engineers |
| **P5** | Polish, instructions, documentation | 1-2 hours | Makes it runnable and professional |

> [!IMPORTANT]
> **Build the detector BEFORE the dashboard.** The detector is the analytical core. The dashboard is the visualization layer on top. If you only had time for one, the detector proves you solved the problem. The dashboard proves you can communicate the solution to non-technical stakeholders.

---

## 9. Design Principles — What Makes a Dashboard "Effective"

From studying NASA, Toyota, Grafana, and modern manufacturing MES systems, the patterns that make dashboards trusted and used:

### 1. The "Glance Test" (Toyota Andon)
> If someone walks past the screen at 20 feet, can they tell if things are OK or not OK?

Use giant colored status tiles: green (pass), yellow (flag), red (quarantine). Numbers big enough to read across a factory floor.

### 2. Progressive Disclosure (NASA Flight Director Console)
> Show the answer first. Let the user drill down for evidence only if they want it.

Don't dump 25 Z-scores on the home page. Show "198,740 PASS / 1,258 FLAG / 2 QUARANTINE." Click a flagged vehicle to see why.

### 3. Trust Through Transparency (Grafana)
> Show the math. Don't just say "FAIL" — show the threshold line, the distribution, and where this data point falls.

SPC charts with μ and ±3σ lines visually prove the system isn't arbitrary. Engineers will trust it because they can see the statistics.

### 4. Actionability (Tesla Manufacturing)
> Every screen element should answer: "So what? What do I do next?"

A flag without a next step is noise. Each flagged vehicle should show: which surface, which metric, which vendor/lot, and a recommended action (quarantine, re-test, escalate to supplier).

### 5. Dark Theme
> Not just aesthetics — factory floors, flight ops centers, and NOCs universally use dark backgrounds because they reduce eye strain on wall-mounted monitors running 24/7 and make color-coded status indicators pop.
