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
from ob.pipeline import Pipeline


def main():
    ap = argparse.ArgumentParser(description="OB SRT return-link session")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="healthy")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--srt-latency", type=int, default=config.SRT_LATENCY_US // 1000)
    ap.add_argument("--pacer-bytes", type=int, default=config.PACER_BYTES_DEFAULT)
    ap.add_argument("--unbounded", action="store_true",
                    help="disable bounded queue (comparison experiment only)")
    ap.add_argument("--loss", type=float, default=None,
                    help="override random loss percent")
    ap.add_argument("--jitter", type=int, default=None,
                    help="override jitter ms")
    args = ap.parse_args()

    imp: Impairment = PROFILES[args.profile]
    if args.loss is not None:
        imp.loss_pct = args.loss
    if args.jitter is not None:
        imp.jitter_ms = args.jitter

    pipe = Pipeline(imp, args.srt_latency, args.pacer_bytes,
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
        print(f"artifacts: {pipe.run_dir}")


if __name__ == "__main__":
    main()
