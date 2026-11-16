"""Direct tests for the bounded sender queue (no ffmpeg involved).

The field outage runs never fill the 600 KB bound: the supervisor restarts the
encoder at session loss, so no backlog is produced. That means the
``queue_overflow`` drop path cannot be evidenced from ``runs/``. These tests
drive the queue itself with a stalled pipe so the bound, the TS-sync-aligned
cut and the cross-generation reattach are all exercised deterministically.

Run with ``python3 -m unittest discover -s tests`` from ``ob/``.
"""
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ob.pacer import TS_PACKET, TS_SYNC, BoundedPacer

PKT = bytes([TS_SYNC]) + bytes(TS_PACKET - 1)


class FakeStats:
    def __init__(self):
        self.drops = []
        self.events = []
        self.lock = threading.Lock()

    def record_drop(self, ev):
        with self.lock:
            self.drops.append(ev)

    def event(self, kind, **kw):
        with self.lock:
            self.events.append((kind, kw))


class FakeProc:
    """Just enough of subprocess.Popen for BoundedPacer to attach to."""

    def __init__(self, stdout=None, stdin=None):
        self.stdout = stdout
        self.stdin = stdin


def _ts_bytes(packets):
    return PKT * packets


class BoundedPacerTest(unittest.TestCase):
    def setUp(self):
        self.stats = FakeStats()
        self.enc_r, self.enc_w = os.pipe()
        self.gw_r, self.gw_w = os.pipe()
        self.src = FakeProc(stdout=os.fdopen(self.enc_r, "rb", buffering=0))
        self.dst = FakeProc(stdin=os.fdopen(self.gw_w, "wb", buffering=0))
        self.max_bytes = TS_PACKET * 20
        self.pacer = BoundedPacer(self.src, self.dst, self.max_bytes,
                                  self.stats)
        self.pacer.start()

    def tearDown(self):
        self.pacer.stop()
        self.pacer.join()
        for fd in (self.enc_w, self.gw_r):
            try:
                os.close(fd)
            except OSError:
                pass
        # the file objects close their own fds when garbage collected; close
        # explicitly so repeated tests do not leak descriptors
        for f in (self.src.stdout, self.dst.stdin):
            try:
                f.close()
            except OSError:
                pass

    def _wait_for(self, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_bound_is_enforced_with_stalled_gateway(self):
        # gateway stdin is never drained, so the writer cannot progress
        os.write(self.enc_w, _ts_bytes(1200))  # ~226 KB, far over the bound
        ok = self._wait_for(lambda: self.pacer.snapshot()["bytes_dropped"] > 0)
        self.assertTrue(ok, "bound never engaged")
        snap = self.pacer.snapshot()
        # the cut is aligned to a 0x47 sync byte, so at most one packet above
        self.assertLessEqual(snap["queue_bytes"], self.max_bytes + TS_PACKET)
        self.assertGreater(snap["bytes_dropped"], 0)
        self.assertGreater(snap["bytes_in"], self.max_bytes)
        overflow = [d for d in self.stats.drops if d.reason == "queue_overflow"]
        self.assertTrue(overflow, "drops not attributed to queue_overflow")
        # every drop starts on a TS boundary and has whole packets
        self.assertTrue(all(d.bytes % TS_PACKET == 0 for d in overflow))

    def test_stalled_gateway_records_pipe_broken_then_keeps_bounded(self):
        # close the gateway read end: writer sees EPIPE and latches broken
        os.close(self.gw_r)
        self.gw_r = -1
        os.write(self.enc_w, _ts_bytes(50))
        ok = self._wait_for(lambda: self.pacer.snapshot()["pipe_broken"])
        self.assertTrue(ok, "pipe_broken never latched")
        # ingest continues while broken and the bound still holds
        os.write(self.enc_w, _ts_bytes(1200))
        ok = self._wait_for(
            lambda: self.pacer.snapshot()["bytes_dropped"] > 0)
        self.assertTrue(ok, "bound stopped enforcing while pipe broken")
        snap = self.pacer.snapshot()
        self.assertTrue(snap["alive"])
        self.assertLessEqual(snap["queue_bytes"], self.max_bytes + TS_PACKET)

    def test_reader_survives_encoder_eof_and_reattach_src(self):
        # first generation produces a little data, then EOF (encoder killed)
        os.write(self.enc_w, _ts_bytes(5))
        self.assertTrue(self._wait_for(
            lambda: self.pacer.snapshot()["bytes_in"] >= 5 * TS_PACKET))
        os.close(self.enc_w)
        self.enc_w = -1
        time.sleep(0.2)  # let the reader hit EOF and wait for reattach
        first = self.pacer.snapshot()["bytes_in"]
        self.assertTrue(self.pacer.threads[0].is_alive(),
                        "reader exited at encoder EOF")
        # a new encoder generation is attached on a fresh pipe
        new_r, new_w = os.pipe()
        self.src.stdout.close()
        self.src.stdout = os.fdopen(new_r, "rb", buffering=0)
        self.pacer.reattach_src(self.src)
        os.write(new_w, _ts_bytes(5))
        self.assertTrue(self._wait_for(
            lambda: self.pacer.snapshot()["bytes_in"] >= first + 5 * TS_PACKET),
            "reader did not consume after reattach_src")
        os.close(new_w)

    def test_reset_timeline_flushes_as_timeline_reset(self):
        os.write(self.enc_w, _ts_bytes(1200))
        self.assertTrue(self._wait_for(
            lambda: self.pacer.snapshot()["bytes_dropped"] > 0))
        self.pacer.reset_timeline()
        snap = self.pacer.snapshot()
        self.assertEqual(snap["queue_bytes"], 0)
        self.assertTrue(any(d.reason == "timeline_reset"
                            for d in self.stats.drops))


if __name__ == "__main__":
    unittest.main()
