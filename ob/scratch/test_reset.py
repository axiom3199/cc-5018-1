"""Legacy check: a timeline reset must not replay the previous media epoch.

Kept runnable. Uses the contract default (ms) and ob.evidence for the
recovery/loss-interval view, so the assertions do not reach into private
Stats structures.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ob.config import PROFILES, Impairment
from ob.contract import PACER_BYTES_DEFAULT, LATENCY_MS_DEFAULT
from ob.pipeline import Pipeline
from ob.evidence import summarize, load_frames

pipe = Pipeline(Impairment(**PROFILES["healthy"].to_dict()),
                LATENCY_MS_DEFAULT, PACER_BYTES_DEFAULT, duration_s=40)
pipe.start()
try:
    time.sleep(8)
    old_id = pipe.run_id
    t_reset = time.time()
    print("resetting timeline at t=8 ...", flush=True)
    pipe.reset_timeline()
    new_id = pipe.run_id
    print(f"old={old_id} new={new_id}", flush=True)
    assert new_id != old_id
    time.sleep(12)
    snap = pipe.pacer.snapshot()
    print("pacer after reset:", snap, flush=True)
    assert snap["alive"] and snap["bytes_out"] > 0
finally:
    pipe.stop()

frames = load_frames(pipe.run_dir)
vid = [f for f in frames if f["kind"] == "video"]
epochs = sorted({f["epoch"] for f in vid}, key=str)
print("video epochs:", epochs)
assert epochs == sorted({(old_id, 0), (new_id, 1)}, key=str), epochs
stale = [f for f in vid if f["side"] == "receiver" and f["epoch"][0] == old_id
         and f["t_wall"] > t_reset + 1.0]
print("stale old-epoch frames after reset (+1s grace):", len(stale))
assert not stale
new_recv = [f for f in vid if f["side"] == "receiver" and f["epoch"][0] == new_id]
first_pts = min(f["pts_time"] for f in new_recv)
print("new epoch first receiver pts:", first_pts, "frames:", len(new_recv))
assert first_pts < 3.0, first_pts

kinds = [e[2] for e in pipe.stats.events]
print("events:", {k: kinds.count(k) for k in set(kinds)})
assert kinds.count("timeline_reset") == 1
assert kinds.count("timeline_epoch_ready") == 1
assert "connection_interruption" not in kinds, kinds

# a timeline reset is not a connection interruption, so no "recovery" is
# recorded for it; the loss interval still shows the deliberate epoch switch
ev = summarize(pipe.run_dir)
print("loss intervals across reset:",
      [g for g in ev["loss_intervals"] if g["from_epoch"] != g["to_epoch"]])
print("run_dir:", pipe.run_dir)
print("RESET_E2E_OK")
