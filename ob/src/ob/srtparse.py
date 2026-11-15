"""Parse the SRT diagnostics this libsrt build actually emits.

The ffmpeg integration here does NOT support ``statistics-log-level`` and
therefore prints no periodic ``pktRetrans:..`` block. It does emit libsrt
alarm lines at debug level, e.g.::

  12:00:01.123/SRT:TsbPd!W:SRT.br: RCV-DROPPED 9 packet(s), ... delayed for 10.9 ms

We accumulate those real counters (late TSBPD drops, reorder distance, NAK
events) and track connection lifecycle, timestamping every tick so it joins
the per-second time series. The impairment relay's own drop counters are the
authoritative network-loss record; these SRT counters show what survived the
network but missed the playback deadline.
"""
import re
import threading
import time

_RCV_DROP_RE = re.compile(r"RCV-DROPPED\s+(\d+)\s+packet")
_REORDER_RE = re.compile(r"REORDER_DIST\s+[=:]?\s*(-?\d+)")
_NAK_RE = re.compile(r"NAK")
_RTT_RE = re.compile(r"RTT[=:\s]+([0-9.]+)\s*ms")
_LATE_RE = re.compile(r"delayed for\s+([0-9.]+)\s*ms")

_CONN_NEEDLES = (
    ("accept streamid", "connected"),
    ("SRT Connected", "connected"),
    ("Connection established", "connected"),
    ("Connection lost", "connection_lost"),
    ("Connection timeout", "connection_timeout"),
    ("SRT timeout", "connection_timeout"),
    ("Input/output error", "io_error"),
)


class SrtLogParser:
    def __init__(self, role, stats):
        self.role = role
        self.stats = stats
        self.lock = threading.Lock()
        self.latest = {
            "rcv_dropped_packets": 0,
            "rcv_drop_events": 0,
            "nak_events": 0,
            "max_reorder_distance": 0,
            "max_late_ms": 0.0,
            "rtt_ms": None,
            "connection_state": "idle",
        }
        self._conn_events = []
        self._last_tick = 0.0

    def feed(self, line: str):
        if "SRT" not in line and "srt @" not in line:
            return
        changed = False
        for needle, kind in _CONN_NEEDLES:
            if needle in line:
                with self.lock:
                    self._conn_events.append((kind, line.strip()[:200]))
                    if kind in ("connected",):
                        self.latest["connection_state"] = "connected"
                    elif kind in ("connection_lost", "connection_timeout",
                                  "io_error"):
                        self.latest["connection_state"] = "broken"
                self.stats.event(f"srt_{kind}", role=self.role,
                                 line=line.strip()[:200])
                break

        m = _RCV_DROP_RE.search(line)
        if m:
            with self.lock:
                self.latest["rcv_dropped_packets"] += int(m.group(1))
                self.latest["rcv_drop_events"] += 1
            changed = True
        if _NAK_RE.search(line):
            with self.lock:
                self.latest["nak_events"] += 1
            changed = True
        m = _REORDER_RE.search(line)
        if m:
            with self.lock:
                self.latest["max_reorder_distance"] = max(
                    self.latest["max_reorder_distance"], abs(int(m.group(1))))
            changed = True
        m = _LATE_RE.search(line)
        if m:
            with self.lock:
                self.latest["max_late_ms"] = max(
                    self.latest["max_late_ms"], float(m.group(1)))
            changed = True
        m = _RTT_RE.search(line)
        if m:
            with self.lock:
                self.latest["rtt_ms"] = float(m.group(1))
            changed = True

        # emit a periodic tick (max once/sec) so the time series moves even
        # when there are no alarm lines
        now = time.monotonic()
        if changed or now - self._last_tick >= 1.0:
            with self.lock:
                tick = dict(self.latest)
            tick["t_wall"] = time.time()
            self.stats.record_srt(self.role, tick)
            self._last_tick = now

    def snapshot(self):
        with self.lock:
            return dict(self.latest)

    def connection_events(self):
        with self.lock:
            return list(self._conn_events)
