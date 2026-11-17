#!/usr/bin/env python3
"""Headless runner for one OB return-link session.

Examples:
  python3 bin/run_session.py --profile healthy --duration 30
  python3 bin/run_session.py --profile outage --duration 40 --srt-latency 400
  python3 bin/run_session.py --unbounded --profile outage --duration 40
"""
import argparse
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ob import config
from ob.config import PROFILES, Impairment
from ob.contract import (LATENCY_MS_DEFAULT, PACER_BYTES_DEFAULT,
                         validate_impairment_fields, validate_latency_ms,
                         validate_pacer_bytes)
from ob.pipeline import Pipeline


def main():
    ap = argparse.ArgumentParser(description="OB SRT return-link session")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="healthy")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--srt-latency", type=int, default=LATENCY_MS_DEFAULT,
                    help="SRT TSBPD budget in ms (converted to us for the URL)")
    ap.add_argument("--pacer-bytes", type=int, default=PACER_BYTES_DEFAULT)
    ap.add_argument("--unbounded", action="store_true",
                    help="disable bounded queue (comparison experiment only)")
    ap.add_argument("--loss", type=float, default=None,
                    help="override random loss percent")
    ap.add_argument("--jitter", type=int, default=None,
                    help="override jitter ms")
    args = ap.parse_args()

    imp: Impairment = Impairment(**PROFILES[args.profile].to_dict())
    overrides = {}
    if args.loss is not None:
        overrides["loss_pct"] = args.loss
    if args.jitter is not None:
        overrides["jitter_ms"] = args.jitter
    try:
        latency = validate_latency_ms(args.srt_latency)
        pacer_bytes = validate_pacer_bytes(
            args.pacer_bytes, unbounded=args.unbounded)
        overrides = validate_impairment_fields(overrides)
    except ValueError as exc:
        ap.error(str(exc))
    for key, val in overrides.items():
        setattr(imp, key, val)

    pipe = Pipeline(imp, latency, pacer_bytes,
                    duration_s=args.duration, unbounded=args.unbounded)
    stop = {"v": False}

    def handler(signum, frame):
        stop["v"] = True
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    pipe.start()
    print(f"[run] run_id={pipe.run_id} dir={pipe.run_dir} profile={args.profile}")
    deadline = time.monotonic() + args.duration
    try:
        while not stop["v"] and time.monotonic() < deadline:
            st = pipe.status()
            srt_r = st["srt"]["receiver"]
            print(
                f"[t={st['elapsed_s']:5.1f}s] decoded={st['frames_decoded']:5d} "
                f"drops={st['drop_packets']:5d} "
                f"pacer_q={st['pacer']['queue_bytes']:7d} "
                f"rtt={srt_r.get('rtt_ms', '-')} "
                f"nak={srt_r.get('nak_events', '-')} "
                f"latedrop={srt_r.get('rcv_dropped_packets', '-')} "
                f"e2e_avg={st['e2e_ms_avg']} ms", flush=True)
            time.sleep(2)
    finally:
        pipe.stop()
        summary = pipe.stats.summary()
        print("\n=== session summary ===")
        for k, v in summary.items():
            print(f"{k}: {v}")
        from ob.evidence import evidence_brief
        ev = evidence_brief(pipe.run_dir)
        print(f"max_loss_interval_s: {ev['max_loss_interval_s']}")
        print(f"max_picture_recovery_s: {ev['max_picture_recovery_s']}")
        print(f"artifacts: {pipe.run_dir}")
        print("现场请用控制页（bin/ob.sh web）启停发送、调整丢包并核对画面/恢复延迟")


if __name__ == "__main__":
    main()
