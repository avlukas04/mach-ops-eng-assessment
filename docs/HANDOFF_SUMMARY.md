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
- **Why $\pm 3\sigma$?** In a Gaussian distribution, $99.73\%$ of parts fall within $\pm 3\sigma$. Only $0.27\%$ fall outside by chance. It is the standard Shewhart limit *for a single chart*.
- **Gate design (decided 2026-10-13, see REPORT.md §6.1):** two tiers, scored on a *robust, high-side* basis.
  - Baseline per metric: median and σ̂ = IQR/1.349 (frozen). z = max(0, (x − median)/σ̂). Reason: all metrics are clipped at an instrument floor (and backdrive at 150 N·mm, sweep time at 980 ms), which shrinks the plain σ and inflated position-error Z. Skew is negligible (≤ 0.2); clipping was the actual problem.
  - Bench metrics are **uncorrelated** (|r| ≤ 0.01), so a combined score per servo D = √Σz² over its 5 metrics has a known null distribution.
  - **QUARANTINE:** any servo D ≥ 6.0 OR any z ≥ 5.5 (each < 0.1 expected false alarms per 200k) → exactly **2 units: GLDR-0421 (D = 8.57), GLDR-200000 (D = 7.31)**; next-highest servo is 5.80.
  - **FLAG:** any z ≥ 3, D ≥ 4.5, or saturated reading → 6,498 (3.25%). That's about the chance rate (~6,640 expected), so FLAG drives lot/line/shift p-charts, not per-unit holds. 731 vehicles have a saturated reading (tag `SATURATED`).
  - Superseded: single |Z| ≥ 4.5 gate (14 units, 12 of them position-error-only and consistent with chance).
- **Role of SPC:** SPC is an **alarm system**, not the mechanic. It flags that an actuator is statistically abnormal so technicians can quarantine it before assembly.

### C. The Quality Control Process Gap
- **The Binary Trap:** Factory bench testing only checked for binary firmware fault codes. GLDR-0421's servo completed the sweep without throwing an error code, so the system stamped `PASS`.
- **The Parametric Reality:** The test recorded `332.70 mA` of sweep current into `manufacturing.csv`, but never evaluated the number against statistical upper limits.
- **100% Pass Rate Red Flag:** Across all 200,000 units in `manufacturing.csv`, `final_qc_pass = 1` for **100% of vehicles** (0 rejections). In manufacturing, a 100% yield across 200,000 units is proof of an inactive or ineffective quality gate.
- **The Rework Gap:** Unit record shows rework code `R05` (*"Updated firmware + parameter profile; repeated self-test"*). R05 is routine (~9,600 units), so it is not evidence of masking by itself — but it was a missed opportunity: a firmware reflash + self-test verifies software, not mechanical health, and no parametric re-check followed.
- **Logged exceptions don't gate:** 32,296 units with `qc_exception_count > 0` still received `final_qc_pass = 1`.

### D. Pragmatic Use of AI / ML
- **Where AI Fits:** High-level narrative drafting and assistance (encouraged).
- **Where AI Does Not Belong:** Core factory screening rules. Safety-critical quality gates require deterministic, auditable statistics ($Z = \frac{x - \mu}{\sigma}$).
- **Roadmap / Phase 2:** Multivariate outlier detection (e.g., Mahalanobis Distance, Isolation Forest) for joint-metric anomalies.

---

## 3. Hard Findings & Quantitative Evidence

### 1. Flight Telemetry (`telemetry.csv` & `flight_plan.csv`)
- **Phase 1 (Release $\rightarrow$ WP-02, 0–246 s):** Released at ~1,510 m AGL. Stable glide; WP-01 hit by 1.21 m, WP-02 by 1.30 m (including a clean −20° bank).
- **Phase 2 (Primary seizure, 246.1–255.1 s):** Autopilot commanded a bank reversal toward WP-03.
  - **Left aileron command:** ramped to ~+16–18°.
  - **Left aileron actual:** held at −3° to −10° for **9.0 s** (max gap 27.8°); roll stalled at ~+6° vs +18° setpoint. Right aileron (same bus) tracked within ~1°.
  - **Breakaway:** at 255.1 s the surface snapped −9.7° → +16.9° in ~100 ms (stiction release).
- **Phase 3 (Recurrence, 292–940 s):** 11 further sticking episodes (>5° gap, up to 17°, up to 8 s). Every waypoint WP-03..WP-21 missed (109 m → 5,146 m).
- **End:** `FAILSAFE` (`NAV_PATH_DEVIATION`) at 1182 s as a *consequence*; impact at 1198.9 s ~5 km off track. Descent rate was normal throughout — no dive or stall.

### 2. Factory Build Data (`manufacturing.csv`)
- **Aircraft ID:** `GLDR-0421` (Serial: `SN-12661455`, Built: 2026-02-06, `HB-PLANT-1`, Line 3, Shift B).
- **Defective Component:** Left Aileron Servo (ActuMax SM-14, Serial: `SV-65031081`, Lot: `L2026-07B`).
- **Fleet Statistics (Fleet Size: 200,000 Units):**
  - **Sweep Current:** **332.70 mA** ($\mu = 185.12$, $\sigma = 24.89$) $\rightarrow$ **$Z = +5.93\sigma$** (one-sided p ≈ 1.5×10⁻⁹, ~1 in 650 million; highest of 200,000; only 2 units > 300 mA)
  - **Peak Current:** **400.32 mA** ($\mu = 260.08$, $\sigma = 34.59$) $\rightarrow$ **$Z = +4.05\sigma$**
  - **Sweep Time:** **940.89 ms** ($\mu = 780.04$, $\sigma = 55.04$) $\rightarrow$ **$Z = +2.92\sigma$**
  - **Position Error RMS:** **1.13°** ($\mu = 0.55$, $\sigma = 0.19$) $\rightarrow$ **$Z = +2.98\sigma$**
  - **Backdrive Torque:** **133.68 N·mm** ($\mu = 92.14$, $\sigma = 17.69$) $\rightarrow$ $Z = +2.35\sigma$
  - **Other 4 servos (Right Aileron, Elevon L/R, Rudder):** All $|Z| < 2.6\sigma$ across their 9 metrics (nominal).
- **Lot L2026-07B is NOT elevated** (5,784 units, normal mean) — this is a single-unit defect, not a bad lot.
- **Second unit with same signature:** `GLDR-200000` — 304.2 mA sweep (+4.78σ), 367.7 mA peak, OC-PLANT-2, lot L2026-02B, built 2026-02-18. Recommend hold + teardown.
- **Data schema:** 5 servos (`elevon_l`, `elevon_r`, `aileron_l`, `aileron_r`, `rudder`) × 9 bench metrics (sweep_time, sweep_current, idle_current, peak_current, backdrive_torque, position_error_rms, deadband, hysteresis, temp_rise). Plants: `HB-PLANT-1`, `OC-PLANT-2`.
- **QC exceptions ignored:** 32,296 units have `qc_exception_count > 0` yet all passed (GLDR-0421 itself had 0).

---

## 4. Current State of the Codebase

The repository is synchronized at [`avlukas04/mach-ops-eng-assessment`](https://github.com/avlukas04/mach-ops-eng-assessment):

```text
andrea_lukas_ops_eng_assessment/
├── README.md                                # Assessment brief
├── REPORT.md                                # Part 1 written deliverable
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

### Phase 1: Write `REPORT.md` (Part 1 Deliverable) — ✅ DRAFTED (2026-10-12)
> Figs 1 and 3 were regenerated after fixing two bugs in `analysis/flight_investigation.py` (anomaly window spanned the whole flight; altitude trace was filtered out).
> **Scripts corrected 2026-10-13. Every number in REPORT.md §3–5 is now printed by a script; nothing is hardcoded:**
> - `flight_investigation.py`: separate sticking episodes (E1); control-mode/FAILSAFE timeline; computed wind, temperature, acceleration, GPS, and attitude checks; computed right-aileron tracking (max 1.3°); the sensor check is limited to before breakaway.
> - `manufacturing_analysis.py`: `--vehicle` argument; computed QC gap (100% pass, 32,296 passed with exceptions, 9,571 R05); lot check; a same-signature scan that lists vehicle IDs (E4). The scan finds GLDR-0421, GLDR-200000, and 8 borderline units (both current readings at 3.0–3.6σ, against about 2 expected by chance), which should be first for retest. The stale hardcoded summary (+5.86σ etc.) was replaced with computed values.
> - `docs/root_cause_analysis.md`, `docs/reviewed_questions_answers.md`, and `docs/deliverable_plan.md` were corrected to match.
Draft the formal written summary required by README Deliverable #1:
1. **Executive Summary & Root Cause Determination:** Clear statement attributing failure to ActuMax SM-14 servo gearbox friction on GLDR-0421.
2. **Flight Investigation & Telemetry Evidence:** Link and explain Figures 1, 2, and 3.
3. **Manufacturing Investigation & Evidence:** Link and explain Figures 4 and 5; detail the $+5.93\sigma$ Z-score.
4. **Quality Process Gap Analysis:** Explain the 100% pass rate anomaly, the R05 rework trap, and absence of parametric limits.
5. **Proposed Prevention & Detection Approach:** Two-tier SPC gate (robust high-side z; QUARANTINE on D ≥ 6.0 or z ≥ 5.5; FLAG on z ≥ 3) across 25 actuator metrics.

### Phase 2: Implement Automated Detection (`spc_detector.py`)
- Python CLI utility that reads `manufacturing.csv` (or single vehicle records), evaluates all 25 metrics against a frozen robust baseline (median, IQR/1.349), computes the high-side z and the per-servo combined score D, and outputs a JSON/CSV verdict with reason codes: `QUARANTINE` (D ≥ 6.0 → `SIGNATURE`, or z ≥ 5.5 → `EXTREME`), `FLAG` (z ≥ 3 → `HIGH`, D ≥ 4.5, or `SATURATED`), else `PASS`. Must list vehicle IDs; the current `analysis/manufacturing_analysis.py` only scores the hardcoded GLDR-0421 and counts per-metric exceedances (GLDR-200000 was found with a one-off query).

### Phase 3: Build Interactive Production Health Dashboard
- Lightweight web application (Python backend + Vanilla HTML/Plotly.js frontend).
- **Tab 1 (Fleet Overview):** Factory KPIs, pass/flag yields, plant/shift trends.
- **Tab 2 (SPC Control Charts):** Distribution histograms and control charts with $\pm 3\sigma$ thresholds.
- **Tab 3 (Vehicle Lookup):** Single vehicle lookup (e.g., search `GLDR-0421`) showing status breakdown across all 25 metrics.
