# Live Production Health System: Requirements, Design and Rationale

This document defines what the README's dashboard requirement means for this data, why the dashboard is needed, and how it is built so that it runs continuously on live data. The README asks for:

> A live, monitorable dashboard or interactive tool that:
> - Surfaces production health across the fleet.
> - Flags at-risk units before shipment.
> - Provides clear visibility to production, quality, and engineering teams.
>
> Design your solution as if it will operate continuously in a real production environment with a live connection to data.

Status: **design for review.** Only `tools/spc_detector.py` (the scoring gate) exists so far. The step-by-step build plan, with how and why for each piece, is in [`DASHBOARD_IMPLEMENTATION_PLAN.md`](DASHBOARD_IMPLEMENTATION_PLAN.md).

---

## 1. Interpreting the requirement

The requirement uses four terms that have to be pinned to the data before anything is built.

### 1.1 "The fleet"

**The fleet is every vehicle in `manufacturing.csv`: 200,000 Glide units built from 2026-02-01 to 2026-02-28** (about 7,150 a day, recorded hourly) at two plants (HB-PLANT-1, OC-PLANT-2), each with three lines (LN1–LN3), across three shifts and six final-QC stations.

It is the **production** fleet, not a flying fleet. Only one of these vehicles (GLDR-0421) has flight data. That matters: the dashboard can't judge units by how they fly. It has to judge them by what the factory measures as they are built, which is exactly where the investigation found the defect signal.

### 1.2 "Production health"

Production health answers one question for each team: **is the factory building units that will work, and would we know if it weren't?** The investigation showed that both halves matter. The defective servo was a product problem, and the 100% pass rate was a quality-system problem that went unnoticed for a month.

It is defined as five layers. The first three come directly from the investigation; the last two are standard manufacturing health that the data supports.

| Layer | Question | Metrics | Source in the investigation |
|:--|:--|:--|:--|
| **1. Product risk** | Are any units likely to fail? | Verdict mix (PASS / FLAG / QUARANTINE); QUARANTINE queue; FLAG units ranked by D | The defect: an out-of-family servo (REPORT §4, §6.1) |
| **2. Process stability** | Is any part of the process drifting? | FLAG rate by servo lot, line, shift, plant, vendor, model, with control limits; daily trends of the bench readings | Single-unit defects today, but a drifting supplier would show here first (REPORT §6.1 "What FLAG is for") |
| **3. Quality-system integrity** | Is QC actually screening? | Gate yield vs reported yield (`final_qc_pass` says 100%); units shipped with open warnings (59,229, 29.6%); rework re-screened or not | The root cause in the process: data recorded but never judged (REPORT §5) |
| **4. Throughput and rework** | Is the line producing at rate, and where is rework concentrated? | Units per day by plant / line / shift; rework rate (20%) by code (R05, R12, R19, R21, R33) and by line | Context the floor needs; rework R05 was GLDR-0421's missed re-check |
| **5. Measurement and data health** | Can we trust the numbers? | Readings at instrument ceilings (1,028 vehicles); ingest errors; data freshness; the independence assumption behind D | Found while building the gate: a third ceiling and a reading above it (REPORT §6.1) |

**Not in scope:** predicting failures from flight data (there is one flight), and blocking on open warnings (the data doesn't link them to failures; REPORT §6 "Scope and assumptions").

### 1.3 "At-risk units" and "before shipment"

The data has no shipment column. The last recorded step is final QC, 1–9 hours after build (median 5). So **"before shipment" means before a unit's final QC release**, and the system has to score each unit as soon as its bench data exists, ideally at the servo bench before installation (REPORT §6.2).

At-risk has three levels, each with a different action:

| Level | Rule | Action | Count today |
|:--|:--|:--|--:|
| **Blocked** | QUARANTINE (D ≥ 6.0, any z ≥ 5.5, or missing data) | Hold. Retest servo; engineer sign-off to release | 2 |
| **Retest first** | FLAG, ranked by D | Ships, but highest-D servos are retested before flight | 6,498 |
| **Ships, visible** | Open QC warnings (exceptions, calibration WARN, strain relief) | Ships; quality closes the warning | 59,229 |

For scale: GLDR-0421 was built on 2026-02-06 and flew on 2026-02-28. The gate would have blocked it 22 days before the crash, and an hour before its final QC.

### 1.4 "Clear visibility to production, quality and engineering"

Each team asks a different question, so each gets a view built around its decision, not a shared wall of charts.

| Team | Their question | What they see | What they do with it |
|:--|:--|:--|:--|
| **Production** (floor leads, line managers) | Can these units ship? Is my line healthy right now? | QUARANTINE queue with age; units/hour vs target; verdict mix by line and shift; rework rate | Pull blocked units; escalate a line with rising FLAG or rework |
| **Quality** | Is the process in control? Is QC doing its job? | FLAG-rate p-charts by lot / line / shift / plant / vendor / model; bench-reading trends; open warnings; ceiling share | Investigate an out-of-control signal; contact a supplier; close warnings; request fixture range change |
| **Engineering / flight test** | Is this vehicle safe to fly? Why was it flagged? | Vehicle lookup: verdict, reasons, 5×5 z grid, servo serials and lots, history; retest list | Go / no-go for flight; choose units for teardown |

Plus a **system health** strip for everyone: last update, data freshness, ingest errors, baseline version.

### 1.5 "Operate continuously with a live connection to data"

The system must run unattended: new units are picked up automatically, scored in well under a second, and appear on every open dashboard without a refresh. It must also tell people when *it* is unhealthy (no data arriving, inputs rejected, assumptions broken), because a monitoring system that fails silently recreates the 100%-pass problem.

The assessment data is static, so a **replay** source streams the 200,000 units in build-time order (for example, one build-hour every 2 seconds plays the month in about 22 minutes). Swapping replay for the factory database is a change to one adapter.

### 1.6 Requirement traceability

| README requirement | Interpretation | Delivered by | Accepted when |
|:--|:--|:--|:--|
| Surfaces production health across the fleet | Five health layers over all 200,000 units, by plant / line / shift / lot / day | Health views, p-charts, trends, integrity panel | Every layer has a panel; group FLAG rates match the gate output |
| Flags at-risk units before shipment | Three-level at-risk list, scored on arrival, before final QC release | Scoring stage + QUARANTINE queue + retest list | During replay GLDR-0421 enters the queue at its build hour (02-06 03:00), before its QC timestamp (04:00); same for GLDR-200000 |
| Clear visibility to production, quality, engineering | One view per team, built around its decision | Three team tabs + system health strip | Each team's question in §1.4 is answerable in one view |
| Operates continuously with live data | Unattended ingest → score → store → push, with self-monitoring | Source adapter, runner, health monitors, server-sent events | Replay runs end to end unattended; health strip goes red if the feed stops |

---

## 2. Motivation: why a live dashboard, from what the investigation found

1. **The data to catch the defect existed for 22 days and nobody looked at it.** The bench recorded +5.93σ on 2026-02-06. A scored, visible queue turns a number in a CSV into a held unit.
2. **The quality system's failure was invisible.** A 100% pass rate over a month and 32,296 exceptions that still passed are obvious on a chart and invisible in a spreadsheet. Showing gate yield next to reported yield makes that gap impossible to miss.
3. **Defects are single units, not lots.** All 21 servo lots are identical, so containment can't be "recall lot X". Every unit has to be scored as it's built, which is a continuous process, not a monthly report.
4. **Drift would show at the process level first.** One supplier going bad would raise its FLAG rate before any unit crossed QUARANTINE. Only a running p-chart catches that.
5. **Different teams act on different signals.** Production needs a hold list now; quality needs trends; engineering needs a per-vehicle explanation. One CSV export serves none of them.

---

## 3. Lessons from the investigation that become design rules

| # | Lesson (where it came from) | Design rule |
|:--|:--|:--|
| L1 | **Sampling hides extremes.** 10 Hz telemetry understated several ranges (found by `tests/verify_report.py`). | Every decision uses every record. Downsampling is for display only. |
| L2 | **Provided values can be opaque.** The flight plan's miss distances had an undocumented definition. | Validate inputs on arrival (schema, types, duplicates). Recompute rather than trust derived fields. |
| L3 | **Out-of-range data went unnoticed.** GLDR-0421's 400.32 mA sat above the fixture's 380 mA ceiling. | Fail closed: unscoreable units are QUARANTINE (`INCOMPLETE`); ceiling readings are tagged. |
| L4 | **Limits drift if recomputed from live data.** | Frozen, versioned, hashed baseline. Every score records its baseline version. |
| L5 | **D assumes uncorrelated measurements** (true today, \|r\| ≈ 0.006). | The system monitors its own assumptions and alerts on drift. |
| L6 | **Claims need a second check.** The verifier caught 18 discrepancies. | Golden tests on every change: both known defects must QUARANTINE; fleet counts must match. |
| L7 | **People need the why.** | Every verdict carries reason codes and the z / D values; every action is logged. |
| L8 | **Defects are single units.** | Score each unit on arrival, before release. Lot views are for trends. |

---

## 4. Architecture

```
 SOURCE              INGEST             SCORE               STORE              SERVE               VIEW
┌─────────────┐   ┌─────────────┐   ┌──────────────┐   ┌─────────────┐   ┌──────────────┐   ┌──────────────┐
│ Replay of   │   │ validate    │   │ spc_detector │   │ SQLite (WAL)│   │ FastAPI      │   │ Browser      │
│ CSV (demo)  │──▶│ schema/types│──▶│ frozen       │──▶│ units       │──▶│ JSON API     │──▶│ Plotly.js    │
│ MES / DB    │   │ dedupe      │   │ baseline     │   │ scores      │   │ + SSE push   │   │ Production   │
│ (production)│   │ new IDs only│   │ reason codes │   │ runs/events │   │              │   │ Quality      │
└─────────────┘   └──────┬──────┘   └──────┬───────┘   └──────┬──────┘   └──────────────┘   │ Engineering  │
                         ▼                 ▼                  ▼                              └──────────────┘
                   ingest_errors     health monitors: freshness · p-charts · correlation · ceilings
```

### 4.1 Walkthrough: one unit's path

Following GLDR-0421 through the replay:

1. **Source.** At simulated time 2026-02-06 03:00 the replay emits that hour's ~300 build records, GLDR-0421 among them.
2. **Ingest.** Each row is checked: required columns present, numbers parse, `vehicle_id` not seen before. Valid rows go on; bad rows go to `ingest_errors` with the reason.
3. **Score.** The batch is scored with the frozen baseline in milliseconds. GLDR-0421 gets QUARANTINE with reasons `SIGNATURE aileron_l D=8.57; EXTREME aileron_l.sweep_current_ma z=5.92; …`.
4. **Store.** The unit, its score (with baseline version) and a `QUARANTINE_RAISED` event are written in one transaction.
5. **Health.** Monitors update: that hour's FLAG rate, freshness, ceiling counts.
6. **Push.** The server sends an update event to every open dashboard.
7. **View.** Production's queue shows GLDR-0421, age 0 h, an hour before its 04:00 QC timestamp. Engineering opens it and sees the left aileron row of the z grid in red. Quality sees no lot shift, because L2026-07B's FLAG rate stays in control (a single-unit defect).
8. **Act.** An engineer records "servo pulled for retest" in the release workflow; the event log keeps who and when.

---

## 5. Tech stack: what each technology does and why

| Layer | Technology | What it does here | Why this choice |
|:--|:--|:--|:--|
| Scoring | **Python, NumPy, pandas** + `tools/spc_detector.py` | Validates and scores each batch | Reuses the exact gate that was tested and verified; vectorised scoring handles 200,000 units in about 4 s, so a batch of hundreds takes milliseconds |
| Source | **Python adapter class** (`fetch_new(cursor)`) | Replay today; MES / database query in production | One interface; swapping data sources touches one file |
| Runner | **Background worker** (asyncio task in the API process) | Polls the source every few seconds, runs ingest → score → store → health | Simple to run (one command), no separate queue to operate; can be split into its own process later |
| Store | **SQLite in WAL mode** | Units, scores, history, runs, events, errors | Single file, no server, safe concurrent reads while the worker writes; schema written to move to **Postgres** in production |
| API | **FastAPI + Uvicorn** | JSON endpoints for each view; auto-generated API docs at `/docs` | Typed responses catch contract errors; async fits the background worker; docs make the API usable by other tools (MES, alerts) |
| Live push | **Server-Sent Events (SSE)** | Tells browsers "new data" the moment a batch lands | One-way push is all a dashboard needs; simpler than WebSockets, works through proxies, auto-reconnects; polling is the fallback |
| Frontend | **Plain HTML / CSS / JavaScript + Plotly.js** | Three team tabs, tables, interactive charts | No build step, so anyone can run it; Plotly gives hover, zoom and per-point inspection, which engineers need to drill from a chart into a unit |
| Tests | **unittest** + golden replay test + `tests/verify_report.py` | Rules, store, endpoints; end-to-end counts | Same standard as the analysis: every number checked |

Alternatives considered. The system has two jobs: **ingest** (pull, score and store new units continuously, whether or not anyone is watching) and **display** (show the stored results to whoever opens the page). **Streamlit** reruns its script from the top for every viewer interaction, so computation is tied to viewing: with no viewer nothing is scored, and several viewers would duplicate the work unless worked around with caching or hidden threads. It is excellent for one analyst exploring data, not for an unattended service. **Dash** runs on Flask with callbacks and can host a background thread or read from a separate worker, so it is a workable choice; its drawbacks here are timer polling instead of server push and no built-in API for other systems. **Grafana** is excellent for time series but can't host the per-vehicle explanation or the release workflow. **FastAPI** starts the ingest loop when the server starts (independent of viewers), serves read-only endpoints to any number of browsers, pushes updates with SSE, and exposes the same JSON API to other tools (a bench station, MES, alerting). In production the ingest worker would become its own process; with FastAPI that move doesn't change the API or the frontend.

---

## 6. Why it detects defects efficiently and thoroughly, and gives clear visibility

**Thorough**
- **Every unit, every measurement.** All 25 servo bench readings on every unit, as it arrives. No sampling (L1).
- **Pattern, not just thresholds.** The combined score D catches a servo that is moderately high on several readings, which is how GLDR-200000 is caught when no single limit would.
- **Two levels of detection.** Per-unit (QUARANTINE) catches single defective units; per-group p-charts catch drift across a lot, line or shift before any unit crosses a limit.
- **Nothing passes unmeasured.** Missing data quarantines; ceiling readings are tagged (L3).
- **It watches itself.** Freshness, ingest errors, the independence assumption and ceiling share are monitored, so a broken feed or a broken assumption is visible, unlike the 100%-pass gate.

**Efficient**
- **Low noise where it matters.** QUARANTINE fires on 2 of 200,000 units, with about 0.1 expected false alarms. Nobody learns to ignore it.
- **Fast.** Scoring a batch takes milliseconds, so a verdict exists long before the unit's final QC (1–9 h after build).
- **Ranked work lists.** FLAG units are ordered by D, so retest effort goes to the highest-risk servos first.

**Clear**
- **One view per decision** (§1.4), so each team sees its question answered first.
- **Every verdict explains itself:** reason codes plus the 5×5 z grid show which servo and which reading.
- **Traceable:** baseline version on every score; every hold and release logged with who and when.

---

## 7. Store and API

### 7.1 Tables (SQLite now, Postgres in production)

| Table | Contents | Why |
|:--|:--|:--|
| `units` | vehicle_id, build time, site, line, shift, QC time and station, servo serials and lots, the 25 bench readings, rework, open warnings | The facts as received |
| `scores` | vehicle_id, verdict, reasons, per-servo D, worst servo / metric, saturated, baseline version, scored_at | Current verdict, reproducible |
| `score_history` | every past score per unit | Audit trail; re-screens after rework |
| `runs` | each cycle: time, rows read / new / rejected, duration | Freshness and throughput |
| `events` | QUARANTINE raised / released, sign-offs, baseline changes | Traceability |
| `ingest_errors` | rejected rows and reasons | Data-quality visibility |

### 7.2 Endpoints

| Endpoint | Returns | View |
|:--|:--|:--|
| `GET /api/summary` | counts by verdict, gate yield vs reported yield, units/hour, freshness | Header tiles (all) |
| `GET /api/quarantine` | blocked units with reasons and age | Production |
| `GET /api/throughput?by=line\|shift\|site` | units and rework per hour / day | Production |
| `GET /api/charts/pchart?by=lot\|line\|shift\|site\|vendor\|model` | FLAG rate per group with control limits | Quality |
| `GET /api/charts/trend?servo=&metric=` | daily median and spread of a bench reading | Quality |
| `GET /api/warnings` | open warnings by type and group | Quality |
| `GET /api/retest` | FLAG units ranked by D | Engineering |
| `GET /api/units/{vehicle_id}` | full record, verdict, z grid, D, history | Engineering |
| `POST /api/units/{vehicle_id}/events` | record retest / release with a note | Engineering |
| `GET /api/health` | freshness, ingest errors, assumption check, ceiling share, baseline version | System health |
| `GET /api/stream` | server-sent events on each new batch | All views |

---

## 8. Build order and how each step is checked

| Step | Deliverable | Check |
|:--|:--|:--|
| 1 | `pipeline/store.py`: schema, data access | Insert; idempotent re-insert; conflicting duplicate → ingest error |
| 2 | `pipeline/source.py`: replay adapter | Full replay yields 200,000 unique units in build order |
| 3 | `pipeline/runner.py`: ingest → score → store | After full replay, DB counts = 2 / 6,498 / 193,500 (golden test); GLDR-0421 raised at 02-06 03:00 |
| 4 | `pipeline/health.py`: p-charts, freshness, correlation, ceilings | FLAG rate 3.25%; all groups in control; max \|r\| ≈ 0.006; 1,028 saturated |
| 5 | `dashboard/app.py`: API + SSE | Endpoint tests on the replayed DB |
| 6 | `dashboard/static/`: three team views + health strip | Each team's question in §1.4 answered in one view |
| 7 | REPORT §7 and run instructions | One command starts replay, API and dashboard |

---

## 9. Decisions for review

1. **Replay as the demo source**, starting empty so viewers watch units arrive. Recommended.
2. **SQLite (WAL) for the demo**, Postgres-ready schema. Recommended.
3. **FastAPI + SSE** (needs `pip install fastapi uvicorn`) vs **Flask + polling** (Flask already installed). FastAPI recommended.
4. **Release workflow** (record retest / release with a note). Recommended: it makes "flag before shipment" an action, not just a list.
