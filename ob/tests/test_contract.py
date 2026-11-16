"""Contract-level tests: units, ranges, recovery bounds and URL emission.

These run in milliseconds and guard the two bugs that this work fixed:
SRT ``latency`` emitted in the wrong unit, and a stale pacer-seconds claim.
Run with ``python3 -m unittest discover -s tests`` from ``ob/``.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ob import config
from ob.contract import (ContractError, build_impairment, describe,
                         gateway_url, pacer_seconds, receiver_url,
                         recovery_contract, validate_session)


class LatencyUnits(unittest.TestCase):
    def test_url_latency_is_microseconds(self):
        for ms in (20, 80, 200, 400, 800, 8000):
            self.assertIn(f"latency={ms * 1000}&", gateway_url("r", ms))
            self.assertIn(f"latency={ms * 1000}&", receiver_url("r", ms))
            self.assertNotIn(f"latency={ms}&", gateway_url("r", ms))

    def test_default_matches_config(self):
        self.assertEqual(config.SRT_LATENCY_MS_DEFAULT, 200)
        self.assertIn("latency=200000&",
                      gateway_url("r", config.SRT_LATENCY_MS_DEFAULT))

    def test_ports_use_config(self):
        self.assertIn(f":{config.RELAY_IN_PORT}?", gateway_url("r", 200))
        self.assertIn(f":{config.SRT_RECEIVER_PORT}?", receiver_url("r", 200))


class Ranges(unittest.TestCase):
    def test_latency_bounds(self):
        validate_session(20, 600_000)
        validate_session(8000, 600_000)
        for bad in (19, 8001, "abc"):
            with self.assertRaises(ContractError):
                validate_session(bad, 600_000)

    def test_pacer_bounds(self):
        validate_session(200, config.PACER_BYTES_MIN)
        validate_session(200, config.PACER_BYTES_MAX)
        for bad in (0, config.PACER_BYTES_MIN - 1,
                    config.PACER_BYTES_MAX + 1):
            with self.assertRaises(ContractError):
                validate_session(200, bad)

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ContractError):
            build_impairment("burst-los")

    def test_unknown_knob_rejected(self):
        with self.assertRaises(ContractError):
            build_impairment("healthy", {"pkt_loss": 5})

    def test_out_of_range_override_rejected(self):
        for field, value in (("loss_pct", 101), ("jitter_ms", -1),
                             ("burst_k", -1), ("reorder_pct", 100.5)):
            with self.assertRaises(ContractError):
                build_impairment("healthy", {field: value})

    def test_profile_is_not_mutated(self):
        imp = build_impairment("jitter")
        imp.jitter_ms = 999
        self.assertEqual(config.PROFILES["jitter"].jitter_ms, 120)


class RecoveryBounds(unittest.TestCase):
    def test_recoverable_bound_is_the_budget(self):
        for ms in (20, 200, 2000):
            facts = recovery_contract(ms, config.PACER_BYTES_DEFAULT)
            self.assertEqual(facts["recoverable_media_loss_ms"], ms)
            self.assertEqual(facts["srt_latency_us"], ms * 1000)

    def test_blackhole_recoverable_only_when_shorter_than_budget(self):
        imp = build_impairment("short_outage")   # 1.2 s
        self.assertFalse(recovery_contract(
            250, config.PACER_BYTES_DEFAULT, imp)["planned_blackhole_recoverable"])
        self.assertTrue(recovery_contract(
            2000, config.PACER_BYTES_DEFAULT, imp)["planned_blackhole_recoverable"])

    def test_pacer_default_is_seconds_not_fraction(self):
        # 600000 B at ~1.6 Mbps is ~2.9 s, not the old (wrong) 0.4 s
        secs = pacer_seconds(config.PACER_BYTES_DEFAULT)
        self.assertGreater(secs, 2.5)
        self.assertLess(secs, 3.2)

    def test_unbounded_has_no_pacer_window(self):
        facts = recovery_contract(200, 600_000, unbounded=True)
        self.assertIsNone(facts["pacer_media_s"])
        self.assertNotIn("retransmit_source_window_ms", facts)


class Description(unittest.TestCase):
    def test_describe_units_are_explicit(self):
        d = describe()
        self.assertEqual(d["latency"]["operator_unit"], "ms")
        self.assertEqual(d["latency"]["srt_url_unit"], "us")
        self.assertEqual(d["latency"]["conversion"], "latency_us = latency_ms * 1000")
        self.assertTrue(d["pacer"]["unbounded_is_comparison_only"])
        self.assertAlmostEqual(
            d["pacer"]["default_media_s_at_declared_envelope"],
            round(pacer_seconds(config.PACER_BYTES_DEFAULT), 2))


if __name__ == "__main__":
    unittest.main()
