# Operations Engineering Technical Assessment: Glide Flight Anomaly and Manufacturing Quality

## Background

A recent Glide test flight failed horribly...
The aircraft was released at altitude, entered a planned loiter pattern, deviated from its intended path, and ultimately impacted the ground. The unit had passed final quality inspection prior to flight and all tests were nominal.

After reviewing the flight test data, engineering suspects the issue was due to manufacturing and not a software or design issue, but quality had not flagged any defects or abnormalities. The teams have no time to dig in to this issue and are planning to repeat the test with other production units and see if the issue is repeated.

Your task is to investigate the data and confirm whether or not the flight failure was due to a manufacturing issue. Additionally, the quality team is in desperate need of automation and tooling to improve their defect detection process. Once you identify the cause of the test failure, build tooling to automatically detect this defect in the future. Additionally, build a live dashboard that Manufacturing, Quality, and Engineering teams can monitor to see the quality status of production units as they are built.

In this directory, you are provided with:

- `telemetry.csv` — Flight telemetry data from the aircraft during the failed test 
- `flight_plan.csv` — the intended flight plan described as waypoints for the test
- `manufacturing.csv` — build and QC data for all produced vehicles
- `telemetry-simulation.py` — local tool to view telemetry data

---

## Quick Start (Telemetry Viewer)

From this `ops_eng_assessment` directory:

1. Launch the telemetry viewer:

```bash
python telemetry-simulation.py
```

2. Open browser to:

```text
http://127.0.0.1:8766/
```

Notes:
- If port `8766` is in use, choose another with `--port`.

---

## Part 1 — Flight Investigation

Engineering and Manufacturing needs to know whether this failure was caused by manufacturing before repeating the test with additional units.

Using `telemetry.csv` and `flight_plan.csv`:

- Determine what caused the aircraft to deviate from the planned flight path.
- Assess whether the failure mechanism is consistent with a manufacturing-related defect.
- Support your conclusion with defensible, data-backed evidence.

Build whatever tooling or analysis workflows you believe are necessary to reach a confident conclusion.

---

## Part 2 — Manufacturing Detection & Automation

If you determine the issue originated in manufacturing:

- Identify the relevant signals in `manufacturing.csv`.
- Explain why the current quality process failed to detect the issue.
- Design and implement an automated method to detect similar defects going forward.

In addition, build a **live, monitorable dashboard or interactive tool** that:

- Surfaces production health across the fleet.
- Flags at-risk units before shipment.
- Provides clear visibility to production, quality, and engineering teams.

Design your solution as if it will operate continuously in a real production environment with a live connection to data.

---

## Deliverables

Please provide:

1. A concise written summary of:
   - Your root cause determination  
   - Supporting evidence  
   - Your proposed detection and prevention approach
   - An overview of the tools you built (including how and why you built them)

2. The tools you built (deliver in whatever form is easiest e.g., links, zips, files, etc.)

3. Clear instructions for running your tools

There are no restrictions on the tools you use to analyze the data / build tooling... the use of AI is strongly encouraged.
