"""Configurable network-impairment UDP relay (single bidirectional socket).

The SRT caller (gateway) sends to this relay; the relay learns the caller's
ephemeral address from the first datagram and forwards impaired traffic to the
SRT listener. Replies from the listener arrive back at the same socket (their
source address equals the listener), so direction is identified by sender
address and the reverse path gets the same impairment model. One heap per
direction handles base delay, jitter and reordering.
"""
import heapq
import random
import socket
import threading
import time


class ImpairmentRelay:
    def __init__(self, listen_port, target_port, cfg, stats,
                 remote_control_port=None, host="127.0.0.1"):
        self.cfg = cfg
        self.stats = stats
        self.target = (host, target_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, listen_port))
        self.sock.settimeout(0.3)
        self.lock = threading.Lock()
        self.peer = None                 # learned caller address
        self.heaps = {"fwd": [], "rev": []}
        self.seq = 0
        # blackhole timing is anchored once at construction and is NOT reset
        # by reset_timeline(): an outage window fires a single time, otherwise
        # flushing the relay would reschedule a second outage after recovery
        self.t0 = time.monotonic()
        self.bad = False                 # Gilbert-Elliott state
        self.alive = True
        self.counters = {
            "fwd": {"in": 0, "sent": 0, "drop_random": 0, "drop_burst": 0,
                    "drop_blackhole": 0, "reordered": 0},
            "rev": {"in": 0, "sent": 0, "drop_random": 0, "drop_burst": 0,
                    "drop_blackhole": 0, "reordered": 0},
        }

    def start(self):
        threading.Thread(target=self._run, name="impair-relay",
                         daemon=True).start()

    def update(self, cfg):
        with self.lock:
            self.cfg = cfg

    def in_blackhole(self):
        with self.lock:
            return self._in_blackhole(time.monotonic())

    def blackhole_ends_in(self):
        """Seconds until the current/next blackhole window is over (0 if none)."""
        with self.lock:
            c = self.cfg
            if c.blackhole_ms <= 0:
                return 0.0
            el = (time.monotonic() - self.t0) * 1000.0
            end = c.blackhole_at_ms + c.blackhole_ms
            if el < c.blackhole_at_ms:
                return (end - el) / 1000.0
            if el < end:
                return (end - el) / 1000.0
            return 0.0

    def reset_timeline(self):
        with self.lock:
            self.heaps = {"fwd": [], "rev": []}
            self.bad = False
            self.peer = None
            # note: self.t0 intentionally unchanged so the blackhole window is
            # not rescheduled

    def _in_blackhole(self, now):
        c = self.cfg
        if c.blackhole_ms <= 0:
            return False
        el = (now - self.t0) * 1000.0
        return c.blackhole_at_ms <= el < c.blackhole_at_ms + c.blackhole_ms

    def _loss_state(self):
        c = self.cfg
        if c.burst_k <= 0 or c.burst_loss_pct <= 0:
            return False
        p_bad = min(c.burst_loss_pct / 100.0, 0.95)
        p_leave = 1.0 / c.burst_k
        p_enter = p_bad * p_leave / max(1e-6, 1.0 - p_bad)
        r = random.random()
        if self.bad:
            if r < p_leave:
                self.bad = False
        elif r < p_enter:
            self.bad = True
        return self.bad

    def _run(self):
        while self.alive:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                self._flush()
                continue
            except OSError:
                return
            now = time.monotonic()
            with self.lock:
                direction = "rev" if addr == self.target else "fwd"
                if direction == "fwd" and self.peer is None:
                    self.peer = addr
                    self.stats.event("relay_learned_peer", addr=str(addr))
                c = self.cfg
                ct = self.counters[direction]
                ct["in"] += 1
                if self._in_blackhole(now):
                    ct["drop_blackhole"] += 1
                    continue
                if self._loss_state():
                    ct["drop_burst"] += 1
                    continue
                if c.loss_pct > 0 and random.random() * 100 < c.loss_pct:
                    ct["drop_random"] += 1
                    continue
                delay = c.base_delay_ms / 1000.0
                if c.jitter_ms:
                    delay += random.uniform(0, c.jitter_ms) / 1000.0
                if (c.reorder_pct > 0
                        and random.random() * 100 < c.reorder_pct):
                    delay += c.reorder_delay_ms / 1000.0
                    ct["reordered"] += 1
                self.seq += 1
                heapq.heappush(self.heaps[direction],
                               (now + delay, self.seq, data, addr))
            self._flush()

    def _flush(self):
        now = time.monotonic()
        due = []
        with self.lock:
            for direction, heap in self.heaps.items():
                while heap and heap[0][0] <= now:
                    due.append((direction, heapq.heappop(heap)))
        for direction, (_, _, payload, src_addr) in due:
            dst = self.peer if direction == "rev" else self.target
            if dst is None:
                continue
            try:
                self.sock.sendto(payload, dst)
            except OSError:
                return
            self.counters[direction]["sent"] += 1

    def stop(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return {
                d: dict(ct, queued=len(self.heaps[d]))
                for d, ct in self.counters.items()
            } | {"state_bad": self.bad, "peer": str(self.peer)}
