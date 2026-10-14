# Live System Design: Foundation for the Production Health Dashboard

The README asks for a dashboard that "will operate continuously in a real production environment with a live connection to data." This document designs the layer underneath it: how units arrive, get validated and scored, get stored, and get served. The dashboard is a view on top of this layer, so the layer has to be right first.

Status: **design for review**. Nothing here is built yet beyond `tools/spc_detector.py`.

---

## 1. Lessons from the investigation that shape the design

Each lesson came from something that went wrong, or nearly did, during the analysis.

| # | Lesson (where it came from) | Design rule |
|:--|:--|:--|
| L1 | **Sampling hides extremes.** 10 Hz telemetry understated wind, temperature and HDOP ranges, and the sticking-episode peaks. | Every decision is made on every record. Downsampling is for display only. |
| L2 | **Provided values can be opaque.** `flight_plan.csv`'s miss distances had an undocumented definition until recomputed. | Validate inputs at the door: schema, types, ranges, duplicates. Recompute derived fields rather than trusting them. |
| L3 | **Missing or out-of-range data must not pass.** GLDR-0421's 400.32 mA sat above the fixture's 380 mA ceiling and nobody noticed. | Fail closed: a unit that can't be fully scored is quarantined (`INCOMPLETE`), and readings at or above an instrument ceiling are tagged. |
| L4 | **Limits drift if recomputed from live data.** | The baseline is frozen, versioned and hashed. Every score records which baseline produced it. Changing it is a reviewed event. |
| L5 | **The D score assumes independent measurements** (true today, \|r\| ≤ 0.01). | The system monitors its own assumptions: correlation between metrics and FLAG rate by lot/line/shift, alerting when either drifts. |
| L6 | **Claims need a second check.** The independent verifier caught 18 discrepancies. | Golden tests run on every change: GLDR-0421 and GLDR-200000 must QUARANTINE, the fleet counts must match, and the rule tests must pass. |
| L7 | **People need the why, not just a verdict.** | Every verdict carries reason codes and the z / D values behind them. Every action is logged. |
| L8 | **Defects are single units, not lots** (all 21 lots identical). | Score each unit as it is built, at the bench, before install. Lot-level views are for trends, not containment. |

---

## 2. Architecture

```
  Source                  Ingest              Score                Store             Serve            View
┌──────────────┐     ┌──────────────┐    ┌───────────────┐    ┌────────────┐    ┌──────────┐    ┌───────────┐
│ CSV (today)  │     │ validate     │    │ spc_detector  │    │  SQLite    │    │ JSON API │    │ Dashboard │
│ MES / DB     │───▶ │ schema/types │──▶ │ frozen        │──▶ │  units     │──▶ │ /summary │──▶ │ Production│
│ (production) │     │ dedupe       │    │ baseline      │    │  scores    │    │ /units   │    │ Quality   │
│              │     │ new IDs only │    │ reason codes  │    │  runs      │    │ /charts  │    │ Engineering│
└──────────────┘     └──────┬───────┘    └───────┬───────┘    │  events    │    │ /health  │    └───────────┘
                            │ rejects            │            └────────────┘    └──────────┘
                            ▼                    ▼
                      ingest_errors        health monitors (drift, freshness, correlation)
```

**Why this shape:** each stage has one job and can be tested alone. The source adapter is the only part that changes when the CSV is replaced by the factory database (the README's "live connection"). The scoring stage reuses `spc_detector.py` unchanged, so the gate that was validated is the gate that runs.

### 2.1 Source adapter

- **Today:** watches `manufacturing.csv` (file modification time plus row count). Because the assessment data is static, a **replay mode** feeds the 200,000 units in `build_date_utc` order at a chosen speed (for example, one simulated day per 10 seconds). That makes the dashboard genuinely live in a demo: units arrive, get scored, and appear.
- **Production:** an adapter with the same interface (`fetch_new(since_cursor) -> rows`) that queries the MES or factory database by build timestamp.
- **Why:** the rest of the system never knows where data comes from, so swapping sources is a one-file change.

### 2.2 Ingest and validate

- Required columns present; numeric fields parse; IDs non-empty.
- **New units only:** keyed by `vehicle_id`, so re-reading the file never double-counts (idempotent).
- A duplicate ID with *different* values is recorded as an ingest error, not silently overwritten.
- Rejected rows go to `ingest_errors` with the reason, and show on the dashboard's health panel.

### 2.3 Score

- Calls `spc_detector.compute_scores` / `classify` with the frozen baseline.
- Each score row stores the baseline version and hash, so a verdict can be reproduced later.
- **Rework:** if a unit's record changes (new rework code, retest values), it is re-scored and the history is kept (6.2: re-screen after any rework).

### 2.4 Store (SQLite)

SQLite because it's a single file, needs no server, and handles this volume easily. In production the same schema moves to Postgres.

| Table | Contents | Why |
|:--|:--|:--|
| `units` | vehicle_id, build date, site, line, shift, servo serials and lots, the 25 bench readings, QC fields, open warnings | The facts as received |
| `scores` | vehicle_id, verdict, reasons, per-servo D, worst servo/metric, saturated, baseline version, scored_at | Current verdict, reproducible |
| `score_history` | every past score per unit | Audit trail; shows re-screens after rework |
| `runs` | each ingest/score cycle: time, rows read, new, rejected, duration | Freshness and throughput monitoring |
| `events` | QUARANTINE raised, released, engineer sign-off, baseline changes | Who did what, when (AS9100-style traceability) |
| `ingest_errors` | rejected rows and reasons | Data-quality visibility |

### 2.5 Health monitors

Run after each cycle and surfaced on the dashboard:
- **Freshness:** time since the last new unit. Alert if the line is running but nothing arrives.
- **FLAG-rate p-charts:** by servo lot, line, shift, plant, vendor and model against the 3.25% baseline, with 3σ binomial control limits. Catches a drifting supplier before any unit crosses QUARANTINE.
- **Assumption check:** rolling correlation between each servo's five metrics. If any |r| passes 0.1, the D score's false-alarm math no longer holds and the baseline needs review.
- **Instrument ceilings:** share of readings at a fixture limit, per metric. Rising share means the fixture range needs extending.

### 2.6 Serve (JSON API)

| Endpoint | Returns | Used by |
|:--|:--|:--|
| `GET /api/summary` | counts by verdict, FLAG rate, last update, freshness | All teams: header tiles |
| `GET /api/quarantine` | QUARANTINE queue with reasons and age | Production: what's blocked |
| `GET /api/retest` | FLAG units ranked by D | Engineering / flight test |
| `GET /api/warnings` | units shipped with open QC warnings, by type | Quality |
| `GET /api/charts/pchart?by=lot\|line\|shift\|site\|vendor\|model` | FLAG rate per group with control limits | Quality |
| `GET /api/charts/trend?metric=...&servo=...` | daily median and spread of a bench reading | Quality / engineering |
| `GET /api/units/{vehicle_id}` | full record, verdict, 5×5 z grid, D, history | Vehicle lookup (go/no-go) |
| `GET /api/health` | freshness, ingest errors, assumption checks, baseline version | Everyone: is the system itself OK? |

Framework: Flask or FastAPI (FastAPI gives typed responses and auto-generated docs). The dashboard frontend polls `/api/summary` every few seconds and refreshes the views when the update time changes.

---

## 3. What the dashboard will show (built on this layer)

| View | Team | Built from |
|:--|:--|:--|
| Fleet overview: verdict mix, units/day by line and shift, quarantine queue, rework rate | Production | `/summary`, `/quarantine`, `units` |
| SPC: FLAG-rate p-charts, bench-reading trends, open warnings, ceiling share | Quality | `/charts/*`, `/warnings`, `/health` |
| Vehicle lookup and retest list: z grid, reasons, servo traceability, go/no-go | Engineering / flight test | `/units/{id}`, `/retest` |
| System health: freshness, ingest errors, baseline version, assumption checks | All | `/health` |

At-risk before shipment is shown in three levels: **blocked** (QUARANTINE), **retest first** (FLAG ranked by D), **ships but visible** (open warnings).

---

## 4. Build order and how each step is checked

| Step | Deliverable | Check |
|:--|:--|:--|
| 1 | `pipeline/store.py`: schema and data access | Unit tests: insert, idempotent re-insert, conflicting duplicate → ingest error |
| 2 | `pipeline/source.py`: CSV watcher and replay adapter | Replay of the full file yields 200,000 unique units in build order |
| 3 | `pipeline/runner.py`: ingest → validate → score → store loop | After a full replay the DB counts equal `spc_detector score`: 2 / 6,498 / 193,500 (golden test) |
| 4 | `pipeline/health.py`: p-charts, freshness, correlation, ceilings | Known values: FLAG rate 3.25%, max \|r\| ≈ 0.006, 1,028 saturated |
| 5 | `dashboard/app.py`: JSON API | Endpoint tests against the replayed DB; GLDR-0421 and GLDR-200000 in `/quarantine` |
| 6 | `dashboard/static/`: frontend | Manual walkthrough per team; data-viz guidelines for charts |
| 7 | REPORT §7 and run instructions | One command starts replay + API + dashboard |

---

## 5. Decisions for review

1. **Replay speed and start point** for the demo: start empty and replay from the first build date, so viewers see units arrive. Recommended.
2. **SQLite for the demo**, with the schema written to move to Postgres. Recommended.
3. **FastAPI vs Flask:** FastAPI recommended for typed responses and built-in API docs.
4. **Quarantine release workflow:** in the demo, an engineer can mark a QUARANTINE unit as retested/released with a note, logged to `events`. This shows the sign-off loop the report recommends. Optional, but it makes "flags at-risk units before shipment" tangible.
