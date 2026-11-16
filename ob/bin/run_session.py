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
from ob.contract import ContractError, build_impairment, recovery_contract
from ob.pipeline import Pipeline


def main():
    ap = argparse.ArgumentParser(description="OB SRT return-link session")
    ap.add_argument("--profile", choices=sorted(config.PROFILES),
                    default="healthy")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--srt-latency", type=int,
                    default=config.SRT_LATENCY_MS_DEFAULT,
                    metavar="MS", help="SRT TSBPD budget in milliseconds")
    ap.add_argument("--pacer-bytes", type=int,
                    default=config.PACER_BYTES_DEFAULT)
    ap.add_argument("--unbounded", action="store_true",
                    help="disable bounded queue (comparison experiment only)")
    ap.add_argument("--loss", type=float, default=None,
                    help="override random loss percent")
    ap.add_argument("--jitter", type=int, default=None,
                    help="override jitter ms")
    args = ap.parse_args()

    try:
        imp = build_impairment(args.profile, {
            "loss_pct": args.loss, "jitter_ms": args.jitter})
        pipe = Pipeline(imp, args.srt_latency, args.pacer_bytes,
                        duration_s=args.duration, unbounded=args.unbounded)
    except ContractError as exc:
        ap.error(str(exc))
    facts = recovery_contract(args.srt_latency, args.pacer_bytes, imp,
                              args.unbounded)
    stop = {"v": False}

    def handler(signum, frame):
        stop["v"] = True
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    pipe.start()
    print(f"[run] run_id={pipe.run_id} dir={pipe.run_dir} profile={args.profile}")
    print(f"[contract] latency {facts['srt_latency_ms']} ms "
          f"-> {facts['srt_latency_us']} us; pacer {facts['pacer_bytes']} B "
          f"(~{facts['pacer_media_s']} s media); "
          f"recoverable contiguous loss < {facts['recoverable_media_loss_ms']} ms")
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
        rec = pipe.status()["recovery"]
        print(f"recovery_s: {rec['recovery_s']} "
              f"loss_interval_s: {rec['loss_interval_s']} "
              f"largest_frame_gap_s: "
              f"{rec['largest_gap']['duration_s'] if rec['largest_gap'] else None}")
        if rec["errors"]:
            print("recovery evidence errors:")
            for err in rec["errors"]:
                print(f"  - {err}")
        print(f"artifacts: {pipe.run_dir}")


if __name__ == "__main__":
    main()
