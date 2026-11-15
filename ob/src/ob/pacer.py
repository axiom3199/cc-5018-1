"""Bounded sender queue between the encoder and the SRT gateway.

The encoder produces an MPEG-TS byte stream (188-byte packets). The pacer keeps
only ``max_bytes`` of it: when the SRT path stalls (blackhole / heavy loss),
oldest TS packets are dropped and every drop is recorded with a wall-clock
timestamp. After recovery the queue drains near the live edge instead of
playing out a long backlog. ``max_bytes=0`` selects an unbounded queue, kept
only for the comparison experiment that demonstrates why unbounded buffering
cannot be called low latency.

The gateway pipe is made non-blocking: a stalled SRT sender can never freeze
the reader thread, so the encoder keeps producing real-time frames and the
bounded policy (rather than upstream back-pressure) decides what is discarded.
"""
import os
import threading
import time

TS_SYNC = 0x47
TS_PACKET = 188


class DropEvent:
    __slots__ = ("t_mono", "t_wall", "bytes", "packets", "reason", "queue_bytes")

    def __init__(self, t_mono, t_wall, nbytes, packets, reason, queue_bytes):
        self.t_mono = t_mono
        self.t_wall = t_wall
        self.bytes = nbytes
        self.packets = packets
        self.reason = reason
        self.queue_bytes = queue_bytes


class BoundedPacer:
    def __init__(self, src_proc, dst_proc, max_bytes, stats, reason="queue_overflow"):
        self.src = src_proc.stdout
        self.dst = dst_proc.stdin
        # non-blocking gateway pipe so SRT back-pressure never stalls ingest
        self._dst_fd = self.dst.fileno()
        os.set_blocking(self._dst_fd, False)
        self.max_bytes = max_bytes
        self.stats = stats
        self.reason = reason
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.alive = True
        self.pipe_broken = False
        self.reset_pending = False
        self._total_in = 0
        self._total_out = 0
        self._total_drop = 0
        self._write_would_block = 0
        self.threads = [
            threading.Thread(target=self._reader, name="pacer-reader", daemon=True),
            threading.Thread(target=self._writer, name="pacer-writer", daemon=True),
        ]

    def start(self):
        for t in self.threads:
            t.start()

    def reset_timeline(self):
        with self.lock:
            self._do_reset_locked()

    def reattach_dst(self, dst_proc, flush_reason=None):
        """Re-point the writer at a restarted gateway's stdin.

        When ``flush_reason`` is given (SRT rebuild / timeline reset), every
        byte still buffered belongs to the dead session's media past: flushing
        it as a recorded drop forces the new session to resume at the live edge
        instead of replaying the backlog accumulated during the outage.
        """
        with self.lock:
            if flush_reason and self.buf:
                self._record_drop(len(self.buf), flush_reason)
                self.buf.clear()
            self.dst = dst_proc.stdin
            self._dst_fd = self.dst.fileno()
            os.set_blocking(self._dst_fd, False)
            self.pipe_broken = False

    def reattach_src(self, src_proc):
        """Re-point the reader at a restarted encoder's stdout.

        A timeline reset kills the old encoder, whose EOF makes the reader
        exit; revive both threads so the bounded queue keeps enforcing its
        policy on the new epoch.
        """
        with self.lock:
            self.src = src_proc.stdout
            self.alive = True
        if not self.threads[0].is_alive() or not self.threads[1].is_alive():
            self.threads = [
                threading.Thread(target=self._reader, name="pacer-reader",
                                 daemon=True),
                threading.Thread(target=self._writer, name="pacer-writer",
                                 daemon=True),
            ]
            for t in self.threads:
                t.start()

    def stop(self):
        self.alive = False

    def join(self):
        for t in self.threads:
            t.join(timeout=3)

    def _record_drop(self, nbytes, reason):
        packets = nbytes // TS_PACKET
        self._total_drop += nbytes
        ev = DropEvent(time.monotonic(), time.time(), nbytes, packets, reason,
                       len(self.buf))
        self.stats.record_drop(ev)

    def _reader(self):
        while self.alive:
            try:
                chunk = self.src.read(65536)
            except (BrokenPipeError, OSError, ValueError):
                self.alive = False
                return
            if not chunk:
                self.alive = False
                return
            self._total_in += len(chunk)
            with self.lock:
                if self.reset_pending:
                    self._do_reset_locked()
                self.buf.extend(chunk)
                if self.max_bytes and len(self.buf) > self.max_bytes:
                    overflow = len(self.buf) - self.max_bytes
                    # align the cut point to a TS sync byte so framing survives
                    sync = self.buf.rfind(TS_SYNC, 1, min(overflow, len(self.buf)
                                                          - TS_PACKET))
                    cut = sync if sync > 0 else overflow
                    dropped = bytes(self.buf[:cut])
                    del self.buf[:cut]
                    self._record_drop(len(dropped), self.reason)

    def _writer(self):
        while self.alive:
            with self.lock:
                if self.reset_pending:
                    self._do_reset_locked()
                if not self.buf or self.pipe_broken:
                    chunk = None
                else:
                    chunk = bytes(self.buf[:65536])
            if not chunk:
                time.sleep(0.01)
                continue
            try:
                written = os.write(self._dst_fd, chunk)
            except BlockingIOError:
                self._write_would_block += 1
                time.sleep(0.01)
                continue
            except (BrokenPipeError, OSError, ValueError):
                # gateway died (connection interruption); keep ingesting and
                # enforce the bounded policy until the supervisor reattaches
                with self.lock:
                    self.pipe_broken = True
                self.stats.event("pacer_pipe_broken",
                                 note="gateway stdin closed; buffering bounded")
                time.sleep(0.1)
                continue
            with self.lock:
                del self.buf[:written]
                self._total_out += written

    def _do_reset_locked(self):
        if self.buf:
            self._record_drop(len(self.buf), "timeline_reset")
            self.buf.clear()
        self.reset_pending = False

    def snapshot(self):
        with self.lock:
            return {
                "queue_bytes": len(self.buf),
                "max_bytes": self.max_bytes,
                "bounded": bool(self.max_bytes),
                "bytes_in": self._total_in,
                "bytes_out": self._total_out,
                "bytes_dropped": self._total_drop,
                "write_stall_events": self._write_would_block,
                "pipe_broken": self.pipe_broken,
                "alive": self.alive,
            }
