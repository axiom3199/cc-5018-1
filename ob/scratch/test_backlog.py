"""Legacy comparison: bounded pacer vs unbounded backlog after an outage.

Kept runnable. Reads public snapshots and recomputable artifacts only (no direct
stats.frames/events poking), so both modes can be re-derived after the fact.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ob.config import PROFILES, Impairment
from ob.contract import PACER_BYTES_DEFAULT, LATENCY_MS_DEFAULT, pacer_media_seconds
from ob.pipeline import Pipeline
from ob.evidence import summarize


def run(unbounded):
    pipe = Pipeline(Impairment(**PROFILES["outage"].to_dict()),
                    LATENCY_MS_DEFAULT, PACER_BYTES_DEFAULT,
                    duration_s=40, unbounded=unbounded)
    pipe.start()
    try:
        time.sleep(34)
        snap = pipe.pacer.snapshot()
        summary = pipe.stats.summary()
        kinds = [e[2] for e in pipe.stats.events]
        peak = max((s.get("pacer_queue_bytes", 0) for s in pipe.stats.series),
                   default=0)
    finally:
        pipe.stop()
    # stop() finalizes events.csv/frames.csv; derive evidence from artifacts
    ev = summarize(pipe.run_dir)
    recoveries = ev["recoveries"]
    loss = max((g["duration_s"] for g in ev["loss_intervals"]), default=0.0)
    return {
        "mode": "unbounded" if unbounded else "bounded",
        "queue_peak_bytes": peak,
        "bounded": snap["bounded"],
        "queue_peak_media_s": None if unbounded
        else round(pacer_media_seconds(peak) or 0.0, 2),
        "recv_frames": summary["frames_decoded"],
        "drops": summary["drops_by_reason"],
        "reconnects": kinds.count("srt_session_restarted"),
        "max_loss_interval_s": loss,
        "picture_recovery_s": [
            r["picture_recovery_s"] for r in recoveries],
    }


for unb in (False, True):
    print(run(unb))
