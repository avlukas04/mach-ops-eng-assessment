# Dashboard Implementation Plan

How the live production health dashboard will be built, step by step: what each piece does, how it works, why it is built that way, and which problem from the investigation it addresses. Requirements and architecture are in [`LIVE_SYSTEM_DESIGN.md`](LIVE_SYSTEM_DESIGN.md); this document is the build plan.

Status: **plan for review**. Nothing below is built yet.

---

## 1. From problems found to what the dashboard does

Everything the dashboard does traces back to a problem the investigation found. Each capability does one of three things:

- **Detect:** notice a problem as soon as the data shows it.
- **Prevent:** stop a problem from reaching flight (hold, retest, re-screen, sign-off).
- **Clarify:** make the state of production obvious to the people who act on it.

| # | Problem found (evidence) | Capability | Type | Team | Built in |
|:--|:--|:--|:--|:--|:--|
| I1 | A +5.93σ servo passed QC; bench readings are recorded but never judged (REPORT §4–5) | Every unit scored on arrival; QUARANTINE queue | Detect, prevent | Production, engineering | Steps 3, 5, 6 |
| I2 | GLDR-200000's fault is spread across several readings; no single limit catches it (§6.1) | Combined score D per servo, shown in the z grid | Detect | Engineering | Steps 3, 6 |
| I3 | Defects are single units; all 21 lots are identical (§4.4) | Per-unit scoring for containment; lot p-chart shows no lot shift, so no needless recall | Detect, clarify | Quality, production | Steps 4, 6 |
| I4 | 100% QC pass rate for a month went unnoticed (§5) | Header tile: "QC passed 100%" next to "Gate held N, flagged M" | Clarify | All (quality first) | Steps 4, 6 |
| I5 | 32,296 units with logged exceptions passed; 59,229 have some open warning (§6) | Open-warnings panel by type, line and station | Clarify, prevent | Quality | Steps 4, 6 |
| I6 | GLDR-0421's R05 rework didn't re-check the servo (§5) | Any changed record is re-scored; history kept; rework panel by code and line | Prevent, clarify | Quality, production | Steps 1, 3, 6 |
| I7 | Readings pinned at instrument ceilings; GLDR-0421 read above one (§6.1) | `SATURATED` tag; ceiling-share panel | Detect | Quality (fixture owners) | Steps 3, 4, 6 |
| I8 | GLDR-200000 and 8 borderline units share the signature (§4.4) | Retest list ranked by D | Prevent | Engineering / flight test | Steps 5, 6 |
| I9 | Teams planned to repeat the test with other units without knowing which were risky (README) | Vehicle lookup with go / no-go and reasons | Prevent, clarify | Engineering / flight test | Steps 5, 6 |
| I10 | 10 Hz sampling hid extremes; a provided column had an undocumented definition (verifier) | Every record scored; inputs validated; rejects logged and shown | Detect | All (system health) | Steps 2, 3, 6 |
| I11 | Limits would drift if recomputed from live data (§6.2) | Frozen, versioned baseline; version shown on every score and in the health strip | Prevent | Quality | Steps 1, 3, 6 |
| I12 | D's false-alarm math assumes uncorrelated readings (\|r\| ≈ 0.006 today) | Rolling correlation check with alert | Detect | Quality | Step 4 |
| I13 | A drifting supplier, line or shift would show at group level before any unit crosses a limit (§6.1) | FLAG-rate p-charts with control limits and run rules | Detect, prevent | Quality | Steps 4, 6 |
| I14 | A gate that silently stops working looks the same as a healthy one (the 100% pass rate) | Freshness, ingest errors and assumption checks; strip turns red | Detect | All | Steps 4, 6 |
| I15 | Nothing recorded who released a unit or why (§6.2: engineer sign-off) | Retest / release workflow with name and note, logged as events | Prevent, clarify | Engineering, quality | Steps 1, 5, 6 |

**Prevention happens at four points:**
1. **At the bench / on arrival:** QUARANTINE before final QC (I1, I2).
2. **Before flight:** retest list and go / no-go lookup (I8, I9).
3. **After rework:** automatic re-score (I6).
4. **Upstream:** p-charts catch drift before it produces quarantined units (I13).

---

## 2. What each team gets

| Team | Detect | Prevent | Clarify |
|:--|:--|:--|:--|
| **Production** (floor leads, line managers) | New QUARANTINE units as they arrive, with age | Pull blocked units before final QC; see re-screens after rework | Units per hour by line vs normal; verdict mix by line and shift; rework rate by code |
| **Quality** | Out-of-control FLAG rates by lot / line / shift / plant / vendor / model; drift in bench readings; ceiling share; broken assumptions | Contact a supplier or stop a line on a p-chart signal before units fail; extend fixture range | QC-reported vs gate verdicts; open warnings by type; baseline version |
| **Engineering / flight test** | Why a unit was flagged: which servo, which reading (z grid, reasons) | Go / no-go before flight; retest the highest-D units first; record retest and release | Full traceability: servo serials and lots, build line and shift, score history, events |
| **Everyone** | System problems: feed stopped, inputs rejected | — | One header showing the state of production, plus the system health strip |

---

## 3. Build steps

Each step has **what** is built, **how** it works, **why** it's built that way, the **problems** it addresses, and the **check** that proves it works. Each step is committed separately.

### Step 0: Setup

- **What:** `requirements.txt` (pandas, numpy, matplotlib, fastapi, uvicorn, httpx) and installation of the three new packages. `outputs/live/` is gitignored.
- **How:** `pip install -r requirements.txt`.
- **Why:** anyone can reproduce the environment with one command. The live database is generated data, so it is rebuilt by replay, not committed.
- **Check:** imports succeed; existing unit tests and `tests/verify_report.py` (110 claims) still pass.

### Step 1: Store, `pipeline/store.py`

- **What:** the SQLite database and every read and write to it.
- **How:**
  - Opened in **WAL mode**, so the dashboard can read while the worker writes.
  - Tables:
    - `units`: one row per vehicle, the facts as received, plus a fingerprint (hash) of the row.
    - `scores`: the current verdict, reasons, per-servo D, worst servo and metric, saturated flag, **baseline version** and `scored_at`.
    - `score_history`: every score ever given, so changes are visible.
    - `events`: `QUARANTINE_RAISED`, `RETEST_PASS`, `RETEST_FAIL_REPLACED`, `RELEASED`, each with who, when and a note.
    - `runs`: one row per ingest cycle (rows read, new, changed, rejected, `duration_ms`). The duration is measured on a monotonic clock from fetch to write, not by subtracting wall-clock timestamps, so an NTP or manual clock change can't corrupt it; `started_at` / `finished_at` are wall-clock times (millisecond resolution) that record *when*. Databases created before the column existed gain it on open, with NULL for old rows.
    - `ingest_errors`: the rejected row and the reason.
  - Each batch is written in **one transaction**.
  - A unit that arrives again unchanged (same fingerprint) is ignored. A unit that arrives with **different values** (for example, after rework) is updated and re-scored, and the old score moves to history.
- **Why:**
  - One transaction per batch means a crash never leaves a half-written batch.
  - Fingerprints make re-reading the source harmless and turn real changes into a re-screen, closing the R05 gap (I6).
  - The baseline version on every score makes any verdict reproducible (I11).
  - Events give sign-off traceability (I15).
- **Check (tests):**
  - re-inserting the same unit changes nothing;
  - a changed unit is re-scored and its old score is in history;
  - a batch that fails midway leaves no partial rows;
  - events are recorded with who and when.

### Step 2: Source, `pipeline/source.py` and `tools/factory_simulator.py` (done)

- **What:** where units come from, behind one interface: `fetch_new(cursor) → Batch(frame, cursor, sim_time)`. The factory and the gate are **separate processes** connected only by a feed, as they would be in a plant.
- **How:**
  - **Factory simulator** (`tools/factory_simulator.py`), the only simulated part:
    - Publishes `manufacturing.csv` into a feed database (`outputs/live/mes.db`, table `production_units`) one build hour at a time, in one transaction per hour, each row with an increasing `seq` and its `published_sim` time.
    - `--speed` sets seconds of real time per build hour: 2 s plays the month in about 22 minutes; `max` publishes it in about 22 s. Stopping and restarting resumes at the next unpublished hour; `--reset` starts over.
    - Test injections, off by default and clearly marked: `--rework N` republishes N earlier units per hour with rework code `SIM`; `--bad-rows N` publishes malformed rows (blank ID, or `SIMBAD-` ID with a bench reading of `ERR`).
  - **MesSource**, the gate's live connection: pulls `seq > cursor` from the feed over a read-only connection, the standard incremental read from an MES or warehouse table.
    - A batch never crosses a published hour (indexed on `published_sim, seq`), so every unit keeps its true arrival time even when a late-starting gate catches up; batches are capped at 5,000 rows.
    - On an empty poll `sim_time` is the feed's own clock, so the gate knows whether the factory is alive. If the feed doesn't exist yet, it returns empty batches until it does.
  - **CsvWatchSource**, for plants that drop file exports: reads the file only once it has settled (same size and time on two polls, ends with a newline), then returns only new or changed rows.
  - **ReplaySource**: the CSV in build order by hour; used by the simulator and as a fast test loader.
  - **Pacer**: schedules hour *k* at start + *k* × speed, so processing time never makes the replay drift slow.
  - **Store speedup**: `write_batch` now uses one `executemany` per table and one lookup per batch.
- **Why:**
  - A gate reading a feed written by another process is a real live connection, not a file played inside the dashboard. Against a real MES only the connection and column names change.
  - Scoring and display never depend on where data comes from.
  - The simulated clock makes "before final QC" and "time in queue" measurable, which is the core of "flag before shipment".
- **Problems addressed:** I10 (every record processed, in order, exactly once).
- **Checks (`tests/test_source.py`, 23 tests), all passing:**
  - full month through simulator → feed → MesSource: 200,000 unique units, identical to the CSV's IDs; 672 hours in order ending 2026-02-28 23:00; every unit's build hour equals its arrival hour; GLDR-0421 arrives 02-06 03:00 and GLDR-200000 02-18 07:00; GLDR-0421's values unchanged through the feed;
  - gate started before the factory, late gate catching up, gate restart equal to an uninterrupted run, factory restart resuming, pacing, rework republishing, bad rows delivered (blank IDs never merged);
  - CSV watcher: first read, unchanged file, added and edited rows only, half-written file never parsed.
- **Measured:** publishing the month at `max` 22 s; gate ingest (fetch, score, store) 144 ms per build hour (slowest 215 ms), 97 s for the month, down from 649 ms per hour. End to end it reproduces 2 / 6,498 / 193,500 with holds at 02-06 03:00 and 02-18 07:00. At the default speed the gate uses about 7% of each 2-second interval.

### Step 3: Runner, `pipeline/runner.py` (done)

- **What:** the gate running unattended: fetch → validate → score → store → report, with a heartbeat when idle.
- **How:**
  1. **Fetch** from the saved cursor. The cursor is written in the same transaction as its batch, so a crash never loses or repeats data. Each feed carries a unique `feed_id`; if the feed is replaced, the gate stalls with a clear message instead of silently waiting past the new feed's sequence numbers.
  2. **Validate**, on the principle that a unit with a valid ID always gets a verdict:

     | Problem | Handling |
     |:--|:--|
     | Blank `vehicle_id` | Rejected and logged (it cannot be held or traced) |
     | Non-numeric bench reading (e.g. `ERR`) | Logged with column and value, stored as not measured, **QUARANTINE INCOMPLETE** (fail closed) |
     | Blank bench reading | QUARANTINE INCOMPLETE, not logged as an error |
     | Required column missing (schema change) | Whole batch refused, cursor kept, logged once, status `STALLED` until fixed |
     | Unknown extra column | Dropped, logged once |
  3. **Score** with `spc_detector.classify` and the baseline pinned at start.
  4. **Store** in one transaction: units, scores, history, new holds, errors, run log, cursor.
  5. **Report** to listeners (Step 4 health and Step 5 live push plug in here). The command line prints each hold with its lead time before final QC and QC's own result.
  6. **Heartbeat** about every 10 s: `last_poll_at`, the feed's clock, runner status and baseline status in `meta`. Empty polls are not logged as runs.
  7. **Failures** retry the same cursor with backoff (1, 2, 4 … 30 s). A batch is never skipped; a broken listener never stops the gate.
- **Baseline control (approved decision: pin, warn, promote by restart):**
  - *Pin:* loaded once at start; version and SHA-256 recorded; every score carries the version.
  - *Warn on drift:* each heartbeat re-hashes the file. If it differs, is missing or unreadable, `meta.baseline` says so and one warning is logged. **Scoring does not change.**
  - *Promote by restart:* the `baselines` table records each activation and the one it replaced. Re-using a version with different content is refused, so a version always means one exact file.
  - *`--rescore`:* explicit; re-scores units scored under another baseline, keeping old scores in `score_history`; a hold is raised only if a unit newly becomes QUARANTINE.
  - *Why:* this is how quality and ML teams control rule changes. Nobody can change production verdicts by editing a file, and every verdict stays reproducible. The cost is that adopting a baseline needs a restart, which is acceptable for rules that should change rarely and deliberately.
- **Why (overall):** scoring each unit on arrival puts a verdict in front of people 1–9 h before final QC (I1); reusing `spc_detector` means the live gate is the verified gate (I2, I7); storing QC's result next to the gate's verdict records the Part 2 gap per unit.
- **Problems addressed:** I1, I2, I6, I7, I10, I11.
- **Command line:** `python3 -m pipeline.runner [--feed … | --csv …] [--db …] [--reset] [--once] [--rescore] [--poll 1] [--quiet]`.
- **Checks (`tests/test_runner.py`, 22 tests), all passing:**
  - validation rules above, one test each; a median unit passes and a stiff-aileron unit is quarantined `SIGNATURE`;
  - failed batch writes nothing and retries; backoff 1, 2, 4, 8, 16, 30, 30 s; idle heartbeat without run rows; rework re-scored with a new hold only on change; broken listener; restart equals uninterrupted; factory and gate running concurrently; replaced feed stalls;
  - baseline: drift warns but verdicts unchanged; restart records promotion; same version with different content refused; rescore keeps history and is idempotent;
  - **golden run** (simulator with rework and bad-row injection → feed → runner): 200,000 real units, **193,500 PASS / 6,498 FLAG / 2 QUARANTINE, identical to `classify()` on every unit** (verdict, reasons, D); GLDR-0421 held 02-06 03:00 (final QC 04:00) and GLDR-200000 held 02-18 07:00 (final QC 08:00), both passed by QC; 672 blank-ID rows rejected, 672 `ERR` rows quarantined INCOMPLETE; 671 rework republishes of 670 units, each re-scored once, the one identical repeat ignored.
- **Measured:** simulator and gate as two concurrent processes over the full month: the factory finished publishing in 24 s and the gate caught up at 117 s; 672 runs, 0 errors, totals 2 / 6,498 / 193,500. Per-batch `duration_ms` (fetch → validate → score → write, monotonic clock): median 137 ms, p95 157 ms, p99 243 ms, max 300 ms for a median of 298 units (0.46 ms per unit). At the default speed of one build hour every 2 s, the gate is busy 6.9% of each interval (median), 15% worst case.

### Step 4: Health, `pipeline/health.py`

- **What:** the calculations behind the five health layers, computed from the database after each batch.
- **How:**
  - **FLAG-rate p-charts.** For each group (servo lot, line, shift, plant, vendor, model, and day), p = flagged / units. The centre line p̄ is the fleet FLAG rate (3.25%). Limits are p̄ ± 3·√(p̄(1 − p̄)/n) for that group's size n, so small groups get wider limits. A point outside the limits, or 8 consecutive points on one side of the centre line (a standard Western Electric run rule), is marked out of control. Lot charts use servo-level flags, so a bad supplier lot shows even when the rest of the vehicle is fine.
  - **QC vs gate.** `final_qc_pass` share (reported) beside the gate's counts (held, flagged, clean). This shows the 100% pass rate against what the data actually says.
  - **Open warnings:** units with QC exceptions, IMU or magnetometer calibration WARN, or failed strain relief, by type, line and QC station.
  - **Throughput and rework:** units per build hour and day by plant, line and shift; rework share and count by code (R05, R12, R19, R21, R33) and line.
  - **Instrument ceilings:** share of readings at each fixture ceiling, per metric.
  - **Freshness:** time since the last batch, compared with the expected cadence. Stale means no batch for more than 3× the cadence.
  - **Assumption check:** correlation between each servo's five metrics over the most recent 20,000 units. Alert if any |r| exceeds 0.1, because the D score's false-alarm rate would no longer hold.
- **Why:**
  - Per-unit flags catch defective units. Group charts catch a process going wrong before units fail, which is upstream prevention (I13).
  - The QC-vs-gate comparison and the self-checks exist because the original failure was a quality system that looked healthy and wasn't (I4, I12, I14).
  - Open warnings and rework give quality its closure list (I5, I6).
  - The ceiling share tells fixture owners when their instrument range is hiding information (I7).
- **Problems addressed:** I3, I4, I5, I6, I7, I12, I13, I14.
- **Check against known values:**
  - FLAG rate 3.25%, with every plant, line, shift and day inside its limits (daily range 2.9–3.7% against limits of about 2.6–3.9%);
  - maximum |r| about 0.006;
  - 1,028 saturated vehicles;
  - 59,229 units with open warnings;
  - 20% reworked;
  - plus a synthetic test that injects a drifting lot and confirms its p-chart goes out of control.

### Step 5: API and live push, `dashboard/app.py`

- **What:** a FastAPI service that starts the runner and serves each view's data.
- **How:**
  - **Startup hook (lifespan):** opens the store and starts the runner as a background task, so scoring runs from server start whether or not anyone is watching.
  - **Endpoints** (all read-only except one):

    | Endpoint | Serves | Team |
    |:--|:--|:--|
    | `GET /api/summary` | counts by verdict, QC-reported pass share, units / hour, simulated time, freshness | All (header) |
    | `GET /api/quarantine` | blocked units: reasons, worst servo, D, age, line, shift | Production |
    | `GET /api/throughput?by=` | units and rework per hour / day by line, shift or plant | Production |
    | `GET /api/charts/pchart?by=` | FLAG rate, n, limits and out-of-control marks per group | Quality |
    | `GET /api/charts/trend?servo=&metric=` | daily median and quartiles of a bench reading | Quality |
    | `GET /api/warnings` | open warnings by type, line and station | Quality |
    | `GET /api/ceilings` | share of readings at each instrument ceiling | Quality |
    | `GET /api/retest` | FLAG units ranked by D | Engineering |
    | `GET /api/units/{id}` | full record, verdict, reasons, 5×5 z grid, D, history, events | Engineering |
    | `POST /api/units/{id}/events` | record a retest result or a release (name and note required) | Engineering |
    | `GET /api/health` | freshness, ingest errors, assumption check, baseline version | All |
    | `GET /api/stream` | server-sent event on every new batch | All |

  - **Release rules:** a quarantined unit stays blocked until a `RELEASED` event with a name and note is recorded. `RETEST_FAIL_REPLACED` keeps it blocked until the replacement servo is scored.
- **Why:**
  - Each view gets exactly what it needs, so the browser does no analysis.
  - The same API can be called by other systems: a bench station could ask "is this unit cleared?" before installing a servo.
  - Server-sent events push an update the moment a batch lands, so a new hold appears on every screen without a refresh.
  - The release rules make the engineer sign-off from REPORT §6.2 enforceable and traceable (I15).
- **Problems addressed:** I1, I8, I9, I15, plus the README's "operate continuously".
- **Check (API tests on a replayed database):**
  - both known defects are in `/api/quarantine`;
  - `/api/units/GLDR-0421` returns D = 8.57 and the left aileron row of the z grid;
  - a release without a note is refused;
  - a valid release is logged and the unit leaves the queue;
  - unknown IDs return 404;
  - `/api/stream` emits an event after a batch.

### Step 6: Frontend, `dashboard/static/`

- **What:** a single page with a header and four tabs: Production, Quality, Engineering, System health. Charts use Plotly.js; the page is plain HTML, CSS and JavaScript with no build step.
- **How:**
  - **Header (everyone):** simulated time and a live indicator; tiles for units built, held, flagged and clean; **"QC reported 100% pass"** shown beside the gate's verdicts; baseline version.
  - **Production:**
    - QUARANTINE queue (newest first: vehicle, line, shift, worst servo, D, reasons, time in queue);
    - units per hour by line, with the normal range shaded;
    - verdict mix by line and shift;
    - rework rate by code and line.
  - **Quality:**
    - p-chart with a group selector (lot, line, shift, plant, vendor, model, day); out-of-control points highlighted with the rule that fired;
    - bench-reading trend for a chosen servo and metric (daily median and spread);
    - open warnings by type and station;
    - ceiling share per metric.
  - **Engineering:**
    - vehicle search showing verdict, reasons, and the **5×5 z grid** (servos × readings) with FLAG-level and QUARANTINE-level cells marked and D per servo;
    - servo serials and lots, build line and shift, score history and events;
    - retest / release form;
    - retest list ranked by D, with GLDR-200000 and the borderline units at the top.
  - **System health:** freshness, ingest errors (latest rejected rows and reasons), assumption check, baseline version and hash. The tab label and header indicator turn red when anything is unhealthy.
  - **Live updates:** the page listens to `/api/stream` and refreshes the open tab when a batch lands, falling back to polling every 10 s if the stream drops.
  - The **dataviz skill** is loaded before any chart code: one colour system across tabs, readable in light and dark, with status colours reserved for verdicts.
- **Why:**
  - Each tab leads with its team's question (§2), so nobody hunts for their answer.
  - The QC-vs-gate tile puts the original failure on screen permanently (I4).
  - The z grid answers "why was this flagged?" in one look (I2, I9).
  - The red health indicator means a broken feed can't look like a quiet day (I14).
- **Check:**
  - run the replay and walk each tab;
  - GLDR-0421 appears in Production at simulated 02-06 03:00;
  - opening it in Engineering shows the left aileron row in red with D = 8.57;
  - releasing it with a note logs the event;
  - stopping the replay turns the health strip red.

### Step 7: One command and documentation

- **What:** `python3 -m dashboard --speed 2 [--reset]` starts the factory simulator and the gate (runner, API and page) as separate processes and opens `http://127.0.0.1:8000`. Updates to REPORT §7 (tools table, run instructions, how the dashboard addresses each README bullet), the handoff doc, and the presentation guide's demo order.
- **Why:** a reviewer should be able to see it running with one command, and the report should explain it.
- **Check:** from a fresh clone, install and run; units start arriving within seconds.

### Step 8: Final verification

- **What:** all unit and golden tests, `tests/verify_report.py`, both analysis scripts, a full replay end to end, and a code review pass.
- **Check:** everything is green, and the dashboard's counts match the report exactly.

---

## 4. How the dashboard answers each README bullet

| README | Answered by |
|:--|:--|
| Surfaces production health across the fleet | Header plus the five health layers: verdicts (product risk), p-charts and trends (stability), QC vs gate and open warnings (quality-system integrity), throughput and rework, ceilings and data health |
| Flags at-risk units before shipment | Scoring on arrival; QUARANTINE queue before the final-QC timestamp (proved by the golden test); retest list by D; open warnings; release only with sign-off |
| Clear visibility to production, quality and engineering | One tab per team built around its decision (§2), plus a shared header and health strip |
| Operates continuously with a live connection to data | Background runner from server start; source adapter (replay now, factory feed later); server push; self-monitoring that turns red when the feed or assumptions break |

---

## 5. Out of scope, stated

- **User accounts and permissions:** the release form records a name but doesn't authenticate it. Production would add login and role-based release rights (for example, quality engineers only).
- **Blocking on open warnings:** shown, not blocked, because the data doesn't link them to failures (REPORT §6).
- **Alert delivery** (email, Slack, pager): the health endpoint is designed for an alerting tool to poll; wiring one up is a deployment step.
- **Postgres and multi-plant deployment:** the schema is written to move; the demo uses SQLite.
