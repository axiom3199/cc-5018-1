"""Tests for the recovery/loss-interval arithmetic (pure functions).

The checks in ``bin/check_latency.py`` assert against these functions, so their
definitions are pinned here with synthetic frame/event data. Run with
``python3 -m unittest discover -s tests`` from ``ob/``.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ob import recovery


class FakeStats:
    def __init__(self, frames, events):
        self.frames = frames
        self.events = events


def _frames(t0, pts_list, gaps_between=None, epoch=("r1", 0)):
    """Build receiver video frames at t0 + pts, inserting wall-clock holes."""
    out = []
    wall = t0
    prev_pts = None
    for i, pts in enumerate(pts_list):
        if prev_pts is not None:
            wall += pts - prev_pts          # normal pacing
        if gaps_between and i in gaps_between:
            wall += gaps_between[i]
        out.append({"side": "receiver", "kind": "video", "t_wall": wall,
                    "pts_time": pts, "epoch": epoch})
        prev_pts = pts
    return out


class RecoveryDefinitions(unittest.TestCase):
    def setUp(self):
        self.t0 = 1_000_000.0

    def _meta(self, blackhole_ms=1200, at_ms=4000, started=None):
        started = started if started is not None else self.t0
        return {"started_wall": started, "relay_started_wall": started,
                "impairment": {"blackhole_ms": blackhole_ms,
                               "blackhole_at_ms": at_ms}}

    def test_repaired_outage_has_small_loss_interval(self):
        # picture runs through the planned window with only one 0.25 s hole
        frames = _frames(self.t0 + 0.5,
                         [i * 0.04 for i in range(300)],
                         gaps_between={100: 0.25})
        stats = FakeStats(frames, [])
        r = recovery.summarize(stats, self._meta())
        self.assertEqual(r["interruptions"], 0)
        self.assertLess(r["loss_interval_s"], 0.5)
        self.assertEqual(r["errors"], [])

    def test_unrepaired_outage_loss_interval_tracks_hole(self):
        frames = _frames(self.t0 + 0.5,
                         [i * 0.04 for i in range(300)],
                         gaps_between={100: 1.4})
        stats = FakeStats(frames, [])
        r = recovery.summarize(stats, self._meta())
        self.assertGreater(r["loss_interval_s"], 1.2)
        self.assertEqual(r["errors"], [])

    def test_rebuild_uses_interruption_event_and_flags_stale_epoch(self):
        frames = _frames(self.t0 + 0.5, [i * 0.04 for i in range(100)],
                         epoch=("r1", 0))
        # a stale epoch frame after the interruption is evidence of backlog
        stale = _frames(self.t0 + 9.0, [0.0, 0.04], epoch=("r1", 0))
        fresh = _frames(self.t0 + 12.0, [0.0, 0.04], epoch=("r1", 1))
        events = [(1.0, self.t0 + 5.0, "connection_interruption", {})]
        r = recovery.summarize(FakeStats(frames + fresh, events),
                               self._meta(blackhole_ms=0))
        self.assertEqual(r["interruptions"], 1)
        self.assertIsNotNone(r["recovery_s"])
        # only the frames passed in are considered; newest epoch is (r1,1)
        r2 = recovery.summarize(FakeStats(frames + stale + fresh, events),
                                self._meta(blackhole_ms=0))
        self.assertTrue(any("older media generation" in e
                            for e in r2["errors"]))

    def test_no_frame_after_window_is_an_error_not_a_success(self):
        frames = _frames(self.t0 + 0.5, [i * 0.04 for i in range(20)])
        # window is later than every frame
        r = recovery.summarize(FakeStats(frames, []),
                               self._meta(at_ms=20000))
        self.assertTrue(r["errors"])

    def test_planned_window_uses_milliseconds(self):
        start, end = recovery.planned_outage(self._meta())
        self.assertAlmostEqual(start, self.t0 + 4.0)
        self.assertAlmostEqual(end - start, 1.2)

    def test_relay_anchor_preferred(self):
        meta = self._meta(started=self.t0 - 5.0)
        meta["relay_started_wall"] = self.t0
        start, _ = recovery.planned_outage(meta)
        self.assertAlmostEqual(start, self.t0 + 4.0)


if __name__ == "__main__":
    unittest.main()
