"""Legacy check: an outage must reconnect and resume at the live edge.

Kept runnable for the field environment. Recovery and loss-interval numbers are
derived from the run artifacts (ob.evidence), not from private Stats internals,
so this script's output can be reproduced from runs/<id>/ after the fact.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ob.config import PROFILES, Impairment
from ob.contract import PACER_BYTES_DEFAULT, LATENCY_MS_DEFAULT
from ob.pipeline import Pipeline
from ob.evidence import summarize

pipe = Pipeline(Impairment(**PROFILES["outage"].to_dict()),
                LATENCY_MS_DEFAULT, PACER_BYTES_DEFAULT, duration_s=40)
pipe.start()
try:
    time.sleep(34)
    alive = {k: v.poll() is None for k, v in pipe.procs.items()}
    state = pipe.parsers["receiver"].snapshot()["connection_state"]
    kinds = [e[2] for e in pipe.stats.events]
finally:
    pipe.stop()

ev = summarize(pipe.run_dir)
rec = ev["recoveries"]
print("events:", {k: kinds.count(k) for k in set(kinds)})
print("alive:", alive, "state:", state)
print("recoveries:", rec)
assert kinds.count("connection_interruption") == 1, kinds
assert "srt_session_restarted" in kinds
assert all(alive.values()), alive
assert state == "connected"
assert len(rec) == 1 and rec[0]["session_restore_s"] is not None, rec
# After a post-outage rebuild the new media generation must be active: the
# recovery must land in a newer generation than the one that was lost.
assert rec[0]["media_generation_after"] is not None
assert rec[0]["media_generation_after"] > rec[0]["media_generation_before"], rec
assert rec[0]["picture_recovery_s"] is not None
print("OUTAGE_RECONNECT_OK")
