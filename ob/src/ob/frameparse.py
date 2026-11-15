"""Parse ffmpeg showinfo/ashowinfo lines into frame records.

Video line example:
  n:  123 pts: 492000 pts_time:0.492 ...
Audio line example (ashowinfo):
  n:  10 pts: 48000 pts_time:1.000 ...
Each side stamps its own wall clock; the receiver joins its record to the
sender's by *media epoch + PTS* to compute a reproducible end-to-end delay that
does not rely on ping or on reading the burned-in picture text. The per-graph
index n is deliberately unused: it restarts in every ffmpeg filter graph and
interleaves on the receiver's split output.
"""
import re
import time

_N_RE = re.compile(r"\bn:\s*(\d+)")
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


class FrameTracker:
    """Tracks frames and detects real media-timeline discontinuities.

    ffmpeg's per-filter-graph index ``n`` is unusable for gap detection here:
    on the receiver the same showinfo feeds both the null decode sink and the
    1 fps snapshot branch, so ``n`` interleaves and restarts. Media PTS is the
    single monotonic time base, so discontinuities are measured against the
    expected PTS step instead.
    """

    def __init__(self, side, stats, audio=False, step=None):
        self.side = side
        self.stats = stats
        self.audio = audio
        self.step = step or (1024.0 / 48000 if audio else 1.0 / 25)
        self.last_pts = None
        self.gaps = 0
        self.count = 0
        self.missing_units = 0
        # epoch tag: after a media-timeline reset (or an encoder restart that
        # follows an SRT rebuild) every side restarts PTS at 0, so frames carry
        # the (run_id, generation) of the epoch that produced them
        self.epoch = None

    def reset_epoch(self, run_id, generation=0):
        self.epoch = (run_id, generation)
        self.last_pts = None

    def feed(self, line):
        # "ashowinfo" contains the substring "showinfo", so match the exact
        # ffmpeg filter token instead of a bare substring test.
        if self.audio:
            if "Parsed_ashowinfo" not in line:
                return
        elif "Parsed_showinfo" not in line or "Parsed_ashowinfo" in line:
            return
        m_n = _N_RE.search(line)
        m_pts = _PTS_RE.search(line)
        if not m_n or not m_pts:
            return
        pts = float(m_pts.group(1))
        if self.last_pts is not None:
            delta = pts - self.last_pts
            # tolerate one step of jitter; anything larger is a real gap
            if delta > self.step * 1.8:
                missing = max(0, round(delta / self.step) - 1)
                self.gaps += 1
                self.missing_units += missing
                self.stats.event(
                    "timeline_gap" if not self.audio else "audio_gap",
                    side=self.side, from_pts=self.last_pts, to_pts=pts,
                    missing=missing)
        self.last_pts = pts
        self.count += 1
        self.stats.record_frame({
            "side": self.side,
            "epoch": self.epoch,
            "kind": "audio" if self.audio else "video",
            "n": int(m_n.group(1)),
            "pts_time": round(pts, 6),
            "t_wall": time.time(),
        })


def join_latency(frames, fps):
    """Pair sender/receiver video frames by *media epoch + PTS* (not per-graph
    index: showinfo's n restarts at zero in every ffmpeg filter graph, and PTS
    itself restarts at zero after every timeline reset).

    Both ends run on the same host in this prototype, so wall clocks are
    identical. PTS values are quantised to one frame to tolerate float noise.
    Frames from different run_id epochs are never paired with each other.
    """
    slot = 1.0 / fps
    sender = {}
    rows = []
    for fr in frames:
        if fr["kind"] != "video" or fr["side"] != "sender":
            continue
        sender[(fr.get("epoch"), round(fr["pts_time"] / slot))] = fr["t_wall"]
    for fr in frames:
        if fr["kind"] == "video" and fr["side"] == "receiver":
            row = dict(fr)
            sw = sender.get((fr.get("epoch"), round(fr["pts_time"] / slot)))
            row["e2e_ms"] = round((fr["t_wall"] - sw) * 1000, 1) if sw else None
            rows.append(row)
    return rows
