# Executive Handoff Document: Mach Operations Engineering Assessment

**Candidate:** Andrea Lukas  
**Repository:** [`avlukas04/mach-ops-eng-assessment`](https://github.com/avlukas04/mach-ops-eng-assessment)  
**Target Next Steps:** Draft `REPORT.md` (Part 1 Written Deliverable) $\rightarrow$ Build `spc_detector.py` & Dashboard (Part 2)

---

## 1. Background, Assessment Context & Philosophy

### Context & Goals
- **Role:** Operations Engineer at Mach (autonomous defense/aerospace startup).
- **Core Prompt:** An uncrewed aircraft (`GLDR-0421`) failed an unpowered glide test after release from altitude. We must:
  1. **Part 1:** Conduct an aerospace-grade root cause analysis to prove whether the failure was environmental, pilot/firmware, design, or a manufacturing defect.
  2. **Part 2:** Trace the defect to `manufacturing.csv`, explain why the existing quality process allowed it to escape, build an automated statistical detection method, and develop an interactive production health dashboard.
- **Guidance from Head of Ops (Andrew):**
  - **Process First, Tools Second:** Build clean, structured data processes before building tools. Avoid premature AI/scraping as a band-aid.
  - **The 80/20 Rule:** Deliver high-leverage, reliable solutions that capture 80% of value with 20% of effort (e.g., deterministic Statistical Process Control) rather than over-engineered models that never ship.
  - **Audience:** Manufacturing Floor Leads (throughput/yields), Quality Engineers (drift/distributions), and Flight Test Engineers (pre-flight safety clearance).

---

## 2. Key Conceptual Principles & Reviewed Questions

### A. Aerospace Root Cause Investigation (from `docs/root_cause_analysis.md`)
- **Methodology Before Data:** Standard aerospace protocol (FAA / NTSB / NASA) mandates brainstorming all possible hypotheses across the Ishikawa (Fishbone) 6Ms (Man, Machine, Method, Material, Measurement, Environment) **before** inspecting data to avoid confirmation bias.
- **Hypothesis Elimination:**
  - *Weather/Environment:* Winds and turbulence were within benign operational limits.
  - *Software/Guidance:* Autopilot correctly issued roll commands at Waypoint 2.
  - *Aerodynamic Design:* Other surfaces responded normally; aircraft was trimmed.
  - *Root Failure:* Mechanical actuator lag on the **left aileron servo**.

### B. Statistical Process Control (SPC) & The $\pm 3\sigma$ Standard (from `docs/reviewed_questions_answers.md`)
- **What is SPC?** A mathematical framework (Walter Shewhart, Bell Labs) establishing that identical manufactured parts follow a stable normal distribution. Measurements falling far into the tails indicate *assignable causes* of error.
- **Why $\pm 3\sigma$?** In a Gaussian distribution, $99.73\%$ of parts fall within $\pm 3\sigma$. Only $0.27\%$ fall outside by chance. It is the gold standard Shewhart control limit across aerospace and precision manufacturing.
- **Role of SPC:** SPC is an **alarm system**, not the mechanic. It flags that an actuator is statistically abnormal so technicians can quarantine it before assembly.

### C. The Quality Control Process Gap
- **The Binary Trap:** Factory bench testing only checked for binary firmware fault codes. GLDR-0421's servo completed the sweep without throwing an error code, so the system stamped `PASS`.
- **The Parametric Reality:** The test recorded `332.70 mA` of sweep current into `manufacturing.csv`, but never evaluated the number against statistical upper limits.
- **100% Pass Rate Red Flag:** Across all 200,000 units in `manufacturing.csv`, `final_qc_pass = 1` for **100% of vehicles** (0 rejections). In manufacturing, a 100% yield across 200,000 units is proof of an inactive or ineffective quality gate.
- **The Rework Trap:** Unit record shows rework code `R05` (*"Updated firmware + parameter profile; repeated self-test"*). Technicians reflashed firmware to clear a self-test anomaly, masking the underlying physical stiffness.

### D. Pragmatic Use of AI / ML
- **Where AI Fits:** High-level narrative drafting and assistance (encouraged).
- **Where AI Does Not Belong:** Core factory screening rules. Safety-critical quality gates require deterministic, auditable statistics ($Z = \frac{x - \mu}{\sigma}$).
- **Roadmap / Phase 2:** Multivariate outlier detection (e.g., Mahalanobis Distance, Isolation Forest) for joint-metric anomalies.

---

## 3. Hard Findings & Quantitative Evidence

### 1. Flight Telemetry (`telemetry.csv` & `flight_plan.csv`)
- **Phase 1 (Release $\rightarrow$ WP2):** Stable flight, heading $045^\circ$, altitude $1,200\text{ m} \rightarrow 980\text{ m}$.
- **Phase 2 (WP2 Turn Anomaly):** At Waypoint 2 ($t \approx 84\text{ s}$), autopilot commanded a right turn toward Waypoint 3.
  - **Left Aileron Command:** Commanded deflection to roll the aircraft.
  - **Left Aileron Actual:** Actuator experienced severe slew rate lag and stalled at $-4.2^\circ$ (demanded $+12^\circ$).
  - **Result:** Left wing remained pinned down; aircraft entered an unrecoverable left spiral dive, missed Waypoints 3–5, and impacted terrain prematurely.

### 2. Factory Build Data (`manufacturing.csv`)
- **Aircraft ID:** `GLDR-0421` (Serial: `SN-12661455`, Built: 2026-02-06, `HB-PLANT-1`, Line 3, Shift B).
- **Defective Component:** Left Aileron Servo (ActuMax SM-14, Serial: `SV-65031081`, Lot: `L2026-07B`).
- **Fleet Statistics (Fleet Size: 200,000 Units):**
  - **Sweep Current:** **332.70 mA** ($\mu = 185.12$, $\sigma = 24.89$) $\rightarrow$ **$Z = +5.93\sigma$** (1 in 200 million event)
  - **Peak Current:** **400.32 mA** ($\mu = 260.08$, $\sigma = 34.59$) $\rightarrow$ **$Z = +4.05\sigma$**
  - **Sweep Time:** **940.89 ms** ($\mu = 780.04$, $\sigma = 55.04$) $\rightarrow$ **$Z = +2.92\sigma$**
  - **Position Error RMS:** **1.13°** ($\mu = 0.55$, $\sigma = 0.19$) $\rightarrow$ **$Z = +2.98\sigma$**
  - **Other 4 Control Surfaces (Right Aileron, Elevon L/R, Rudder):** All $|Z| < 2.1\sigma$ (completely nominal).

---

## 4. Current State of the Codebase

The repository is synchronized at [`avlukas04/mach-ops-eng-assessment`](https://github.com/avlukas04/mach-ops-eng-assessment):

```text
andrea_lukas_ops_eng_assessment/
├── README.md                                # Assessment brief
├── .gitignore                               # Ignores large telemetry.csv & manufacturing.csv
├── flight_plan.csv                          # Intended waypoint targets
├── telemetry-simulation.py                  # Local telemetry visualizer
├── analysis/
│   ├── flight_investigation.py              # Telemetry analysis script (outputs Fig 1-3)
│   └── manufacturing_analysis.py            # Fleet SPC analysis script (outputs Fig 4-5)
├── docs/
│   ├── HANDOFF_SUMMARY.md                   # Full context, methodology & findings handoff
│   ├── deliverable_plan.md                  # Comprehensive assessment plan & Part 2 strategy
│   ├── root_cause_analysis.md               # 6M Ishikawa, 5 Whys, hypothesis matrix
│   └── reviewed_questions_answers.md        # Detailed Q&A on SPC, QC gaps, AI, and architecture
└── outputs/
    └── figures/
        ├── fig1_aileron_seizure.png         # Telemetry command vs actual divergence
        ├── fig2_waypoint_misses.png         # Planned vs actual GPS path
        ├── fig3_altitude_profile.png        # Flight altitude and glide slope
        ├── fig4_mfg_distributions.png       # Fleet histogram highlighting GLDR-0421 (+5.93σ)
        └── fig5_zscore_radar.png            # 5-surface actuator Z-score radar plot
```

---

## 5. Immediate Action Plan for Claude Code

### Phase 1: Write `REPORT.md` (Part 1 Deliverable)
Draft the formal written summary required by README Deliverable #1:
1. **Executive Summary & Root Cause Determination:** Clear statement attributing failure to ActuMax SM-14 servo gearbox friction on GLDR-0421.
2. **Flight Investigation & Telemetry Evidence:** Link and explain Figures 1, 2, and 3.
3. **Manufacturing Investigation & Evidence:** Link and explain Figures 4 and 5; detail the $+5.93\sigma$ Z-score.
4. **Quality Process Gap Analysis:** Explain the 100% pass rate anomaly, the R05 rework trap, and absence of parametric limits.
5. **Proposed Prevention & Detection Approach:** Outline the Shewhart $\pm 3\sigma$ gate across all 25 actuator metrics.

### Phase 2: Implement Automated Detection (`spc_detector.py`)
- Python CLI utility that reads `manufacturing.csv` (or single vehicle records), evaluates all 25 metrics against baseline means and standard deviations, and outputs a JSON/CSV verdict (`PASS`, `FLAG`, `QUARANTINE`).

### Phase 3: Build Interactive Production Health Dashboard
- Lightweight web application (Python backend + Vanilla HTML/Plotly.js frontend).
- **Tab 1 (Fleet Overview):** Factory KPIs, pass/flag yields, plant/shift trends.
- **Tab 2 (SPC Control Charts):** Distribution histograms and control charts with $\pm 3\sigma$ thresholds.
- **Tab 3 (Vehicle Lookup):** Single vehicle lookup (e.g., search `GLDR-0421`) showing status breakdown across all 25 metrics.
