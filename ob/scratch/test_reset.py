import sys, time, csv
sys.path.insert(0, "/workspace/ob/src")
from ob.config import PROFILES
from ob.pipeline import Pipeline

pipe = Pipeline(PROFILES["healthy"], 200, 600_000, duration_s=40)
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
    # pacer must still be moving data after the reset
    snap = pipe.pacer.snapshot()
    print("pacer after reset:", snap, flush=True)
    assert snap["alive"] and snap["bytes_out"] > 0
finally:
    pipe.stop()

# verify frame epochs
frames = pipe.stats.frames
vid = [f for f in frames if f["kind"] == "video"]
epochs = sorted({f["epoch"] for f in vid})
print("video epochs:", epochs)
assert epochs == sorted({(old_id, 0), (new_id, 1)}), epochs
# no old-epoch receiver frames after the reset wall time
stale = [f for f in vid if f["side"] == "receiver" and f["epoch"][0] == old_id
         and f["t_wall"] > t_reset + 1.0]
print("stale old-epoch frames after reset (+1s grace):", len(stale))
assert not stale
# new epoch receiver PTS must start near zero (no backlog replay)
new_recv = [f for f in vid if f["side"] == "receiver" and f["epoch"][0] == new_id]
first_pts = min(f["pts_time"] for f in new_recv)
print("new epoch first receiver pts:", first_pts, "frames:", len(new_recv))
assert first_pts < 3.0, first_pts
# event semantics
kinds = [e[2] for e in pipe.stats.events]
print("events:", {k: kinds.count(k) for k in set(kinds)})
assert kinds.count("timeline_reset") == 1
assert kinds.count("timeline_epoch_ready") == 1
assert "connection_interruption" not in kinds, kinds
# per-epoch e2e pairing has no cross-epoch matches
from ob.frameparse import join_latency
rows = join_latency(frames, 25)
unpaired = [r for r in rows if r["e2e_ms"] is None]
print("joined rows:", len(rows), "unpaired:", len(unpaired))
paired = [r for r in rows if r["e2e_ms"] is not None]
import statistics
print("e2e p50 new epoch:", statistics.median(
    r["e2e_ms"] for r in paired if r["epoch"][0] == new_id))
print("run_dir:", pipe.run_dir)
print("RESET_E2E_OK")
