# Presentation Guide: How the Assessment Was Conducted

This is the speaking guide for walking a hiring manager, hardware engineers, operations engineers and software engineers through the assessment from start to finish. Every decision has a **why** (the reasoning) and a **how** (what was done and where to find it).

---

## 1. The one-minute version

> A Glide unit crashed after passing QC, and the teams were about to repeat the test with more production units. I found that the left aileron servo stuck for 9 seconds during a bank reversal and kept sticking for the rest of the flight. The factory's own bench test had recorded that servo as the most abnormal of 200,000, three weeks before the flight, but the quality gate never looks at those numbers. One more unit with the same signature is still in the fleet. I built a two-tier detection gate that catches both defective units with about 0.1 expected false alarms per 200,000, and I verified every number in my report with independent code.

Three things to land, in this order:
1. **The answer:** it was manufacturing, specifically one defective servo that QC passed.
2. **The action:** don't repeat the test blindly; hold GLDR-200000; add the gate.
3. **The discipline:** every claim is computed from raw data, checked twice, and its limits are stated.

---

## 2. The story arc (about 20 minutes)

| # | Section | Time | Message |
|:--|:--|:--|:--|
| 1 | The ask and the stakes | 1 min | Repeating the test with more units risks losing more aircraft if the cause is in production. The real question is whether it's manufacturing. |
| 2 | How I approached it | 2 min | Principles: start from the symptom, screen everything before narrowing, test every alternative, compute from raw data, verify independently. |
| 3 | Part 1: what happened in flight | 5 min | Timeline → waypoint misses pin the onset → screen of all 5 surfaces singles out the left aileron → stick-slip signature → every alternative ruled out. Figs 1–3. |
| 4 | Part 1: is it manufacturing? | 2 min | Mechanical friction inside one part, persistent, with power and commands fine. The bench data shows it at +5.93σ before flight. |
| 5 | Part 2: why QC missed it | 3 min | 100% pass rate, exceptions don't block, bench numbers are never judged. 5 Whys. |
| 6 | Part 2: the gate | 4 min | Design choices and why: robust baseline, high side only, combined score, two tiers, fails closed. Results: 2 / 6,498 / 193,500. Standards basis (PAT). |
| 7 | How I know it's right | 2 min | Independent verifier (110 claims), unit tests, and the errors it caught and I fixed. |
| 8 | Live dashboard demo | (when built) | What each team sees; how it runs continuously. |
| 9 | Recommendations and open items | 1 min | Hold GLDR-200000, teardown, add gate at the bench, confirm QC procedure, stiction test. |

---

## 3. Why and how, decision by decision

### Approach

| Decision | Why | How / where |
|:--|:--|:--|
| List every possible cause before looking at data | Engineering already suspected manufacturing. Starting from their guess risks confirming it instead of testing it | Fishbone across 6 categories, fault tree, 5 Whys: `docs/root_cause_analysis.md` |
| Explore visually first, then prove with code | The provided viewer is fast for spotting patterns, but a screenshot can't be rerun or audited | Viewer → scripts. REPORT §3 method paragraph |
| Every number printed by a script | Anyone can rerun and check it; nothing depends on my notes | REPORT §7 maps each script step to the report section it backs |

### Part 1: flight investigation

| Decision | Why | How / where |
|:--|:--|:--|
| Use waypoint misses to time the onset | The flight plan is the definition of "deviated". WP-02 hit (1.3 m) and WP-03 missed (109 m) brackets the failure | Step [4]. Miss distances **recomputed from telemetry** (closest 3D distance within ±3 s) and checked against the provided file: max difference 0.04 m |
| Check descent rate | Separates a steering failure from a lift failure (stall or dive) | Fig 3: steady glide slope throughout |
| Screen all five control surfaces | So the left aileron is chosen by the data, not by suspicion | Step [5]: only the left aileron diverges (24,592 samples, from 246.1 s); the elevons and rudder never pass 4°. REPORT §3.2 |
| Use the full 500 Hz data for anything reporting a max or min | Sampling hides short peaks. The independent check proved it: 10 Hz data understated several ranges | Steps [4]–[7] stream every row. REPORT §7 sampling note |
| Define "episode" explicitly (> 5° gap, gaps < 0.5 s merged, lasting ≥ 0.5 s) | A count is only meaningful with its rule stated. Without the duration rule, two single-sample blips and a 30 ms transient would inflate the count | Step [6]; REPORT §8 explains 14 raw segments vs 11 episodes |
| Test each alternative with its own signal | "Ruled out" has to mean a specific measurement contradicts it | Step [7], H0–H5; REPORT §3.4 table |
| Compare against working parts | Strongest evidence against power, software and design | Right aileron on the same bus (1.3° max gap); same bank flown cleanly 46 s earlier; other 4 servos normal on the bench |
| Say "consistent with" and name the teardown | Telemetry and bench data show the signature, not the physical cause | REPORT §8 Limitations |

### Part 2: manufacturing and detection

| Decision | Why | How / where |
|:--|:--|:--|
| Screen the vehicle's entire build record | The same discipline as the 5-surface screen. Answers "did you only look where you expected?" | `manufacturing_analysis.py` step [3]: only 2 of 74 numeric columns reach 3σ, both left aileron. REPORT §4.2 |
| Check the supplier lot | Decides containment: recall a lot, or screen units? | Step [7]: all 21 lots within 0.5 mA. It's a single-unit defect, so every unit has to be screened |
| Look for the same signature fleet-wide | If it happened once it can happen again | Step [8]: GLDR-200000 (different plant, lot, rework), plus 8 borderline units |
| Separate what the data shows from what is inferred | The QC procedure isn't in the data. Overclaiming loses credibility with the people who run it | REPORT §5: "bench readings never affect the pass decision" is shown; "the gate only checks fault codes" is labelled an inference |
| Robust baseline (median, IQR) | Readings are clipped at instrument limits, which shrinks the ordinary std and inflated false alarms in the first design | `spc_detector.py: build_baseline`. REPORT §6.1 "Why this replaced the first version" |
| High side only | Every friction symptom is a high reading; low readings sit at an instrument floor | `compute_scores` |
| Combined score D per servo | Catches the pattern (several readings high on one servo) that single limits miss, which is how GLDR-200000 is caught | `compute_scores`; REPORT §6.1 signature table |
| Limits from false-alarm math, not tuning | With only 2 known defects, tuning to them would be overfitting | About 0.1 false alarms per 200k for each QUARANTINE limit (checked with scipy) |
| Two tiers | A gate that flags 3% of units becomes a rubber stamp. QUARANTINE blocks; FLAG feeds trend charts | REPORT §6.1 "What FLAG is for" |
| Fail closed; exit code 2 | A flight-safety gate must not pass what it can't measure, and a bench station must be able to stop on it | `classify` (INCOMPLETE), `cmd_score` |
| Detect instrument ceilings | A reading pinned at the fixture limit is unknown, not normal. Found a third ceiling (380 mA) and that GLDR-0421 read above it | `find_ceiling`; REPORT §6.1 |
| Ground it in an existing standard | Shows this isn't invented; the quality team will recognise it | Part Average Testing (AEC-Q001) uses the same robust spread. REPORT §6.3 |
| State the scope | "Similar defects" is ambiguous. The data supports out-of-family actuator performance; other warnings are surfaced, not blocked | REPORT §6 "Scope and assumptions" |

### Verification

| Decision | Why | How / where |
|:--|:--|:--|
| Independent verifier | A bug in one implementation is unlikely to be repeated in a second, separately written one | `tests/verify_report.py`: 110 claims, pandas, full 500 Hz |
| Unit tests per gate rule | Proves each rule does what it says, independent of the real data | `tests/test_spc_detector.py`: 13 tests |
| Correct the record openly | Errors found and fixed are evidence the process works | Commit history: 27.9 → 27.8° rounding, peak-current ceiling, false-alarm wording, 10 Hz ranges, competition ranks |

---

## 4. Tailoring by audience

| Audience | What they care about | Lead with | Go deeper on |
|:--|:--|:--|:--|
| **Hiring manager** | Judgment, impact, ownership, communication | The one-minute version and the recommendation not to repeat the test blindly | How you scoped ambiguity, separated fact from inference, and corrected your own work |
| **Hardware / flight engineers** | Mechanism and physical plausibility | Fig 1: stick at reversal, drift under load, ~100 ms breakaway, 11 recurrences | Rule-outs (right aileron on same bus, earlier bank), current draw 0.34 → 0.39 A, reversal readings normal on the bench → stiction test, teardown plan |
| **Operations / quality engineers** | Process, false alarms, rollout, containment | Why QC missed it (100% pass rate, exceptions don't block) | Two tiers, false-alarm budget, p-charts by lot/line/shift, run at the bench before install, re-screen after rework, spec limits alongside statistical limits |
| **Software engineers** | Correctness, reproducibility, design | The pipeline: scripts → detector → verifier, all rerunnable | Frozen versioned baseline, fail-closed, exit codes, vectorised scoring (~4 s for 200k), tests, live architecture (`docs/LIVE_SYSTEM_DESIGN.md`) |

---

## 5. Questions to expect

| Question | Answer |
|:--|:--|
| Couldn't it be the autopilot? | The command was correct throughout and mirrored on the right aileron. The surface didn't follow its command. |
| Couldn't it be power? | Servo bus ≥ 12.26 V, no brownout, and the right aileron on the same bus tracked within 1.3°. |
| Couldn't it be design? | The same ±20° bank was flown 46 s earlier and hit WP-02 within 1.3 m. |
| Couldn't it be assembly? | Hinge torque, freeplay and screw torque were all normal for this unit. |
| How do you know it's not chance? | +5.93σ is about 1 in 650 million on a normal assumption, and it ranked 1st of 200,000. |
| Why not just put a limit on sweep current? | That catches GLDR-0421 only. GLDR-200000's fault is spread across several readings; the combined score catches it. |
| Won't this flag too many units? | QUARANTINE: 2 units, about 0.1 expected false alarms. FLAG is about the chance rate, which is why it feeds trend charts, not holds. |
| Why the median instead of the mean? | Readings are clipped at instrument floors, which shrinks the std. The first design quarantined 14 units, 12 of them on position error alone, which is consistent with chance. |
| What if a new servo model has different normal values? | Baselines would be kept per servo model. In this data, bench readings are identical across vendors and models. |
| How do you know your numbers are right? | An independent verifier recomputes 110 claims from raw data; the first run found 18 disagreements and every one was fixed. |
| What would you do next? | Hold GLDR-200000 and tear down its servo; put the gate at the bench; confirm the QC procedure; add a stiction test. |
| What don't you know? | The physical cause (needs teardown), the actual QC procedure, the datasheet limits, and the gate's miss rate (only 2 known defects). |

---

## 6. Demo order

1. `python3 analysis/flight_investigation.py`: show step [5], the surface screen, then step [7], the rule-outs.
2. `python3 analysis/manufacturing_analysis.py`: show step [3], the full-record screen, then step [8], GLDR-200000.
3. `python3 tools/spc_detector.py score --vehicle GLDR-0421 --vehicle GLDR-200000`: the z grid and verdicts.
4. `python3 tests/verify_report.py`: "110 of 110 claims match the data."
5. The live dashboard (once built).
