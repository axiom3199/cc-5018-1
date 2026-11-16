"""Single source of truth for the OB return-link configuration contract.

Units are declared next to every value because two of them were historically
mixed up:

* SRT ``latency`` on the ffmpeg URL is **microseconds** (libsrt TSBPD).  The
  operator-facing CLI / web form is in **milliseconds**; the conversion happens
  exactly once, in ``Pipeline._gateway_cmd`` / ``_receiver_cmd``.
* The bounded pacer queue is in **bytes** of MPEG-TS (188-byte packets).

Values here are defaults for the control page and CLI; every one of them can be
overridden at start time, and overrides are validated against the ranges below
by ``ob.contract``.  No caller may silently substitute a default for an invalid
or unknown value.
"""
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]      # /workspace (shared tools)
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /workspace/ob
FFMPEG = str(ROOT / "tools" / "ffmpeg")
FFPROBE = str(ROOT / "tools" / "ffprobe")
RUNS_DIR = PROJECT_ROOT / "runs"
EVIDENCE_DIR = PROJECT_ROOT / "evidence"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# ---- A/V envelope (declared resource range applies at these settings) ----
WIDTH, HEIGHT, FPS = 1280, 720, 25
GOP = 25                 # 1 s GOP
VIDEO_BITRATE = "1500k"  # 1.5 Mbps
AUDIO_BITRATE = "96k"
SAMPLE_RATE = 48000

# ---- Network plumbing (all loopback in this single-host prototype) ----
# encoder -> pacer(stdin/out) -> srt_gateway(caller to RELAY_IN_PORT)
# impairment relay listens RELAY_IN_PORT and forwards to the receiver's listener
SRT_RECEIVER_PORT = 19001   # receiver's SRT listener (studio side)
RELAY_IN_PORT = 19002       # impairment relay input (gateway's SRT target)
WEB_PORT = 8080

# ---- SRT timing / retransmission contract ----
# ffmpeg passes srt://...&latency=<int64> straight to libsrt as MICROSECONDS.
# 200 ms is the operator-facing default; accepted range is deliberately narrow
# enough to stay a live-return link (20 ms .. 8 s).
SRT_LATENCY_MS_DEFAULT = 200
SRT_LATENCY_MS_MIN = 20
SRT_LATENCY_MS_MAX = 8000
# The peer idle timeout is disabled (-1 ms) so a blackhole does not kill the
# session from the socket side; if a peer still exits, supervise_srt() rebuilds
# the session.  This libsrt build exposes no keepalive option through ffmpeg.
SRT_PEERIDLE_TIMEOUT_MS = -1
SRT_CONNECT_TIMEOUT_MS = 3000

# Bounded pacer queue: 600 000 B of MPEG-TS.  With the declared envelope
# (1.5 Mbps video + 96 kbps audio + TS overhead ~= 1.6 Mbps ~= 200 kB/s) that is
# about 3.0 s of media, not 0.4 s.
PACER_BYTES_DEFAULT = 600_000
PACER_BYTES_MIN = 188 * 10          # at least 10 TS packets
PACER_BYTES_MAX = 64 * 1024 * 1024
SNAPSHOT_INTERVAL = 1.0             # receiver jpeg snapshot every second

# Supported ranges for every impairment knob (inclusive).  ``ob.contract``
# rejects values outside them instead of clamping, so a typo cannot masquerade
# as a working weak-network scenario.
IMPAIRMENT_RANGES = {
    "loss_pct": (0.0, 100.0),
    "burst_k": (0, 100),
    "burst_loss_pct": (0.0, 95.0),
    "reorder_pct": (0.0, 100.0),
    "reorder_delay_ms": (0, 2000),
    "jitter_ms": (0, 2000),
    "base_delay_ms": (0, 5000),
    "blackhole_ms": (0, 3_600_000),
    "blackhole_at_ms": (0, 3_600_000),
}

INT_IMPAIRMENT_FIELDS = ("burst_k", "reorder_delay_ms", "jitter_ms",
                         "base_delay_ms", "blackhole_ms", "blackhole_at_ms")


@dataclass
class Impairment:
    """Network impairment model applied by the UDP relay (per direction)."""
    name: str = "healthy"
    loss_pct: float = 0.0          # independent random loss
    burst_k: int = 0               # Gilbert-Elliott burst: avg burst length
    burst_loss_pct: float = 0.0    # fraction of time spent in lossy state
    reorder_pct: float = 0.0       # fraction of packets held back then released
    reorder_delay_ms: int = 0      # hold time for reordered packets
    jitter_ms: int = 0             # extra uniform random one-way delay 0..jitter
    base_delay_ms: int = 0         # constant extra one-way delay
    blackhole_ms: int = 0          # scheduled outage window
    blackhole_at_ms: int = 0       # start offset of the outage

    def to_dict(self):
        return asdict(self)


PROFILES = {
    "healthy": Impairment("healthy"),
    "jitter": Impairment("jitter", jitter_ms=120, base_delay_ms=20),
    "reorder": Impairment("reorder", reorder_pct=8.0, reorder_delay_ms=90,
                          jitter_ms=30),
    "burst_loss": Impairment("burst_loss", burst_k=8, burst_loss_pct=12.0,
                             loss_pct=1.0),
    "severe": Impairment("severe", burst_k=12, burst_loss_pct=25.0,
                         loss_pct=2.0, jitter_ms=150, reorder_pct=5.0,
                         reorder_delay_ms=120),
    "outage": Impairment("outage", blackhole_ms=15_000, blackhole_at_ms=8_000,
                         loss_pct=0.5),
    # Short outage (1.2 s) used by the latency-maintenance check: it is longer
    # than a 250 ms budget (loss permanent) but shorter than a 2 s budget
    # (SRT can retransmit the withheld packets and the picture continues).
    "short_outage": Impairment("short_outage", blackhole_ms=1_200,
                               blackhole_at_ms=4_000),
    # Loss pattern too long for a small budget to repair (see check_latency).
    "burst_over_budget": Impairment("burst_over_budget", burst_k=32,
                                    burst_loss_pct=55.0, loss_pct=2.0),
}
