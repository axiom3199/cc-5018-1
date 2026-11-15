"""Global configuration and impairment profiles for the OB return-link prototype."""
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]      # /workspace (shared tools)
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /workspace/ob
FFMPEG = str(ROOT / "tools" / "ffmpeg")
FFPROBE = str(ROOT / "tools" / "ffprobe")
RUNS_DIR = PROJECT_ROOT / "runs"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# ---- A/V envelope (declared resource range applies at these settings) ----
WIDTH, HEIGHT, FPS = 1280, 720, 25
GOP = 25                 # 1 s GOP
VIDEO_BITRATE = "1500k"  # 1.5 Mbps
AUDIO_BITRATE = "96k"
SAMPLE_RATE = 48000

# ---- Network plumbing (all loopback in this single-host prototype) ----
# encoder -> pacer(stdin/out) -> srt_gateway(srt://127.0.0.1:GATEWAY_SRT)
# impairment UDP relay listens PROXY_IN, forwards to gateway's UDP port GATEWAY_UDP
GATEWAY_SRT = 19000      # SRT listener port (control connection)
GATEWAY_UDP = 19001      # gateway's UDP port, impairment relay output target
PROXY_IN = 19002         # impairment relay input, SRT connection target
WEB_PORT = 8080

# ---- Latency / queue bounds ----
SRT_LATENCY_US = 200_000          # SRT TSBPD latency, 200 ms default
# keepalives tuned so a 15 s blackhole does NOT expire the SRT connection;
# longer outages still break it and are handled as connection interruptions
SRT_KEEP_IDLE_MS = 60_000
SRT_KEEP_INTERVAL_MS = 1_000
SRT_KEEP_COUNT = 30
PACER_BYTES_DEFAULT = 600_000     # bounded pacer queue (~0.4 s at 1.5 Mbps + overhead)
SNAPSHOT_INTERVAL = 1.0           # receiver jpeg snapshot every second


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
}
