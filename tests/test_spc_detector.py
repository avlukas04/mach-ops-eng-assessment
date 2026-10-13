"""
Unit tests for tools/spc_detector.py on a small synthetic fleet, so each
gate rule is checked on its own, independent of manufacturing.csv.

Run from the repository root:
  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import spc_detector as spc  # noqa: E402

N = 2000
CENTER, SPREAD = 100.0, 10.0
CEILING = 150.0


def synthetic_fleet(seed=0):
    rng = np.random.default_rng(seed)
    data = {
        "vehicle_id": [f"V-{i:05d}" for i in range(N)],
        "build_date_utc": ["2026-02-01T00:00:00"] * N,
        "build_shift": ["A"] * N,
        "factory_site": ["PLANT"] * N,
        "line_id": ["LN1"] * N,
    }
    for c in spc.MEASURE_COLS:
        # Healthy readings: a normal truncated at +-2.5 sigma (redrawn, not
        # clipped, so nothing piles up at the edge and looks like a ceiling)
        v = rng.normal(CENTER, SPREAD, N)
        out = np.abs(v - CENTER) > 2.5 * SPREAD
        while out.any():
            v[out] = rng.normal(CENTER, SPREAD, out.sum())
            out = np.abs(v - CENTER) > 2.5 * SPREAD
        data[c] = v
    return pd.DataFrame(data)


def baseline_for(df):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        df.to_csv(f.name, index=False)
        path = f.name
    try:
        return spc.build_baseline(df, path)
    finally:
        os.unlink(path)


def reset_vehicle(df, base, i):
    """Put every reading on vehicle i at the baseline median (z = 0), so a
    test only sees the readings it sets itself."""
    for c in spc.MEASURE_COLS:
        df.loc[i, c] = base["measures"][c]["median"]


def set_z(df, base, i, servo, metric, z):
    """Set a reading to a given robust z-score."""
    m = base["measures"][spc.col(servo, metric)]
    df.loc[i, spc.col(servo, metric)] = m["median"] + z * m["sigma"]


def verdict(df, base, i):
    results, _, _ = spc.classify(df, base)
    return results.iloc[i]


class TestGate(unittest.TestCase):
    def setUp(self):
        self.df = synthetic_fleet()
        self.base = baseline_for(self.df)

    def test_clean_fleet_passes(self):
        results, _, _ = spc.classify(self.df, self.base)
        self.assertEqual(set(results["verdict"]), {"PASS"})

    def test_signature_quarantines_even_when_each_reading_is_below_flag_limit(self):
        # Five readings at z = 2.9 each: no single reading reaches 3,
        # but D = 2.9 * sqrt(5) = 6.48 >= 6.0. This is the GLDR-200000 case.
        reset_vehicle(self.df, self.base, 7)
        for metric in spc.METRICS:
            set_z(self.df, self.base, 7, "aileron_l", metric, 2.9)
        r = verdict(self.df, self.base, 7)
        self.assertEqual(r["verdict"], "QUARANTINE")
        self.assertIn("SIGNATURE aileron_l", r["reasons"])
        self.assertNotIn("HIGH", r["reasons"])

    def test_same_readings_spread_across_servos_do_not_quarantine(self):
        # The same five z = 2.9 readings on five different servos are not a
        # single-servo signature, so D stays low on every servo.
        reset_vehicle(self.df, self.base, 7)
        for servo, metric in zip(spc.SERVOS, spc.METRICS):
            set_z(self.df, self.base, 7, servo, metric, 2.9)
        self.assertEqual(verdict(self.df, self.base, 7)["verdict"], "PASS")

    def test_single_extreme_reading_quarantines(self):
        reset_vehicle(self.df, self.base, 3)
        set_z(self.df, self.base, 3, "rudder", "sweep_time_ms", 5.6)
        r = verdict(self.df, self.base, 3)
        self.assertEqual(r["verdict"], "QUARANTINE")
        self.assertIn("EXTREME rudder.sweep_time_ms", r["reasons"])

    def test_single_high_reading_flags(self):
        reset_vehicle(self.df, self.base, 5)
        set_z(self.df, self.base, 5, "elevon_r", "peak_current_ma", 3.5)
        r = verdict(self.df, self.base, 5)
        self.assertEqual(r["verdict"], "FLAG")
        self.assertIn("HIGH elevon_r.peak_current_ma", r["reasons"])
        self.assertEqual(r["flag_elevon_r"], 1)
        self.assertEqual(r["flag_rudder"], 0)

    def test_elevated_d_flags(self):
        # Three readings at z = 2.8: D = 4.85, between 4.5 and 6.0
        reset_vehicle(self.df, self.base, 9)
        for metric in spc.METRICS[:3]:
            set_z(self.df, self.base, 9, "aileron_r", metric, 2.8)
        r = verdict(self.df, self.base, 9)
        self.assertEqual(r["verdict"], "FLAG")
        self.assertIn("ELEVATED aileron_r", r["reasons"])

    def test_low_readings_never_flag(self):
        reset_vehicle(self.df, self.base, 11)
        set_z(self.df, self.base, 11, "aileron_l", "sweep_current_ma", -8.0)
        r = verdict(self.df, self.base, 11)
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["worst_z"], 0.0)

    def test_missing_reading_fails_closed(self):
        self.df.loc[13, spc.col("elevon_l", "backdrive_torque_nmm")] = np.nan
        r = verdict(self.df, self.base, 13)
        self.assertEqual(r["verdict"], "QUARANTINE")
        self.assertIn("INCOMPLETE", r["reasons"])

    def test_boundaries_are_inclusive(self):
        reset_vehicle(self.df, self.base, 15)
        set_z(self.df, self.base, 15, "rudder", "peak_current_ma", 3.0 + 1e-9)
        self.assertEqual(verdict(self.df, self.base, 15)["verdict"], "FLAG")


class TestBaseline(unittest.TestCase):
    def test_robust_spread_matches_normal_sigma(self):
        rng = np.random.default_rng(1)
        df = synthetic_fleet()
        c = spc.col("rudder", "sweep_current_ma")
        df[c] = rng.normal(CENTER, SPREAD, N)
        m = baseline_for(df)["measures"][c]
        self.assertAlmostEqual(m["median"], CENTER, delta=1.0)
        self.assertAlmostEqual(m["sigma"], SPREAD, delta=1.0)

    def test_ceiling_detected_even_with_one_reading_above_it(self):
        df = synthetic_fleet()
        c = spc.col("aileron_l", "peak_current_ma")
        df.loc[:39, c] = CEILING          # 40 readings pile up at the limit
        df.loc[40, c] = CEILING + 20      # one defective unit reads above it
        base = baseline_for(df)
        self.assertEqual(base["measures"][c]["ceiling"], CEILING)
        r = verdict(df, base, 0)
        self.assertIn("SATURATED aileron_l.peak_current_ma", r["reasons"])

    def test_no_ceiling_on_smooth_tail(self):
        base = baseline_for(synthetic_fleet())
        self.assertIsNone(base["measures"][spc.col("rudder", "sweep_time_ms")]["ceiling"])

    def test_baseline_round_trips_through_json(self):
        df = synthetic_fleet()
        base = baseline_for(df)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(base, f)
            path = f.name
        try:
            loaded = spc.load_baseline(path)
        finally:
            os.unlink(path)
        a, _, _ = spc.classify(df, base)
        b, _, _ = spc.classify(df, loaded)
        self.assertTrue((a["verdict"] == b["verdict"]).all())


if __name__ == "__main__":
    unittest.main()
