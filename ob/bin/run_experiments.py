#!/usr/bin/env python3
"""Repeatable comparison experiments across healthy and impaired networks.

Runs a fixed matrix of sessions, each a real two-end Pipeline, then writes a
machine-readable summary (experiments.json + experiments.csv) covering measured
end-to-end latency (frame-marker PTS pairing, never ping), SRT late drops,
bounded-queue drops and recovery behaviour. The latency sweep demonstrates how
the SRT latency budget bounds retransmission opportunity: a bigger buffer
trades recoverable loss for added delay and cannot honestly be called low
latency.
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ob import config
from ob.contract import build_impairment, recovery_contract
from ob.pipeline import Pipeline
from ob.frameparse import join_latency


def run_one(profile_name, duration, srt_latency, unbounded=False,
            pacer_bytes=config.PACER_BYTES_DEFAULT):
    imp = build_impairment(profile_name)
    pipe = Pipeline(imp, srt_latency, pacer_bytes, duration_s=duration,
                    unbounded=unbounded)
    facts = recovery_contract(srt_latency, pacer_bytes, imp, unbounded)
    pipe.start()
    try:
        time.sleep(duration)
    finally:
        pipe.stop()
    # frame-marker based e2e latency (recomputable from frames.csv)
    rows = join_latency(pipe.stats.frames, config.FPS)
    lat = sorted(r["e2e_ms"] for r in rows if r.get("e2e_ms") is not None)
    steady = [x for r, x in ((r, r.get("e2e_ms")) for r in rows)
              if x is not None and r["pts_time"] > 2.0]
    steady.sort()
    recv_frames = [f for f in pipe.stats.frames
                   if f["side"] == "receiver" and f["kind"] == "video"]
    audio_frames = [f for f in pipe.stats.frames
                    if f["side"] == "receiver" and f["kind"] == "audio"]
    drops = pipe.stats.drops
    res = {
        "profile": profile_name,
        "srt_latency_ms": srt_latency,
        "srt_latency_us": srt_latency * 1000,
        "pacer_media_s": facts["pacer_media_s"],
        "recoverable_media_loss_ms": facts["recoverable_media_loss_ms"],
        "bounded_queue": not unbounded,
        "run_id": pipe.run_id,
        "run_dir": str(pipe.run_dir),
        "video_frames_decoded": len(recv_frames),
        "audio_frames_decoded": len(audio_frames),
        "frame_gaps": pipe.trackers["receiver_video"].gaps,
        "e2e_ms_avg": round(sum(lat) / len(lat), 1) if lat else None,
        "e2e_ms_p50": steady[len(steady) // 2] if steady else None,
        "e2e_ms_p95": steady[int(len(steady) * 0.95)] if steady else None,
        "e2e_ms_max": max(lat) if lat else None,
        "queue_drop_packets": sum(
            d["packets"] for d in drops if d["reason"] == "queue_overflow"),
        "timeline_reset_drops": sum(
            d["packets"] for d in drops if d["reason"] == "timeline_reset"),
        "session_reconnect_drops": sum(
            d["packets"] for d in drops if d["reason"] == "session_reconnect"),
        "media_generations": len({
            f.get("epoch") for f in pipe.stats.frames
            if f["side"] == "receiver" and f["kind"] == "video"}),
        "srt_late_dropped": pipe.parsers["receiver"].snapshot().get(
            "rcv_dropped_packets", 0),
        "connection_events": sum(
            1 for e in pipe.stats.events if e[2] == "connection_interruption"),
        "restart_events": sum(
            1 for e in pipe.stats.events
            if e[2] in ("srt_session_restarted", "peer_restarted")),
    }
    # live-edge evidence for outage profiles: does the receiver still emit
    # frames in the latter part of the run, and how big did the queue grow?
    vid = [f for f in pipe.stats.frames
           if f["side"] == "receiver" and f["kind"] == "video"]
    if vid:
        t_first = min(f["t_wall"] for f in vid)
        res["frames_in_last_third"] = sum(
            1 for f in vid if f["t_wall"] - t_first > duration * 2 / 3)
    res["pacer_queue_peak_bytes"] = max(
        (s.get("pacer_queue_bytes", 0) for s in pipe.stats.series),
        default=0)
    # measured recovery / loss interval from the same evidence the page shows
    status = pipe.status()
    res["recovery"] = {
        k: status["recovery"][k] for k in (
            "loss_interval_s", "recovery_s", "interruptions",
            "largest_gap", "errors")}
    # resource envelope from /proc samples
    rsrc = pipe.stats.resources
    if rsrc:
        by_proc = {}
        for r in rsrc:
            p = by_proc.setdefault(r["process"], {"cpu": [], "rss": []})
            if r["cpu_pct"] is not None:
                p["cpu"].append(r["cpu_pct"])
            p["rss"].append(r["rss_mb"])
        res["cpu_pct_max_by_proc"] = {
            k: round(max(v["cpu"]), 1) if v["cpu"] else None
            for k, v in by_proc.items()}
        res["rss_mb_max_by_proc"] = {
            k: round(max(v["rss"]), 1) for k, v in by_proc.items()}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--out", default=str(config.RUNS_DIR / "experiments.json"))
    ap.add_argument("--quick", action="store_true",
                    help="smaller matrix for fast checks")
    args = ap.parse_args()

    matrix = [
        ("healthy", 200, False),
        ("jitter", 200, False),
        ("reorder", 200, False),
        ("burst_loss", 200, False),
    ]
    if not args.quick:
        matrix += [
            ("severe", 200, False),
            ("outage", 200, False),
            # latency-budget sweep on the same burst-loss profile
            ("burst_loss", 80, False),
            ("burst_loss", 400, False),
            ("burst_loss", 800, False),
            # unbounded queue under outage: the anti-pattern comparison
            ("outage", 200, True),
        ]

    results = []
    for profile, latency, unbounded in matrix:
        print(f"[experiment] {profile} latency={latency}ms "
              f"unbounded={unbounded}", flush=True)
        r = run_one(profile, args.duration, latency, unbounded)
        results.append(r)
        print("   ->", json.dumps(
            {k: r[k] for k in ("e2e_ms_p50", "e2e_ms_p95",
                               "queue_drop_packets", "srt_late_dropped",
                               "connection_events")}), flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    csv_out = out.with_suffix(".csv")
    keys = sorted({k for r in results for k in r})
    with open(csv_out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(results)
    print(f"\nwrote {out}\nwrote {csv_out}")


if __name__ == "__main__":
    main()
