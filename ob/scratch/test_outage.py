import sys, time
sys.path.insert(0, "/workspace/ob/src")
from ob.contract import build_impairment
from ob.pipeline import Pipeline

pipe = Pipeline(build_impairment("outage"), 200, 600_000, duration_s=40)
pipe.start()
try:
    time.sleep(34)
    alive = {k: v.poll() is None for k, v in pipe.procs.items()}
    state = pipe.parsers["receiver"].snapshot()["connection_state"]
    kinds = [e[2] for e in pipe.stats.events]
    recv = [dict(f) for f in pipe.stats.frames
            if f["side"] == "receiver" and f["kind"] == "video"]
    epochs = sorted({str(f["epoch"]) for f in recv})
finally:
    pipe.stop()

from collections import Counter
print(Counter(kinds))
st = pipe.stats.summary()
print("frames:", st["frames_decoded"], "drops:", st["drops_by_reason"])
print("alive:", alive, "state:", state, "media gens:", epochs)
assert kinds.count("connection_interruption") == 1
assert "srt_session_restarted" in kinds
assert all(alive.values()), alive
assert state == "connected"
assert len(epochs) == 2, epochs
# live edge: newest post-outage generation must already be several seconds in,
# not stuck replaying the pre-outage generation's range
gen_last = {}
for f in recv:
    gen_last[str(f["epoch"])] = max(gen_last.get(str(f["epoch"]), 0), f["pts_time"])
print("max pts per generation:", gen_last)
newest_gen = max(gen_last, key=lambda g: gen_last[g])
assert gen_last[newest_gen] > 5.0, gen_last
print("OUTAGE_RECONNECT_OK")
