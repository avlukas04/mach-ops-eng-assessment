"""
Tests for dashboard/app.py: the gate's API, sign-off rules and live push.

The replayed database holds two real build days (02-06 and 02-18, about
14,000 units), which contain both known defects. Run from the repository root:
  python3 -m unittest tests.test_app -v
"""

import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import warnings  # noqa: E402

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import spc_detector as spc  # noqa: E402
from dashboard import app as A  # noqa: E402
from pipeline import runner as rn  # noqa: E402
from pipeline.source import Batch, ReplaySource  # noqa: E402
from pipeline.store import Store, columns_from_frame  # noqa: E402
from test_health import SCORING, make_units, write  # noqa: E402

CSV = os.path.join(ROOT, "manufacturing.csv")
DAYS = ("2026-02-06", "2026-02-18")


def wait_for(cond, timeout=30.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


FIXTURE = {}


def setUpModule():
    if not os.path.exists(CSV):
        return
    FIXTURE["tmp"] = tempfile.TemporaryDirectory()
    FIXTURE["db"] = os.path.join(FIXTURE["tmp"].name, "gate.db")
    df = pd.read_csv(CSV, low_memory=False)
    FIXTURE["sub"] = df[df["build_date_utc"].str[:10].isin(DAYS)].reset_index(drop=True)
    r = rn.Runner(ReplaySource(frame=FIXTURE["sub"]), FIXTURE["db"])
    r.run_forever(until_idle=True)
    r.close()


def tearDownModule():
    if "tmp" in FIXTURE:
        FIXTURE["tmp"].cleanup()


@unittest.skipUnless(os.path.exists(CSV), "manufacturing.csv not present")
class ReplayBase(unittest.TestCase):
    """Each class gets its own copy of the replayed database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "gate.db")
        src = Store(FIXTURE["db"], readonly=True)
        src.conn.execute("VACUUM INTO ?", (cls.db_path,))
        src.close()
        cls.sub = FIXTURE["sub"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.gate = A.Gate(db_path=self.db_path, run_runner=False, snapshot_s=0.2)
        self.client = TestClient(A.create_app(self.gate))
        self.client.__enter__()
        wait_for(lambda: self.gate.state == "ready")

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def get(self, path, code=200):
        r = self.client.get(path)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()


class HoldsTest(ReplayBase):
    def test_both_known_defects_are_held(self):
        holds = self.get("/api/quarantine?open=true")["holds"]
        self.assertEqual({h["vehicle_id"]: h["held_at"] for h in holds},
                         {"GLDR-0421": "2026-02-06T03:00:00", "GLDR-200000": "2026-02-18T07:00:00"})
        for h in holds:
            self.assertEqual((h["lead_hours"], h["final_qc_pass"], h["allowed"]),
                             (1.0, 1, ["RETEST_PASS", "RETEST_FAIL_REPLACED"]))
        alarms = self.get("/api/alerts?layer=product&severity=ALARM")["alerts"]
        self.assertEqual(sorted(a["group"] for a in alarms), ["GLDR-0421", "GLDR-200000"])

    def test_unit_record_and_z_grid(self):
        u = self.get("/api/units/GLDR-0421")
        self.assertEqual(u["score"]["verdict"], "QUARANTINE")
        rows = {r["servo"]: r for r in u["z_grid"]["servos"]}
        self.assertAlmostEqual(rows["aileron_l"]["d"], 8.57, places=2)
        self.assertEqual(rows["aileron_l"]["level"], "QUARANTINE")
        self.assertEqual([r["level"] for s, r in rows.items() if s != "aileron_l"], [None] * 4)
        self.assertAlmostEqual(u["score"]["d_aileron_l"], rows["aileron_l"]["d"], places=3)
        self.assertEqual([e["event_type"] for e in u["events"]], ["QUARANTINE_RAISED"])
        self.assertTrue(u["hold"]["open"])
        self.assertEqual(len(u["history"]), 1)
        self.get("/api/units/NOPE-1", 404)


class SignOffTest(ReplayBase):

    def test_sign_off_rules(self):
        vid = "GLDR-200000"
        post = lambda body: self.client.post(f"/api/units/{vid}/events", json=body)  # noqa: E731
        r = post({"event_type": "RELEASED", "actor": "J. Doe", "note": "  "})
        self.assertEqual(r.status_code, 422)
        r = post({"event_type": "RELEASED", "actor": "J. Doe"})
        self.assertEqual(r.status_code, 422)
        r = post({"event_type": "QUARANTINE_RAISED", "actor": "J. Doe", "note": "x"})
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/api/units/NOPE-1/events",
                             json={"event_type": "RETEST_PASS", "actor": "J. Doe", "note": "x"})
        self.assertEqual(r.status_code, 404)
        r = post({"event_type": "RELEASED", "actor": "J. Doe", "note": "looks fine"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("passing retest", r.json()["detail"])
        r = post({"event_type": "RETEST_PASS", "actor": "J. Doe", "note": "bench retest 3x clean"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("RELEASED", r.json()["hold"]["allowed"])
        r = post({"event_type": "RELEASED", "actor": "J. Doe", "note": "released after retest"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["event"]["at_sim"], "2026-02-18T23:00:00")
        self.assertFalse(r.json()["hold"]["open"])
        # Every screen sees it on the next snapshot: no longer an open hold or an ALARM
        wait_for(lambda: vid not in {h["vehicle_id"] for h in self.get("/api/quarantine?open=true")["holds"]})
        self.assertNotIn(vid, [a["group"] for a in self.get("/api/alerts?layer=product")["alerts"]])
        closed = self.get("/api/quarantine?open=false")["holds"]
        self.assertEqual([(h["vehicle_id"], h["released"]["actor"]) for h in closed], [(vid, "J. Doe")])
        r = post({"event_type": "RELEASED", "actor": "J. Doe", "note": "again"})
        self.assertEqual(r.status_code, 409)


class ViewsTest(ReplayBase):

    def test_views(self):
        s = self.get("/api/summary")
        self.assertEqual(s["layers"]["product"], "red")
        self.assertEqual(s["final_qc_pass_share"], 1.0)
        t = self.get("/api/throughput?by=line&period=hour&last=3")
        self.assertEqual([x["hour"] for x in t["series"]],
                         ["2026-02-18T21:00:00", "2026-02-18T22:00:00", "2026-02-18T23:00:00"])
        self.assertEqual(len(t["series"][0]["units"]), 6)
        self.get("/api/throughput?by=plant&period=hour", 422)
        d = self.get("/api/throughput?by=plant&period=day")
        self.assertEqual([x["day"] for x in d["series"]], list(DAYS))
        self.assertEqual(sum(sum(x["units"].values()) for x in d["series"]), len(self.sub))
        p = self.get("/api/charts/pchart?by=line")
        self.assertEqual(len(p["charts"]), 6)
        self.get("/api/charts/pchart?by=nope", 404)
        self.get("/api/charts/pchart?by=line&group=XX/LN9", 404)
        lot = self.get("/api/charts/pchart?by=aileron_l.lot_id")
        self.assertTrue(lot["charts"])
        tr = self.get("/api/charts/trend?servo=aileron_l&metric=sweep_current_ma")
        self.assertEqual([x["day"] for x in tr["days"]], list(DAYS))
        self.assertLess(tr["days"][0]["q1"], tr["days"][0]["median"])
        self.get("/api/charts/trend?servo=wing&metric=sweep_current_ma", 422)
        self.assertIn("any", self.get("/api/warnings")["open_warnings"])
        self.assertTrue(self.get("/api/ceilings")["ceilings"])
        rt = self.get("/api/retest?limit=5")["units"]
        self.assertEqual(len(rt), 5)
        self.assertEqual([u["worst_d"] for u in rt], sorted((u["worst_d"] for u in rt), reverse=True))
        self.assertEqual(rt[0]["allowed"], ["RETEST_PASS", "RETEST_FAIL_REPLACED"])
        h = self.get("/api/health")
        self.assertIn("correlation", h["gate"])
        st = self.get("/api/status")
        self.assertEqual((st["state"], st["scoring_baseline"]["version"]), ("ready", SCORING["version"]))


class CountsTest(ReplayBase):

    def test_counts_match_the_store_and_classify(self):
        s = self.get("/api/summary")
        db = Store(self.db_path, readonly=True)
        self.assertEqual(s["verdicts"], db.verdict_counts())
        db.close()
        res, _, _ = spc.classify(self.sub, SCORING)
        self.assertEqual(s["verdicts"], {v: int((res["verdict"] == v).sum())
                                         for v in ("QUARANTINE", "FLAG", "PASS")})
        self.assertEqual(s["units"], len(self.sub))


class SyntheticApiTest(unittest.TestCase):
    """Small synthetic databases: start-up, replaced servos, the clean-retest path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "gate.db")
        self.rng = np.random.default_rng(3)
        units = make_units(200, "2026-02-01T00:00:00", self.rng)
        self.store = Store(self.db_path, columns=columns_from_frame(units))
        write(self.store, units, "2026-02-01T00:00:00")
        self.store.set_meta({"sim_time": "2026-02-01T00:00:00"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def bad(self, sigmas, start=5000):
        u = make_units(1, "2026-02-01T01:00:00", np.random.default_rng(9), start=start)
        for m in spc.METRICS:
            c = spc.col("aileron_l", m)
            u[c] = SCORING["measures"][c]["median"] + sigmas * SCORING["measures"][c]["sigma"]
        return u

    def test_loading_answers_and_snapshot_endpoints_wait(self):
        gate = A.Gate(db_path=self.db_path, run_runner=False, snapshot_s=0.1)
        release = threading.Event()
        real = gate.health.refresh
        gate.health.refresh = lambda: (release.wait(10), real())[1]
        with TestClient(A.create_app(gate)) as client:
            st = client.get("/api/status").json()
            self.assertEqual((st["state"], st["snapshot_seq"]), ("loading", 0))
            r = client.get("/api/summary")
            self.assertEqual((r.status_code, r.headers.get("retry-after")), (503, "2"))
            self.assertEqual(client.get("/api/charts/pchart?by=line").status_code, 503)
            release.set()
            wait_for(lambda: client.get("/api/summary").status_code == 200)
            self.assertEqual(client.get("/api/status").json()["state"], "ready")

    def test_before_any_units_views_are_empty_not_errors(self):
        gate = A.Gate(db_path=os.path.join(self.tmp.name, "none.db"), run_runner=False, snapshot_s=0.1)
        with TestClient(A.create_app(gate)) as client:
            wait_for(lambda: gate.state == "ready")
            self.assertEqual(client.get("/api/charts/pchart?by=line").json()["charts"], [])
            self.assertEqual(client.get("/api/quarantine").json()["holds"], [])
            s = client.get("/api/summary").json()
            self.assertEqual((s["units"], s["layers"]["gate"]), (0, "red"))  # no database: ALARM
            self.assertEqual(client.get("/api/units/V000001").status_code, 503)

    def test_replaced_servo_still_bad_raises_alarm_again(self):
        write(self.store, self.bad(4), "2026-02-01T01:00:00")
        gate = A.Gate(db_path=self.db_path, run_runner=False, snapshot_s=0.1)
        with TestClient(A.create_app(gate)) as client:
            wait_for(lambda: gate.state == "ready")
            r = client.post("/api/units/V005000/events",
                            json={"event_type": "RETEST_FAIL_REPLACED", "actor": "J. Doe", "note": "servo swapped"})
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.json()["hold"]["allowed"], [])
            r = client.post("/api/units/V005000/events",
                            json={"event_type": "RETEST_PASS", "actor": "J. Doe", "note": "too early"})
            self.assertEqual(r.status_code, 409)
            write(self.store, self.bad(5), "2026-02-01T02:00:00")  # the replacement is bad too
            hold = wait_for(lambda: next((h for h in client.get("/api/quarantine?open=true").json()["holds"]
                                          if h["gate_retest"]), None))
            self.assertEqual((hold["verdict"], hold["gate_retest"]["verdict"], hold["awaiting_rescore"]),
                             ("QUARANTINE", "QUARANTINE", False))
            alarms = client.get("/api/alerts?layer=product").json()["alerts"]
            self.assertEqual([a["severity"] for a in alarms], ["ALARM"])

    def test_clean_gate_retest_allows_release_but_does_not_release(self):
        write(self.store, self.bad(4), "2026-02-01T01:00:00")
        clean = make_units(1, "2026-02-01T01:00:00", np.random.default_rng(9), start=5000)
        write(self.store, clean, "2026-02-01T02:00:00")  # new readings, clean
        gate = A.Gate(db_path=self.db_path, run_runner=False, snapshot_s=0.1)
        with TestClient(A.create_app(gate)) as client:
            wait_for(lambda: gate.state == "ready")
            hold = client.get("/api/quarantine?open=true").json()["holds"][0]
            self.assertEqual((hold["vehicle_id"], hold["verdict"], hold["gate_retest"]["verdict"]),
                             ("V005000", "PASS", "PASS"))
            self.assertIn("ready to release", client.get("/api/alerts?layer=product").json()["alerts"][0]["message"])
            r = client.post("/api/units/V005000/events",
                            json={"event_type": "RELEASED", "actor": "J. Doe", "note": "gate retest clean"})
            self.assertEqual(r.status_code, 201, r.text)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StreamTest(unittest.TestCase):
    """A real server on a real socket: a batch lands, every screen hears it."""

    def setUp(self):
        import uvicorn  # noqa: F401
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "gate.db")
        self.go = threading.Event()
        frames = [make_units(50, f"2026-02-01T{h:02d}:00:00", np.random.default_rng(h), start=100 * h)
                  for h in range(3)]
        go = self.go

        class GatedSource:
            """Hour 0 at once; hours 1-2 only after the test says go."""
            def fetch_new(self, cursor):
                i = cursor or 0
                if i >= len(frames) or (i > 0 and not go.is_set()):
                    return Batch(pd.DataFrame(), i, f"2026-02-01T{min(i, 2):02d}:00:00")
                return Batch(frames[i], i + 1, f"2026-02-01T{i:02d}:00:00")

        self.gate = A.Gate(db_path=self.db_path, source=GatedSource(), poll_s=0.05, snapshot_s=0.2)
        self.port = free_port()
        self.server = A.make_server(A.create_app(self.gate), port=self.port)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_for(lambda: self.server.started)
        self.url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self.tmp.cleanup()

    def test_stream_sends_an_event_when_a_batch_lands(self):
        wait_for(lambda: httpx.get(self.url + "/api/summary").status_code == 200
                 and httpx.get(self.url + "/api/summary").json()["units"] == 50)
        events = []
        empty = httpx.get(self.url + "/api/charts/pchart?by=line").json()
        self.assertEqual(len(empty["charts"]), 1)   # one line so far: HB-PLANT-1/LN1
        with httpx.stream("GET", self.url + "/api/stream", timeout=20) as r:
            self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
            lines = r.iter_lines()
            t_go = None
            for line in lines:
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    events.append((time.monotonic(), ev))
                    if t_go is None:
                        t_go = time.monotonic()
                        self.go.set()   # first event = where things stand; now release two batches
                    elif ev["new_units"]:
                        break
        first, latest = events[0][1], events[-1][1]
        self.assertGreater(latest["seq"], first["seq"])
        self.assertGreater(latest["new_units"], 0)
        self.latency = events[-1][0] - t_go
        self.assertLess(self.latency, 3.0)
        # The ids in the event match the alert list
        s = httpx.get(self.url + "/api/summary").json()
        alerts = httpx.get(self.url + "/api/alerts").json()["alerts"]
        self.assertEqual(sum(s["alert_counts"].values()), len(alerts))
        self.assertEqual(latest["alert_counts"], s["alert_counts"])
        self.assertLessEqual(set(latest["alerts_added"]), {a["id"] for a in alerts})
        print(f"\n  [stream] batch released → event received in {self.latency * 1000:.0f} ms "
              f"(seq {first['seq']} → {latest['seq']}, new units {latest['new_units']})")

    def test_stop_with_a_screen_still_connected(self):
        """A stop signal must not wait forever for open streams."""
        got = threading.Event()

        def screen():
            try:
                with httpx.stream("GET", self.url + "/api/stream", timeout=30) as r:
                    for line in r.iter_lines():
                        got.set()
            except httpx.HTTPError:
                pass  # cut by the server: expected

        threading.Thread(target=screen, daemon=True).start()
        self.assertTrue(got.wait(10))
        t = time.monotonic()
        self.server.handle_exit(signal.SIGTERM, None)  # what a stop signal calls
        self.thread.join(timeout=15)
        took = time.monotonic() - t
        self.assertFalse(self.thread.is_alive())
        self.assertLess(took, A.SHUTDOWN_GRACE_S)  # streams ended at once, not cut at the backstop
        self.assertTrue(self.gate._stop.is_set())   # lifespan shutdown ran: runner told to stop
        print(f"\n  [shutdown] stopped with a screen connected in {took:.1f} s")


if __name__ == "__main__":
    unittest.main()
