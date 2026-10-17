# Dashboard Implementation Plan

How the live production health dashboard will be built, step by step: what each piece does, how it works, why it is built that way, and which problem from the investigation it addresses. Requirements and architecture are in [`LIVE_SYSTEM_DESIGN.md`](LIVE_SYSTEM_DESIGN.md); this document is the build plan.

Status: **Steps 0–5 built and tested**; Steps 6–8 planned.

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
  6. **Heartbeat** about every 10 s: `last_poll_at`, the feed's clock and status (with silence measured on the gate's monotonic clock, added in Step 4), runner status and baseline status in `meta`. Empty polls are not logged as runs.
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

### Step 4: Health, `pipeline/health.py` (done)

- **What:** five health layers, each green / amber / red, and one alert list, read from the gate's database (read-only) after each batch. Approved severities: **ALARM** = contain now (an open hold, a chart limit breached, the pipeline broken, D's assumption broken); **WATCH** = an early trend or a slowdown; **INFO** = the legacy QC comparison. A layer is red with any ALARM, amber with any WATCH.
- **How, layer by layer:**

  | Layer | Calculation | Alert |
  |:--|:--|:--|
  | 1. Product risk | Verdict counts; hold queue with each unit's held time, final-QC time, lead time and age (build hours); saturated units; latest human action per hold | Each open hold: ALARM until a person releases it (Step 5 release rules) |
  | 2. Process stability | Daily FLAG-rate p-charts for the fleet and each plant, line (plant + line: LN1–LN3 exist at both plants), shift and QC station (vehicle verdict), and for each servo's lots, vendors and models (that servo's own flag); fleet hourly series for display | Point at or above its exact upper limit: ALARM. 8+ days in a row above the centre line: WATCH. At or below the exact lower limit: WATCH ("unusually clean: check the gate"). Only signals on the last 2 build days drive status; older ones stay marked on the chart |
  | 3. Quality-system integrity | Final-QC pass share beside the gate's verdicts (a QC × verdict table); holds placed before final QC; open warnings (QC exception, IMU / magnetometer calibration WARN, strain relief failed) by type, line and QC station | QC passing ≥ 99.9% while the gate holds or flags units: INFO |
  | 4. Throughput and rework | Units per build hour by line; per day by plant, line and shift; rework share by line and units by code (`|` or `;` separated) | A line building under 50% of its normal hourly units in each of the gate's last 2 build hours: WATCH (one slow hour is normal variation) |
  | 5. Gate and data health | Runner status and heartbeat age; feed state, cadence and silence; gate lag in build hours; batch latency p50 / p95 / max over the last 24 runs from `duration_ms`; ingest errors in those runs, grouped by reason; scoring- and health-baseline drift; correlation of each servo's five metrics over the 20,000 most recent units by build hour; share of readings at each fixture ceiling | STALLED: ALARM. Heartbeat older than 30 s: ALARM. Feed silent > 3× its cadence: ALARM (finished: INFO). Lag > 3 build hours: WATCH. p95 > 50% of the cadence: WATCH. Any ingest error: WATCH. Baseline drift: WATCH. Any \|r\| > 0.1: ALARM |

  - **Exact binomial limits.** The plan had p̄ ± 3·√(p̄(1 − p̄)/n) and an 8-point run on either side. Checked on the real month before building (158 groups × 28 days, no real problem), that would have raised 45 false alarms, because servo flag rates are about 0.65% (≈1.6 flags per lot per day): the normal approximation is wrong at such small counts and most days fall below the average. Exact binomial limits at the same 0.135% tail, with the run rule applied above the centre line only, raise **2 ALARM points and 3 WATCH runs** on the same month, matched exactly by the tests. `binom_limits` walks the binomial pmf in log space (no SciPy); results are cached because closed days never change.
  - **Frozen health baseline** (`tools/health_baseline.json`, built by `python3 -m pipeline.health baseline`): centre lines (vehicle 3.25%; servo 0.622–0.695%), normal units per line-hour (49–50), and every threshold above. Pinned at start with its SHA-256 and kept apart from the scoring baseline, so tuning an alarm never changes the scoring version. If either file on disk changes, or the health baseline was built for a different scoring version, health raises a WATCH.
  - **Incremental counts.** One SQLite GROUP BY over 200,000 units takes about 570 ms, so recomputing ~20 of them per batch would exceed the 2 s demo cadence. Counts are loaded once and then updated from `score_history.id` (it only increases): a re-scored or reworked unit's old contribution is subtracted and its new one added, so nothing is counted twice. Units are held in memory as append-only parts with a live-row mask (compacted every 64 parts), so a batch never copies the whole table. All reads for one refresh run inside one read transaction (a consistent WAL snapshot).
  - **Feed silence measured by the gate.** The factory now records `cadence_s`, `state` (running / finished) and `published_at` in `feed_meta`; `MesSource.feed_status()` reads them. The runner notes the feed clock on every poll and measures silence on its own monotonic clock from the last time that clock advanced, so a skewed factory wall clock cannot fake or hide silence; the heartbeat publishes it with the time it was checked. The heartbeat also now records the true feed clock (it previously recorded the batch's hour, which understated lag during catch-up), and the first heartbeat is written as soon as the gate's database exists.
- **Why:**
  - Per-unit flags contain defective units; group charts catch a process going wrong before many units fail, which is upstream prevention (I13). The lot charts use that servo's own flag, so a bad supplier lot shows even when the rest of the vehicle is fine.
  - The QC-vs-gate comparison and the gate's self-checks exist because the original failure was a quality system that looked healthy and wasn't (I4, I12, I14): a silent gate or a stopped feed turns red instead of looking like a clean day.
  - Open warnings and rework give quality its closure list (I5, I6); the ceiling share tells fixture owners when their range hides information (I7).
- **Problems addressed:** I3, I4, I5, I6, I7, I12, I13, I14.
- **Command line:** `python3 -m pipeline.health [--db …] [--watch 5] [--json]` (exit 2 when red, so a cron job or CI can alert); `python3 -m pipeline.health baseline` rebuilds the health baseline.
- **Checks (`tests/test_health.py`, 27 tests), all passing:**
  - exact limits equal a brute-force binomial sum; chart signals (above, below, run of 8, run reset, active window);
  - running counts equal a full recount and a fresh load after new, reworked and rescored units, and after compaction;
  - each alert rule at and just past its threshold: open hold until released, throughput (one slow hour, slow then recovered, two slow hours, 25 vs 20 units, a stopped line, a feed gap), QC INFO, open warnings by type, stalled runner, heartbeat 29 s vs 31 s, feed silent 5.9 s vs 6.1 s at a 2 s cadence (finished → INFO; unpaced → none), lag 3 vs 4 build hours, latency from `duration_ms` with the wall clock running backwards, ingest errors grouped by reason, both baselines drifting, correlated readings, no database;
  - simulator records cadence and `finished`; runner measures silence on its monotonic clock while the factory's wall clock is wrong; first heartbeat written as soon as the database exists;
  - **real month** (200,000 units): 2 / 6,498 / 193,500; 1,028 saturated; QC passed 200,000 of 200,000; 59,229 units with open warnings (32,296 QC exceptions); 39,927 reworked (19.96%), codes R12 9,668 · R21 9,627 · R05 9,571 · R33 9,522 · R19 9,473; both holds placed 1 h before final QC. FLAG rate exactly 3.25%; every fleet, plant, line, shift and QC-station chart in control; daily fleet rate 2.88–3.67% against exact limits of 2.62–2.64% (low) and 3.90–3.92% (alarm). The rule set's signals equal an independent count made before the module existed: 2 above (aileron_l lot L2026-07C on 02-28, 9/358; rudder model AM-9 on 02-22, 24/1,699), 0 below, 5 run points; only the 02-28 lot point is recent enough to drive status. No throughput WATCH in any of the 672 hours. A single hour would have little margin (lowest line-hour 27 units vs a 24.5–25 threshold), so the WATCH needs 2 slow build hours in a row: in the month's slowest 2-hour stretch, the better hour still built 37 units. The cost is that a stopped line is flagged after 2 build hours instead of 1. Max \|r\| 0.0059 over the month, under 0.03 on the 20,000-unit window;
  - **drifting lot:** 55 units of aileron_l lot L2026-03B made FLAG from 02-15 (about 3× its normal rate), applied as changed units through the store so the incremental path runs on 200,000 units: first ALARM on 02-15, the lot's later days above or in a run, no other aileron_l lot changed. (One seeded run; by simulation a 3× lot drift alarms in a median of 3 days, 90th percentile 8.)
- **Measured:** first load of 200,000 units 7.3 s (reading 4.7 s, preparing 23 text columns 2.9 s; once per start). Per batch at 200,000 units: update median 96 ms, max 127 ms for 300 reworked units (each subtracted and re-added); snapshot 50–60 ms (it was 525 ms before counts were indexed by chart and units kept in parts). Live, with factory, gate and health as separate processes: stopping the factory raised `feed_silent` 12 s later (3 s threshold plus the 10 s heartbeat), stopping the gate raised `heartbeat` at 31 s, catch-up showed `lag` WATCH until it reached 0, and the finished replay showed `feed_finished` INFO with totals 2 / 6,498 / 193,500.

### Step 5: API, live push and release workflow, `dashboard/app.py` (done)

- **What:** one gate process (FastAPI, served by uvicorn) that runs the runner, keeps a health snapshot current, serves each view's data, pushes a server-sent event on every change, and enforces the hold and release rules. The factory simulator stays a separate process; the two share only the feed.
- **How:**

  | Part | Role | Why |
  |:--|:--|:--|
  | Runner thread | Polls the feed and scores, exactly as Step 3; starts with the server (lifespan hook) | Detection runs whether or not anyone has the page open |
  | Health worker (one thread) | After every batch, and at least once a second, `refresh()` then `snapshot()`; saves the result | `Health` isn't thread-safe, so one thread owns it. The once-a-second round shows a silent feed or a dead runner even when no batch arrives |
  | Request handlers | Read the saved snapshot; only a unit record, the retest list and a reading trend touch the database or the in-memory units | 50 open screens cost what one does |

  Each part has its own SQLite connection (WAL): the runner writes batches, the API writes sign-offs, health and requests read.

  - **Endpoints:**

    | Endpoint | Serves | Team |
    |:--|:--|:--|
    | `GET /api/status` | loading / ready, runner state, both baselines (version and SHA-256), timings; answers before the first snapshot | All |
    | `GET /api/summary` | status and layer colours, alert counts, verdicts, open holds, QC-reported pass share, freshness | Header |
    | `GET /api/alerts?layer=&severity=` | the alert list | All |
    | `GET /api/quarantine?open=true\|false\|all` | holds: held time, lead before final QC, age, current verdict, latest gate retest, evidence, allowed actions, last action, who released | Production |
    | `GET /api/throughput?by=line\|shift\|plant&period=hour\|day&last=` | units per hour (by line) or day, normal rates, rework by code and line | Production |
    | `GET /api/charts/pchart?by=&group=` | daily points, exact limits and signals for one group type (fleet, plant, line, shift, QC station, or a servo's lot / vendor / model) | Quality |
    | `GET /api/charts/trend?servo=&metric=` | daily quartiles of one bench reading, with the baseline median, FLAG and QUARANTINE levels and the ceiling; cached until the next batch | Quality |
    | `GET /api/warnings`, `GET /api/ceilings` | open warnings by type, line and station; share of readings at each ceiling | Quality |
    | `GET /api/retest?limit=` | FLAG units ranked by D, with allowed actions | Engineering |
    | `GET /api/units/{id}` | full record, score, 5×5 z grid (recomputed from the readings with the pinned scoring baseline, since the store keeps only D and the worst z), D per servo, score history, events, hold state | Engineering |
    | `POST /api/units/{id}/events` | records `RETEST_PASS`, `RETEST_FAIL_REPLACED` or `RELEASED`: 422 if a field is missing or blank, 404 for an unknown unit, 409 with the reason if not allowed | Engineering |
    | `GET /api/health` | the gate layer: freshness, lag, latency, ingest errors, correlation, baselines | All |
    | `GET /api/stream` | server-sent events (below) | All |

  - **Start-up:** the first snapshot loads every stored unit (7.3 s for the full month). Until it's ready `/api/status` says `loading` and the snapshot endpoints return 503 with `Retry-After: 2`. Before any units exist the views return empty lists, not errors.
  - **Live push:** one small event per change (new units, an alert raised or cleared, a status change) with the snapshot number, simulated time, layer colours, alert counts and the ids of alerts added and cleared; the page refetches only its open tab. A keep-alive comment every 15 s and `retry: 3000`. Each client keeps only the newest event, so a slow screen skips ahead. Server-sent events rather than WebSockets: one-way, the browser reconnects by itself, plain HTTP, no extra library.
  - **Stopping:** a live stream never ends by itself, so a plain server waits forever for open screens on SIGTERM (found in the live run). On a stop signal the gate ends every stream first; a 3 s grace limit is the backstop. Then the lifespan shutdown stops the runner between batches.
- **Release rules** (approved; in `pipeline/store.py`, so the API and any future caller obey the same rules):
  - A QUARANTINE opens a hold, and **only a person closes it**, with `RELEASED`. New bench readings never close a hold on their own (before this step, a republished unit that scored PASS silently dropped off the queue).
  - `RELEASED` needs a name, a note and passing evidence as the latest retest result since the hold opened: a `RETEST_PASS` (a person re-ran the bench test), or a **gate retest** that scored PASS.
  - **Gate retest:** when a held unit comes back through the feed with changed bench readings, the gate scores them and logs `GATE_RETEST` with the verdict. PASS makes the hold releasable; FLAG or QUARANTINE is failing evidence and cancels an earlier `RETEST_PASS`. Rework that changes only paperwork (same readings) logs nothing.
  - `RETEST_FAIL_REPLACED` (servo swapped) blocks every action until the replacement's readings arrive and are scored. Before this step it closed the hold, so a replacement that still scored QUARANTINE was shown as cleared (reproduced, then fixed).
  - After a release, a QUARANTINE with changed readings opens a new hold.
  - FLAG units can record retests and replacements; `RELEASED` is refused because nothing is held. PASS units: nothing to record.
  - The check and the insert run in one transaction, so a batch landing in between can't slip past the rules. Older databases are migrated (the events table is rebuilt to allow `GATE_RETEST`).
- **Why:**
  - Each view gets exactly what it needs, so the browser does no analysis, and other systems can call the same API (a bench station could ask whether a unit is cleared before installing a servo).
  - A push the moment a batch lands means a new hold appears on every screen without a refresh.
  - The release rules make the engineer sign-off from REPORT §6.2 enforceable and traceable (I15). A release without a passing retest is impossible, and missing a required re-check is how GLDR-0421 got through.
- **Problems addressed:** I1, I8, I9, I15, plus the README's "operate continuously".
- **Command line:** `python3 -m dashboard.app [--feed …] [--db …] [--port 8000] [--no-runner]`.
- **Checks, all passing:** `tests/test_app.py` (11 tests), seven new hold tests in `tests/test_store.py` (22 in all), one in `tests/test_health.py` (28 in all):
  - on a replayed database of two real build days (14,308 units): both known defects held at 02-06 03:00 and 02-18 07:00, 1 h before final QC, each an ALARM; `/api/units/GLDR-0421` gives D = 8.57 with only the left aileron row at QUARANTINE level; counts equal the store and `classify()`;
  - sign-offs: blank or missing note 422, unknown type 422, unknown unit 404, release without retest 409 with the reason, retest pass then release 201; the hold leaves the open queue and its alert clears on the next snapshot; a second release 409;
  - replaced servo still bad: stays held, gate retest QUARANTINE, ALARM stays; clean readings: still held, "ready to release", release allowed; later failing evidence cancels a retest pass; a released unit with new bad readings is held again, but not after a paperwork-only republish; old events table migrated;
  - start-up: `/api/status` answers while loading, snapshot endpoints 503 with `Retry-After`; no database: empty views and a red gate layer;
  - on a real socket: a batch released → event received in 130–144 ms over four runs; a stop signal with a screen connected stops the server in 0.1–0.2 s;
  - breaking the release rule on purpose (release always allowed) fails 4 tests; removing the stop handling fails the shutdown test.
- **Measured, live** (factory and gate as separate processes, whole month at 0.5 s per build hour, 4× the demo speed, with 11 endpoints polled every second):
  - 2 / 6,498 / 193,500; both holds on screen by the next build hour, with lead 1.0 h; a release without retest refused (409), retest then release accepted, and the hold left the queue with one stream event;
  - endpoint median 9–27 ms (p95 31–119 ms) except the reading trend, median 61 ms (p95 309 ms), which groups 200,000 readings after each batch; 674 stream events for 672 batches;
  - largest gap between batch events 1.78 s; the only longer gap (5.2 s) is start-up, while the factory loads the CSV;
  - batches took 262 ms on average in the shared process (167 ms standalone in Step 3), so at 0.5 s per hour the `latency` WATCH (p95 over half the cadence) fired, as designed. At the demo's 2 s cadence the limit is 1 s.

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
- **Throughput alert in CSV mode:** the throughput WATCH is validated on the factory feed only. The CSV watcher has no factory clock, so it uses the current wall time, which never matches a build hour in the file, and the check stays silent (verified: a line building 5 units an hour for 2 hours raised nothing in CSV mode and a WATCH when checked at its build hour). The CSV watcher is still tested as an ingest path. For a plant that sends live CSV exports, the fix is to check the two most recent completed build hours, never the one still being built.
- **Postgres and multi-plant deployment:** the schema is written to move; the demo uses SQLite.
- **CUSUM charts (future improvement):** a binomial CUSUM would catch small supplier drifts faster. Simulated for one lot of about 330 units a day at the same false-alarm rate as the p-charts (about one false signal per 860–940 days per group, decision interval h = 5), it detects a 2× drift in a median of 6 days (90th percentile 11), against 10 days (90th percentile 24) for the p-chart. It reaches 3–4 days only at 5–13× more false alarms (h = 2.5–3.5). Left out so the charts stay readable for operators, and because the per-unit gate already contains each bad unit during a slow drift.
