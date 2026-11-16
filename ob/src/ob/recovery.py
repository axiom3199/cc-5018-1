"""Recovery and loss-interval arithmetic over the frames the receiver decoded.

Everything here is derived from execution evidence (receiver frame wall clocks,
kernel events and the planned impairment window), never from the requested
configuration.  The web page and the maintenance checks use the same functions,
so what an operator sees is what the checks assert.

Definitions used throughout the docs and the checks:

``loss_interval_s``
    Measured wall-clock interval in which the studio received no video frame,
    i.e. the media hole that the audience actually sees.

``recovery_s``
    From the reference point (end of the planned outage window, or the
    ``connection_interruption`` event when the session was torn down) to the
    first decoded frame afterwards.  This is the time the picture takes to
    come back, not the time the network needed to heal.
"""
from . import config


def _receiver_video(stats):
    rows = [f for f in stats.frames
            if f["side"] == "receiver" and f["kind"] == "video"]
    rows.sort(key=lambda f: f["t_wall"])
    return rows


def _event_wall(stats, kind):
    return [tw for (_, tw, k, _) in stats.events if k == kind]


def planned_outage(meta):
    """Planned blackhole window as absolute wall-clock (start, end), or None."""
    imp = (meta or {}).get("impairment") or {}
    length = float(imp.get("blackhole_ms") or 0) / 1000.0
    if length <= 0:
        return None
    anchor = float(meta.get("relay_started_wall") or meta["started_wall"])
    start = anchor + float(imp.get("blackhole_at_ms") or 0) / 1000.0
    return start, start + length


def summarize(stats, meta, fps=config.FPS):
    """Return measured recovery facts and any contradictions to the plan.

    ``errors`` is non-empty when the evidence does not support a recovery
    claim (no frame after an interruption, frames continuing from a stale
    generation).  Callers surface these instead of reporting success.
    """
    frames = _receiver_video(stats)
    errors = []
    interruptions = _event_wall(stats, "connection_interruption")
    resets = _event_wall(stats, "timeline_reset")
    window = planned_outage(meta)

    gaps = []
    if len(frames) >= 2:
        for i in range(len(frames) - 1):
            gaps.append({
                "start_wall": frames[i]["t_wall"],
                "end_wall": frames[i + 1]["t_wall"],
                "duration_s": round(frames[i + 1]["t_wall"]
                                    - frames[i]["t_wall"], 3),
                "before_pts": frames[i]["pts_time"],
                "after_pts": frames[i + 1]["pts_time"],
                "frames_missing_estimate": max(
                    0, round((frames[i + 1]["t_wall"]
                              - frames[i]["t_wall"]) * fps) - 1),
            })

    def gap_overlapping(start, end):
        hits = [g for g in gaps
                if g["end_wall"] > start and g["start_wall"] < end]
        return max(hits, key=lambda g: g["duration_s"]) if hits else None

    report = {
        "frames": len(frames),
        "first_frame_wall": frames[0]["t_wall"] if frames else None,
        "last_frame_wall": frames[-1]["t_wall"] if frames else None,
        "largest_gap": max(gaps, key=lambda g: g["duration_s"]) if gaps else None,
        "interruptions": len(interruptions),
        "timeline_resets": len(resets),
        "planned_outage": None,
        "recovery_reference": None,
        "recovery_s": None,
        "loss_interval_s": None,
        "errors": errors,
    }

    reference = None
    if window:
        start, end = window
        report["planned_outage"] = {
            "start_wall": start, "end_wall": end,
            "duration_s": round(end - start, 3)}
        reference = end
    if interruptions:
        # an interruption after the planned window is the relevant reference
        event = max(interruptions)
        if reference is None or event > reference:
            reference = event
    report["recovery_reference"] = reference

    if gaps and reference is not None:
        start, end = window if window else (reference - 1.0, reference)
        # The receiver delivers on the TSBPD clock, so a planned hole shows up
        # shifted by about one budget. Search from the budget before the hole
        # start to the end of the hole; only fall back to the global maximum
        # when that window contains no gap at all.
        hole = gap_overlapping(start, end)
        if hole is None:
            hole = gap_overlapping(start - config.SRT_LATENCY_MS_MAX / 1000.0,
                                   end + 5.0)
        if hole is None:
            hole = max(gaps, key=lambda g: g["duration_s"])
        report["loss_interval_s"] = hole["duration_s"]
        after = [f for f in frames if f["t_wall"] >= hole["end_wall"]]
        if not after:
            errors.append(
                "no receiver frame after the interruption/outage window: "
                "the studio picture never recovered")
        else:
            report["recovery_s"] = round(
                max(0.0, after[0]["t_wall"] - reference), 3)

    if window and not interruptions:
        after = [f for f in frames if f["t_wall"] >= window[1]]
        if not after and not errors:
            errors.append("no receiver frame after the planned outage window")

    # Frames from a pre-interruption generation after the rebuild would prove
    # the "resume at the live edge" claim false.
    if interruptions and frames:
        ref = max(interruptions)
        newest_epoch = frames[-1].get("epoch")
        stale = [f for f in frames
                 if f["t_wall"] > ref and f.get("epoch") != newest_epoch]
        if stale:
            errors.append(
                f"{len(stale)} frame(s) from an older media generation after "
                "the interruption")
    return report
