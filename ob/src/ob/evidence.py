"""Recovery time and loss interval measured from executed run artifacts.

These are the two facts the return-link docs claims must be checked against:
* a *loss interval* is a contiguous wall-clock window in which the studio
  decoded no video at all (frames.csv, receiver side); and
* *recovery time* is how long after the SRT session was declared lost the
  studio decoded the first frame of the rebuilt media generation, plus how
  long the peer rebuild itself took (events.csv).

Everything is derived from files a run already writes, so any statement in the
README can be recomputed from the recorded evidence rather than trusted.
"""
import ast
import csv
import json
from pathlib import Path


def _epoch(value):
    if isinstance(value, (tuple, list)):
        return tuple(value)
    if isinstance(value, str) and value.startswith("("):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value
    return value


def load_events(run_dir):
    path = Path(run_dir) / "events.csv"
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                detail = json.loads(r["detail"]) if r.get("detail") else {}
            except json.JSONDecodeError:
                detail = {"raw": r.get("detail")}
            out.append({
                "elapsed": float(r["elapsed"]),
                "t_wall": float(r["t_wall"]),
                "kind": r["kind"],
                "detail": detail,
            })
    return out


def load_frames(run_dir):
    path = Path(run_dir) / "frames.csv"
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append({
                "side": r["side"],
                "epoch": _epoch(r.get("epoch")),
                "kind": r["kind"],
                "n": int(r["n"]),
                "pts_time": float(r["pts_time"]),
                "t_wall": float(r["t_wall"]),
                "elapsed": float(r["elapsed"]),
            })
    return out


def loss_intervals(frames, min_gap_s=0.4):
    """Windows with no decoded receiver video longer than ``min_gap_s``.

    Uses receiver video wall-clock stamps (the same clock across both ends in
    this single-host prototype). The default threshold is well above one frame
    step (40 ms) but below any real outage, so ordinary decoder/pacing jitter
    is not reported as a lost picture. A loss interval is the honest
    description of "the studio had no picture"; a large latency budget can
    hide the same network loss from the video decoder, which is why this is
    measured from decoded frames, not inferred from the impairment profile.

    ``duration_s`` is wall-clock and therefore counts both lost media and
    delivered-late media (a burst that arrives after a stall is still a frozen
    picture). ``missing_frames`` is separate and comes from the media timeline:
    it only counts frames the sender never produced within the same epoch.
    Across an epoch boundary PTS restarts, so the wall estimate is reported
    as ``missing_frames_estimate`` and the exact count is left unknown rather
    than invented.
    """
    vid = sorted((f for f in frames
                  if f["side"] == "receiver" and f["kind"] == "video"),
                 key=lambda f: f["t_wall"])
    if len(vid) < 2:
        return []
    step = 1.0 / 25
    gaps = []
    for prev, cur in zip(vid, vid[1:]):
        dt = cur["t_wall"] - prev["t_wall"]
        if dt > min_gap_s:
            same_epoch = prev["epoch"] == cur["epoch"]
            media_skip = (cur["pts_time"] - prev["pts_time"]) if same_epoch else None
            gap = {
                "start_wall": prev["t_wall"],
                "end_wall": cur["t_wall"],
                "duration_s": round(dt, 3),
                "missing_frames": max(0, round(media_skip / step) - 1)
                if media_skip is not None else None,
                "media_skipped_s": round(max(0.0, media_skip - step), 3)
                if media_skip is not None else None,
                "from_elapsed": round(prev["elapsed"], 3),
                "to_elapsed": round(cur["elapsed"], 3),
                "from_epoch": prev["epoch"],
                "to_epoch": cur["epoch"],
            }
            if media_skip is None:
                gap["missing_frames_estimate"] = max(0, round(dt / step) - 1)
            gaps.append(gap)
    return gaps


def recoveries(events, frames):
    """One entry per SRT session loss: rebuild time, picture recovery, gap.

    ``session_restore_s`` is peer-rebuild time (interruption -> session
    restarted); ``picture_recovery_s`` is interruption -> first decoded frame
    of the new session. ``loss_interval_s`` is the last pre-loss picture ->
    first recovered picture, i.e. the interval the viewer actually missed.
    """
    vid = sorted((f for f in frames
                  if f["side"] == "receiver" and f["kind"] == "video"),
                 key=lambda f: f["t_wall"])
    interruptions = [e for e in events if e["kind"] == "connection_interruption"]
    restarts = [e for e in events if e["kind"] == "srt_session_restarted"]
    out = []
    for i, intr in enumerate(interruptions):
        t_intr = intr["t_wall"]
        restart = next((r for r in restarts if r["t_wall"] >= t_intr), None)
        before = [f for f in vid if f["t_wall"] <= t_intr]
        after_start = restart["t_wall"] if restart else t_intr
        after = [f for f in vid if f["t_wall"] >= after_start]
        first = after[0] if after else None
        last_before = before[-1] if before else None
        out.append({
            "index": i,
            "interruption_elapsed": round(intr["elapsed"], 3),
            "session_restored_elapsed": round(restart["elapsed"], 3)
            if restart else None,
            "session_restore_s": round(restart["elapsed"] - intr["elapsed"], 3)
            if restart else None,
            "first_frame_elapsed": round(first["elapsed"], 3) if first else None,
            "picture_recovery_s": round(first["t_wall"] - t_intr, 3)
            if first else None,
            "loss_interval_s": round(first["t_wall"] - last_before["t_wall"], 3)
            if first and last_before else None,
            "from_epoch": last_before["epoch"] if last_before else None,
            "to_epoch": first["epoch"] if first else None,
            "media_generation_before": last_before["epoch"][1]
            if last_before and isinstance(last_before["epoch"], tuple) else None,
            "media_generation_after": first["epoch"][1]
            if first and isinstance(first["epoch"], tuple) else None,
        })
    return out


def summarize(run_dir):
    events = load_events(run_dir)
    frames = load_frames(run_dir)
    return {
        "run_dir": str(run_dir),
        "loss_intervals": loss_intervals(frames),
        "recoveries": recoveries(events, frames),
    }


def evidence_brief(run_dir):
    """Compact, execution-derived evidence for matrix/JSON output.

    Keeps only the longest loss interval and per-interruption recovery
    scalars; the full per-gap lists live in the run artifacts, not in every
    experiment row.
    """
    events = load_events(run_dir)
    frames = load_frames(run_dir)
    gaps = loss_intervals(frames)
    worst = max(gaps, key=lambda g: g["duration_s"], default=None)
    recs = recoveries(events, frames)
    return {
        "max_loss_interval_s": worst["duration_s"] if worst else 0.0,
        "max_loss_missing_frames": worst["missing_frames"] if worst else 0,
        "max_loss_missing_frames_estimate": worst.get(
            "missing_frames_estimate", 0) if worst else 0,
        "recovery_count": len(recs),
        "max_session_restore_s": max(
            (r["session_restore_s"] or 0.0 for r in recs), default=0.0),
        "max_picture_recovery_s": max(
            (r["picture_recovery_s"] or 0.0 for r in recs), default=0.0),
        "recoveries": [
            {k: r[k] for k in ("session_restore_s", "picture_recovery_s",
                               "loss_interval_s", "media_generation_before",
                               "media_generation_after")}
            for r in recs],
    }


def summarize_live(events, frames):
    """Same derivation for the in-memory Stats rows (no artifact round-trip).

    Stats events are ``(t_mono, t_wall, kind, detail)`` with an absolute
    monotonic clock; normalize their elapsed to the run start (the first event)
    so the live view matches the CSV convention.
    """
    base = events[0][0] if events else 0.0
    ev = [{"elapsed": (tm - base) if events else 0.0, "t_wall": tw,
           "kind": kind, "detail": detail}
          for tm, tw, kind, detail in events]
    fr = [{"side": f["side"], "epoch": f.get("epoch"), "kind": f["kind"],
           "n": f["n"], "pts_time": f["pts_time"], "t_wall": f["t_wall"],
           "elapsed": f.get("elapsed", 0.0)} for f in frames]
    return {
        "loss_intervals": loss_intervals(fr),
        "recoveries": recoveries(ev, fr),
    }
