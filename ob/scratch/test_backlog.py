import sys, time, ast
sys.path.insert(0, "/workspace/ob/src")
from ob.config import PROFILES
from ob.pipeline import Pipeline

def ep(r):
    e = r["epoch"]
    return e if isinstance(e, tuple) else ast.literal_eval(e)

def run(unbounded):
    pipe = Pipeline(PROFILES["outage"], 200, 600_000, duration_s=40,
                    unbounded=unbounded)
    pipe.start()
    try:
        time.sleep(34)
        rows = [dict(f) for f in pipe.stats.frames
                if f["side"] == "receiver" and f["kind"] == "video"]
        drops = pipe.stats.summary()["drops_by_reason"]
        q = pipe.pacer.snapshot()
        kinds = [e[2] for e in pipe.stats.events]
    finally:
        pipe.stop()
    rows.sort(key=lambda r: r["t_wall"])
    t0 = rows[0]["t_wall"]
    last = rows[-1]
    wall_pos = last["t_wall"] - t0
    # generation-aware lag: within the newest generation compare pts vs its age
    gens = sorted({ep(r) for r in rows})
    newest = gens[-1]
    gen_rows = [r for r in rows if ep(r) == newest]
    gen_t0 = min(r["t_wall"] for r in gen_rows)
    newest_age = last["t_wall"] - gen_t0
    gen_lag = newest_age - float(last["pts_time"])
    return {
        "mode": "unbounded" if unbounded else "bounded",
        "generations": len(gens),
        "reconnects": kinds.count("srt_session_restarted"),
        "recv_frames": len(rows),
        "frames_after_wall20": sum(1 for r in rows if r["t_wall"]-t0 > 20),
        "newest_gen_pts": float(last["pts_time"]),
        "gen_lag_s": round(gen_lag, 2),
        "pacer_queue_peak": q["max_bytes"] or "unbounded",
        "pacer_dropped": q["bytes_dropped"],
        "drops": drops,
    }

for unb in (False, True):
    print(run(unb))
