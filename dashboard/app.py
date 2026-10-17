"""
Gate service — API and live push (Step 5 of the dashboard plan)
===============================================================
One process, three parts, each with its own SQLite connection:

  runner thread   polls the factory feed, scores, stores (Step 3). Starts with
                  the server, so detection runs whether or not anyone watches.
  health worker   one thread owns pipeline.health.Health. After every batch,
                  and at least once a second (so a silent feed or a dead
                  runner shows up without a batch), it refreshes and saves a
                  snapshot. Health is not thread-safe; only this worker and
                  calls made under its lock touch it.
  request handlers read the saved snapshot. Only one unit's record, the
                  retest list and a reading trend touch the database or the
                  in-memory units, so 50 open screens cost what one does.

The factory simulator is a separate process; the gate and the factory share
only the feed.

Start-up: the first snapshot loads every stored unit (about 7 s for the full
month). Until it is ready /api/status answers "loading" and the snapshot
endpoints return 503 with Retry-After, instead of the server being silent.

Live push (/api/stream): server-sent events. One small event per change
(new units, an alert raised or cleared, a status change), carrying the
snapshot number and alert ids; the page refetches only its open tab. A
keep-alive comment every 15 s; each client keeps only the newest event, so a
slow screen skips ahead instead of falling behind.

Sign-offs (POST /api/units/{id}/events) follow the store's hold rules
(pipeline.store, "Holds"): 422 if a field is missing, 404 for an unknown
unit, 409 with the reason if the action isn't allowed in the unit's state.

Usage (from the repository root)
--------------------------------
  python3 tools/factory_simulator.py --speed 2          # in one terminal
  python3 -m dashboard.app                              # in another
  python3 -m dashboard.app --no-runner --db PATH        # view a database only
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import spc_detector as spc  # noqa: E402
from pipeline import health as H  # noqa: E402
from pipeline import store as st  # noqa: E402
from pipeline.runner import DEFAULT_DB, DEFAULT_FEED, Runner, read_baseline  # noqa: E402
from pipeline.source import MesSource  # noqa: E402

log = logging.getLogger("gate")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
KEEPALIVE_S = 15
SHUTDOWN_GRACE_S = 3  # open streams never end on their own; cut them after this
RETRY_MS = 3000
CHANGE_KEYS = ("status", "layers", "alert_counts")


def to_json(obj):
    """JSON-safe copy: numpy scalars and NaN become plain values / null."""
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if f != f or f in (float("inf"), float("-inf")) else f
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def z_grid(unit, base):
    """The 5x5 grid of robust z scores (servos x readings) for one unit,
    recomputed from its stored readings with the pinned scoring baseline
    (the store keeps only D per servo and the worst z)."""
    row = {c: unit.get(c) for c in spc.MEASURE_COLS}
    df = pd.DataFrame([row]).apply(pd.to_numeric, errors="coerce")
    z, D, saturated, _ = spc.compute_scores(df, base)
    rules = base["rules"]

    def level(v, flag, quarantine):
        return "QUARANTINE" if v >= quarantine else "FLAG" if v >= flag else None

    servos = []
    for s, servo in enumerate(spc.SERVOS):
        cells = []
        for m, metric in enumerate(spc.METRICS):
            raw = df.iloc[0][spc.col(servo, metric)]
            zv = float(z[0, s, m])
            cells.append({"metric": metric, "value": None if pd.isna(raw) else float(raw),
                          "z": round(zv, 3), "missing": bool(pd.isna(raw)),
                          "saturated": bool(saturated[0, s, m]),
                          "level": level(zv, rules["flag_z"], rules["quarantine_z"])})
        d = float(D[0, s])
        servos.append({"servo": servo, "d": round(d, 3),
                       "level": level(d, rules["flag_d"], rules["quarantine_d"]), "cells": cells})
    return {"metrics": spc.METRICS, "rules": rules, "servos": servos}


class Gate:
    """The runner, the health worker and the latest snapshot."""

    def __init__(self, feed_path=DEFAULT_FEED, db_path=DEFAULT_DB,
                 baseline_path=spc.DEFAULT_BASELINE, health_baseline=H.DEFAULT_HEALTH_BASELINE,
                 source=None, run_runner=True, poll_s=1.0, snapshot_s=1.0):
        self.db_path = db_path
        self.snapshot_s = snapshot_s
        self.base, self.base_sha = read_baseline(baseline_path)  # pinned, as the runner does
        self.runner = (Runner(source or MesSource(feed_path), db_path, baseline_path,
                              poll_s=poll_s, listeners=[self._on_batch]) if run_runner else None)
        self.health = H.Health(db_path, health_baseline, baseline_path)
        self.lock = threading.Lock()        # held while Health is in use
        self._db_lock = threading.Lock()    # the API's own read connection
        self._read = None
        self._write = None                  # sign-offs, opened on first use
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._threads = []
        self.state = "loading"
        self.started_at = st.utc_now()
        self.error = None
        self.snapshot = None
        self.seq = 0
        self.event = None
        self._trend_cache = {}
        self._loop = None
        self._clients = set()
        self.closing = False

    # ---- lifecycle

    def start(self, loop=None):
        self._loop = loop
        if self.runner:
            self._threads.append(threading.Thread(target=self._run_runner, name="runner", daemon=True))
        self._threads.append(threading.Thread(target=self._run_health, name="health", daemon=True))
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        self._dirty.set()
        for t in self._threads:
            t.join(timeout=10)
        if self.runner:
            self.runner.close()
        self.health.close()
        for s in (self._read, self._write):
            if s:
                s.close()

    def _run_runner(self):
        try:
            self.runner.run_forever(self._stop)
        except Exception:  # run_forever retries everything; this is a bug
            log.exception("runner thread ended")

    def _on_batch(self, summary):
        self._dirty.set()

    def _run_health(self):
        while not self._stop.is_set():
            try:
                with self.lock:
                    n = self.health.refresh()
                    snap = to_json(self.health.snapshot(refresh=False))
                    if n:
                        self._trend_cache.clear()
                self._publish(snap, n)
                self.error = None
            except Exception as e:  # logged, retried next round; the old snapshot shows its age
                self.error = f"{type(e).__name__}: {e}"
                log.exception("health snapshot failed")
            self._dirty.wait(self.snapshot_s)
            self._dirty.clear()

    def refresh_now(self):
        """Ask the worker for a new snapshot now (after a sign-off)."""
        self._dirty.set()

    # ---- snapshots and push

    def _publish(self, snap, new_units):
        prev = self.snapshot
        self.snapshot = snap
        self.state = "ready"
        old_ids = {a["id"]: a["severity"] for a in prev["alerts"]} if prev else {}
        new_ids = {a["id"]: a["severity"] for a in snap["alerts"]}
        added = sorted(i for i in new_ids if old_ids.get(i) != new_ids[i])
        cleared = sorted(i for i in old_ids if i not in new_ids)
        changed = (prev is None or new_units or added or cleared
                   or any(prev.get(k) != snap.get(k) for k in CHANGE_KEYS))
        if not changed:
            return
        self.seq += 1
        self.event = {"seq": self.seq, "generated_at": snap["generated_at"],
                      "as_of_sim": snap["as_of_sim"], "status": snap["status"],
                      "layers": snap["layers"], "alert_counts": snap["alert_counts"],
                      "new_units": new_units, "alerts_added": added, "alerts_cleared": cleared}
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._broadcast, self.event)

    def _broadcast(self, event):
        for q in list(self._clients):
            if q.full():          # keep only the newest event
                q.get_nowait()
            q.put_nowait(event)

    def close_streams(self):
        """End every open stream now (called on a stop signal, from any thread)."""
        self.closing = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._broadcast, None)

    def subscribe(self):
        q = asyncio.Queue(maxsize=1)
        self._clients.add(q)
        return q

    def unsubscribe(self, q):
        self._clients.discard(q)

    # ---- database access for requests

    def reader(self):
        if self._read is None and os.path.exists(self.db_path):
            self._read = st.Store(self.db_path, readonly=True)
        return self._read

    def writer(self):
        if self._write is None and os.path.exists(self.db_path):
            self._write = st.Store(self.db_path)
        return self._write

    def trend(self, servo, metric):
        key = (servo, metric)
        with self.lock:
            if key not in self._trend_cache:
                self._trend_cache[key] = to_json(self.health.reading_trend(servo, metric))
            return self._trend_cache[key]


class SignOff(BaseModel):
    event_type: str
    actor: str
    note: str

    @field_validator("actor", "note")
    @classmethod
    def not_blank(cls, v):
        if not v.strip():
            raise ValueError("required")
        return v.strip()

    @field_validator("event_type")
    @classmethod
    def known(cls, v):
        if v not in st.HUMAN_EVENTS:
            raise ValueError(f"must be one of {list(st.HUMAN_EVENTS)}")
        return v


def create_app(gate=None, **gate_args):
    gate = gate or Gate(**gate_args)

    @asynccontextmanager
    async def lifespan(app):
        gate.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            await asyncio.get_running_loop().run_in_executor(None, gate.stop)

    app = FastAPI(title="Mach quality gate", lifespan=lifespan)
    app.state.gate = gate

    def snap():
        s = gate.snapshot
        if s is None:
            raise HTTPException(503, detail={"state": gate.state, "error": gate.error},
                                headers={"Retry-After": "2"})
        return s

    def reader():
        db = gate.reader()
        if db is None:
            raise HTTPException(503, detail={"state": "no database yet"}, headers={"Retry-After": "2"})
        return db

    @app.get("/api/status")
    def status():
        s = gate.snapshot
        return to_json({
            "state": gate.state, "started_at": gate.started_at, "error": gate.error,
            "snapshot_seq": gate.seq, "generated_at": s["generated_at"] if s else None,
            "status": s["status"] if s else None,
            "runner": gate.runner.status if gate.runner else None,
            "scoring_baseline": {"version": gate.base["version"], "sha256": gate.base_sha},
            "health_baseline": {"version": gate.health.hb["version"], "sha256": gate.health.hb_sha},
            "timings": s["timings"] if s else None})

    @app.get("/api/summary")
    def summary():
        s = snap()
        p, q = s["product"], s["quality"]
        return {"generated_at": s["generated_at"], "as_of_sim": s["as_of_sim"],
                "gate_sim_time": s["gate_sim_time"], "status": s["status"], "layers": s["layers"],
                "alert_counts": s["alert_counts"], "verdicts": p["verdicts"], "units": p["units"],
                "open_holds": sum(h["open"] for h in p["holds"]),
                "final_qc_pass_share": q["final_qc_pass_share"],
                "units_latest_hour": sum(s["throughput"]["units_latest_hour"].values()),
                "freshness": {"heartbeat_age_s": (s["gate"].get("runner") or {}).get("heartbeat_age_s"),
                              "lag_build_hours": s["gate"].get("lag_build_hours"),
                              "feed": s["gate"].get("feed")},
                "baselines": s["gate"].get("baseline"), "health_baseline": s["health_baseline"]}

    @app.get("/api/alerts")
    def alerts(layer: str | None = None, severity: str | None = None):
        out = [a for a in snap()["alerts"]
               if (layer is None or a["layer"] == layer) and (severity is None or a["severity"] == severity)]
        return {"alerts": out}

    @app.get("/api/quarantine")
    def quarantine(open: str = Query("all", pattern="^(true|false|all)$")):
        holds = snap()["product"]["holds"]
        if open != "all":
            holds = [h for h in holds if h["open"] == (open == "true")]
        return {"holds": holds}

    @app.get("/api/throughput")
    def throughput(by: str = Query("line", pattern="^(line|shift|plant)$"),
                   period: str = Query("day", pattern="^(hour|day)$"),
                   last: int = Query(48, ge=1, le=2000)):
        t = snap()["throughput"]
        base = {"latest_hour": t["latest_hour"], "normal_per_hour": t["normal_per_hour"],
                "rework": t["rework"]}
        if period == "hour":
            if by != "line":
                raise HTTPException(422, "hourly counts are kept by line only")
            hours = sorted(t["by_hour_line"])[-last:]
            return dict(base, by="line", period="hour",
                        series=[{"hour": h, "units": t["by_hour_line"][h]} for h in hours])
        days = sorted(t["by_day"])[-last:]
        return dict(base, by=by, period="day",
                    series=[{"day": d, "units": t["by_day"][d][by]} for d in days])

    @app.get("/api/charts/pchart")
    def pchart(by: str = "fleet", group: str | None = None):
        s = snap()["stability"]
        dims = (["fleet"] + list(H.VEHICLE_DIMS)
                + [f"{sv}.{d}" for sv in H.SERVOS for d in H.SERVO_DIMS])
        if by not in dims:
            raise HTTPException(404, f"unknown chart group type {by!r}; one of {dims}")
        charts = [c for c in s["charts"] if c["dim"] == by and (group is None or c["group"] == group)]
        if group is not None and not charts:
            raise HTTPException(404, f"no {by} chart for {group!r} yet")
        return {"by": by, "summary": s["summary"].get(by), "charts": charts}  # [] until units arrive

    @app.get("/api/charts/trend")
    def trend(servo: str, metric: str):
        if servo not in spc.SERVOS or metric not in spc.METRICS:
            raise HTTPException(422, f"servo in {spc.SERVOS}, metric in {spc.METRICS}")
        snap()
        return gate.trend(servo, metric)

    @app.get("/api/warnings")
    def warnings():
        return {"open_warnings": snap()["quality"]["open_warnings"]}

    @app.get("/api/ceilings")
    def ceilings():
        return {"ceilings": snap()["gate"].get("ceilings", {})}

    @app.get("/api/retest")
    def retest(limit: int = Query(50, ge=1, le=1000)):
        db = reader()
        with gate._db_lock:
            rows = [dict(r) for r in db.conn.execute(
                "SELECT s.vehicle_id, s.worst_d, s.worst_servo, s.worst_metric, s.worst_z, s.reasons, "
                "u.build_date_utc, u.factory_site, u.line_id, u.build_shift FROM scores s "
                "JOIN units u USING (vehicle_id) WHERE s.verdict='FLAG' "
                "ORDER BY s.worst_d DESC LIMIT ?", (limit,))]
            for r in rows:
                h = st.hold_status(db.get_events(r["vehicle_id"]), "FLAG")
                r["allowed"], r["awaiting_rescore"] = h["allowed"], h["awaiting_rescore"]
        return to_json({"units": rows})

    @app.get("/api/units/{vehicle_id}")
    def unit(vehicle_id: str):
        db = reader()
        with gate._db_lock:
            u = db.get_unit(vehicle_id)
            if u is None:
                raise HTTPException(404, f"unknown vehicle {vehicle_id!r}")
            score = db.get_score(vehicle_id)
            out = {"vehicle_id": vehicle_id, "unit": u, "score": score,
                   "history": db.get_score_history(vehicle_id), "events": db.get_events(vehicle_id),
                   "hold": db.hold_status(vehicle_id), "z_grid": z_grid(u, gate.base),
                   "scoring_baseline": gate.base["version"]}
        return to_json(out)

    @app.post("/api/units/{vehicle_id}/events", status_code=201)
    def sign_off(vehicle_id: str, body: SignOff):
        db = gate.writer()
        if db is None:
            raise HTTPException(503, detail={"state": "no database yet"}, headers={"Retry-After": "2"})
        sim = db.get_meta("feed_clock") or db.get_meta("sim_time")
        try:
            event_id = db.add_event(vehicle_id, body.event_type, body.actor, body.note, at_sim=sim)
        except KeyError:
            raise HTTPException(404, f"unknown vehicle {vehicle_id!r}")
        except st.ActionRefused as e:
            raise HTTPException(409, str(e))
        gate.refresh_now()
        event = next(e for e in db.get_events(vehicle_id) if e["id"] == event_id)
        return to_json({"event": event, "hold": db.hold_status(vehicle_id)})

    @app.get("/api/health")
    def health():
        s = snap()
        return {"generated_at": s["generated_at"], "status": s["status"], "layers": s["layers"],
                "gate": s["gate"], "alerts": [a for a in s["alerts"] if a["layer"] == "gate"]}

    @app.get("/api/stream")
    async def stream(request: Request):
        q = gate.subscribe()

        async def events():
            try:
                yield f"retry: {RETRY_MS}\n\n"
                if gate.event:  # where things stand now, so a new screen starts current
                    yield f"id: {gate.event['seq']}\nevent: snapshot\ndata: {json.dumps(gate.event)}\n\n"
                while not gate.closing and not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if ev is None:  # server stopping: the browser reconnects to the next one
                        break
                    yield f"id: {ev['seq']}\nevent: snapshot\ndata: {json.dumps(ev)}\n\n"
            finally:
                gate.unsubscribe(q)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if os.path.isdir(STATIC_DIR):  # the page (Step 6)
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


def make_server(app, host="127.0.0.1", port=8000):
    """A uvicorn server that ends open streams on a stop signal.

    A live stream never finishes by itself, so a plain server would wait
    forever for open screens to disconnect. On SIGINT / SIGTERM every stream
    is ended first; SHUTDOWN_GRACE_S is the backstop. Then the lifespan
    shutdown stops the runner between batches."""
    import uvicorn

    class GateServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            app.state.gate.close_streams()
            super().handle_exit(sig, frame)

    return GateServer(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                     timeout_graceful_shutdown=SHUTDOWN_GRACE_S))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the quality gate: runner, health and API.")
    ap.add_argument("--feed", default=DEFAULT_FEED, help="factory feed database")
    ap.add_argument("--db", default=DEFAULT_DB, help="the gate's own database")
    ap.add_argument("--baseline", default=spc.DEFAULT_BASELINE)
    ap.add_argument("--health-baseline", default=H.DEFAULT_HEALTH_BASELINE)
    ap.add_argument("--no-runner", action="store_true", help="serve an existing database only")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    app = create_app(feed_path=args.feed, db_path=args.db, baseline_path=args.baseline,
                     health_baseline=args.health_baseline, run_runner=not args.no_runner)
    make_server(app, args.host, args.port).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
