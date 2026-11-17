#!/usr/bin/env python3
"""Maintenance checker for the OB return-link latency/queue contract.

Three layers, cheapest first:

1. **Contract audit** (no processes): every documented unit/default/range is
   compared against ``ob.config`` and ``ob.contract``; the CLI and UI defaults
   must agree with the contract; stale symbols must be gone.
2. **Supported-range scenario generation**: weak-network scenarios are built
   only from profiles/ranges the relay can actually represent, plus three
   derived ones the docs must speak to — an over-budget retransmission case
   (jitter far beyond the latency budget), a short outage (absorbed inside the
   budget without a rebuild), and a compact longer-than-budget outage that
   must take the connection-interruption path.
3. **Startup diagnostics**: each scenario is actually started, briefly run,
   and its *own* artifacts are mined for loss interval and recovery time
   (``ob.evidence``), so the printed numbers are execution evidence, not
   constants copied from the README.

Exit code is nonzero if the contract audit fails or any scenario fails to
start, so this doubles as a maintenance gate.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ob import config
from ob.config import PROFILES, Impairment
from ob.contract import (IMPAIRMENT_FIELDS, LATENCY_MS_DEFAULT, LATENCY_MS_MAX,
                         LATENCY_MS_MIN, PACER_BYTES_DEFAULT, PACER_BYTES_MAX,
                         PACER_BYTES_MIN, TS_BYTES_PER_PACKET, contract,
                         pacer_media_seconds, srt_latency_us,
                         validate_impairment_fields, validate_latency_ms,
                         validate_pacer_bytes)
from ob.pipeline import Pipeline
from ob.evidence import loss_intervals, summarize

DOC = Path(__file__).resolve().parents[1] / "README.md"
BIN = Path(__file__).resolve().parent


# --------------------------------------------------------------- audit
def _read(path):
    return Path(path).read_text(encoding="utf-8")


def audit_contract():
    """Static checks: units, defaults, ranges, stale names, doc agreement."""
    problems = []
    c = contract()

    # 1) the conversion point must be exact and used by both SRT peers
    if srt_latency_us(LATENCY_MS_DEFAULT) != LATENCY_MS_DEFAULT * 1000:
        problems.append("srt_latency_us() does not convert ms -> us by 1000")

    pipe_src = _read(Path(__file__).resolve().parents[1] / "src/ob/pipeline.py")
    for expected in ("latency={latency_url_value(self.srt_latency_ms)}",):
        if expected not in pipe_src:
            problems.append(
                "pipeline no longer routes the SRT latency through "
                "latency_url_value() (raw ms would be sent as us)")
    if pipe_src.count("latency={latency_url_value(") != 2:
        problems.append("pipeline must convert latency in both URL builders")

    # 2) CLI/UI defaults equal the contract default
    cli = _read(BIN / "run_session.py")
    if "default=LATENCY_MS_DEFAULT" not in cli:
        problems.append("run_session --srt-latency default not tied to contract")
    if "default=PACER_BYTES_DEFAULT" not in cli:
        problems.append("run_session --pacer-bytes default not tied to contract")
    shell = _read(BIN / "ob.sh")
    if f'${{3:-{LATENCY_MS_DEFAULT}}}' not in shell:
        problems.append(f"ob.sh session default latency != {LATENCY_MS_DEFAULT} ms")
    ui = _read(Path(__file__).resolve().parents[1] / "src/ob/webapp.py")
    if f'value="{LATENCY_MS_DEFAULT}"' not in ui:
        problems.append("web UI latency default != contract")
    if f'value="{PACER_BYTES_DEFAULT}"' not in ui:
        problems.append("web UI pacer default != contract")

    # 3) stale symbols that encoded the old broken unit convention
    stale_names = ["SRT_LATENCY" + "_US", "GATEWAY" + "_SRT"]
    self_path = Path(__file__).resolve()
    for stale in stale_names:
        for path in (Path(__file__).resolve().parents[1] / "src", BIN):
            for py in path.rglob("*.py"):
                if py.resolve() == self_path:
                    continue
                if stale in _read(py):
                    problems.append(f"stale symbol {stale} still used in {py}")

    # 4) doc must state units/ranges/defaults that match the contract
    doc = _read(DOC)
    for needle in (f"{LATENCY_MS_DEFAULT} ms", str(PACER_BYTES_DEFAULT),
                   f"[{LATENCY_MS_MIN}, {LATENCY_MS_MAX}] ms"):
        if needle not in doc:
            problems.append(f"README missing contract statement: {needle!r}")
    if "微秒" not in doc and "microsecond" not in doc.lower():
        problems.append("README does not state that SRT URL latency is in us")
    if "~0.4 s" in doc or "0.4s" in doc:
        problems.append("README still claims the 600000-byte queue is ~0.4 s")

    # 5) every config profile is inside the declared supported ranges
    for name, imp in PROFILES.items():
        try:
            validate_impairment_fields(
                {k: v for k, v in imp.to_dict().items() if k != "name"})
        except ValueError as exc:
            problems.append(f"profile {name} out of contract: {exc}")

    # 6) the contract must REJECT out-of-range input, not silently clamp to a
    #    default: a wrong value that becomes 200 ms would mask operator error
    probes = [
        ("latency below min", lambda: validate_latency_ms(LATENCY_MS_MIN - 1)),
        ("latency above max", lambda: validate_latency_ms(LATENCY_MS_MAX + 1)),
        ("loss pct > 100", lambda: validate_impairment_fields({"loss_pct": 101})),
        ("unknown impairment field",
         lambda: validate_impairment_fields({"no_such_field": 1})),
        ("pacer below one TS packet",
         lambda: validate_pacer_bytes(TS_BYTES_PER_PACKET - 1)),
    ]
    for label, fn in probes:
        try:
            fn()
        except ValueError:
            continue
        problems.append(
            f"contract accepted out-of-range input ({label}); it must reject, "
            f"not fall back to a default")

    # 7) contract arithmetic is internally consistent
    media_s = pacer_media_seconds(PACER_BYTES_DEFAULT)
    if media_s is None or media_s < 0.5:
        problems.append(f"pacer default media time implausible: {media_s}")

    return {"ok": not problems, "problems": problems, "contract": c,
            "pacer_default_media_s": round(media_s, 2)}


def audit_evidence():
    """Evidence derivation must separate lost media from a frozen picture.

    ``loss_intervals`` is read by the UI and by the experiment matrix, so a
    wall-clock-only implementation would report a delivery stall (frames late
    but all present) as lost frames, and the README's loss-interval claim
    would silently become wrong.
    """
    problems = []

    def frame(pts, wall, epoch=("r", 0)):
        return {"side": "receiver", "kind": "video", "epoch": epoch,
                "n": 0, "pts_time": pts, "t_wall": wall, "elapsed": wall}

    # 1) continuous PTS, 0.5 s wall stall: picture frozen, nothing missing
    stall = [frame(0.00, 0.0), frame(0.04, 0.04), frame(0.08, 0.54)]
    g = loss_intervals(stall)
    if len(g) != 1:
        problems.append(f"evidence: wall stall not reported as one interval ({len(g)})")
    else:
        if g[0]["duration_s"] < 0.4:
            problems.append("evidence: wall stall duration under-reported")
        if g[0]["missing_frames"] != 0:
            problems.append(
                f"evidence: a delivery stall was counted as "
                f"{g[0]['missing_frames']} lost frames (PTS is continuous)")

    # 2) genuine media skip inside one epoch
    skipped = [frame(0.00, 0.0), frame(0.04, 0.04), frame(0.44, 0.50)]
    g = loss_intervals(skipped)
    if not g or g[0]["missing_frames"] != 9:
        problems.append(
            "evidence: a real 0.4 s PTS skip was not counted as 9 lost frames "
            f"({g[0]['missing_frames'] if g else None})")

    # 3) across an epoch boundary PTS restarts: no invented frame count
    reset = [frame(0.00, 0.0, ("a", 0)), frame(0.04, 0.04, ("a", 0)),
             frame(0.80, 1.60, ("b", 1))]
    g = loss_intervals(reset)
    if not g:
        problems.append("evidence: epoch-crossing interval not reported")
    else:
        if g[0]["missing_frames"] is not None:
            problems.append(
                "evidence: frame count invented across an epoch boundary "
                "where PTS restarts")
        if "missing_frames_estimate" not in g[0]:
            problems.append(
                "evidence: no wall-clock estimate offered across an epoch")
    return problems


# --------------------------------------------------- scenario generation
def supported_scenarios():
    """Weak-network scenarios within the relay's supported range.

    Named profiles plus three derived cases the contract must answer:
    * ``over_budget``: jitter far above the budget, so retransmissions cannot
      land in time -> documented as unrecoverable within budget.
    * ``short_outage``: blackhole much shorter than the session's survival and
      the latency budget, expected to be absorbed without a rebuild.
    * ``outage``: compact but longer-than-budget blackhole. The full 15 s
      profile is used by the experiment matrix; here a 6 s blackhole at 2 s
      keeps the *same* connection-interruption path runnable in a short check.

    Each entry: (name, impairment, latency_ms, min_duration_s, expectations)
    where expectations keys are optional: rebuild(bool), no_rebuild(bool),
    min_late(int) late TSBPD drops, min_loss(float) loss interval seconds.
    """
    healthy = Impairment(**PROFILES["healthy"].to_dict())
    over = Impairment(name="over_budget", jitter_ms=300, base_delay_ms=20)
    short = Impairment(name="short_outage", blackhole_ms=100,
                       blackhole_at_ms=4000)
    outage = Impairment(name="outage", blackhole_ms=6000, blackhole_at_ms=2000,
                        loss_pct=0.5)
    scen = [
        ("healthy", healthy, LATENCY_MS_DEFAULT, 8,
         {"no_rebuild": True}),
        ("burst_loss", Impairment(**PROFILES["burst_loss"].to_dict()),
         LATENCY_MS_DEFAULT, 8, {"no_rebuild": True}),
        ("over_budget", over, LATENCY_MS_MIN, 8,
         {"no_rebuild": True, "min_late": 1}),
        ("short_outage", short, LATENCY_MS_DEFAULT, 10,
         {"no_rebuild": True}),
        ("outage", outage, LATENCY_MS_DEFAULT, 14,
         {"rebuild": True, "min_loss": 1.0}),
    ]
    # validate everything we are about to run
    for name, imp, latency, _, _ in scen:
        validate_impairment_fields(
            {k: v for k, v in imp.to_dict().items() if k != "name"})
        validate_latency_ms(latency)
        validate_pacer_bytes(PACER_BYTES_DEFAULT)
    return scen


def run_scenario(name, imp, latency, duration):
    """Start a real pipeline, run for ``duration`` and mine its evidence."""
    pipe = Pipeline(imp, latency, PACER_BYTES_DEFAULT, duration_s=duration)
    started = False
    try:
        pipe.start()
        started = all(p.poll() is None for p in pipe.procs.values())
        import time
        time.sleep(duration)
    finally:
        pipe.stop()
    ev = summarize(pipe.run_dir)
    kinds = [e[2] for e in pipe.stats.events]
    return {
        "name": name,
        "started": started,
        "run_dir": str(pipe.run_dir),
        "srt_latency_ms": latency,
        "srt_latency_us": srt_latency_us(latency),
        "frames_decoded": pipe.stats.summary()["frames_decoded"],
        "connection_interruptions": kinds.count("connection_interruption"),
        "session_restarts": kinds.count("srt_session_restarted"),
        "srt_late_dropped": pipe.parsers["receiver"].snapshot().get(
            "rcv_dropped_packets", 0),
        "frame_gaps": pipe.trackers["receiver_video"].gaps,
        "max_loss_interval_s": max(
            (g["duration_s"] for g in ev["loss_intervals"]), default=0.0),
        "max_picture_recovery_s": max(
            (r["picture_recovery_s"] or 0.0 for r in ev["recoveries"]),
            default=0.0),
        "recoveries": ev["recoveries"],
    }


def check_expectations(results):
    problems = []
    for r, (_, _, _, _, exp) in zip(results, supported_scenarios()):
        if not r["started"]:
            problems.append(f"{r['name']}: pipeline did not start")
            continue
        if exp.get("rebuild") and r["connection_interruptions"] < 1:
            problems.append(
                f"{r['name']}: expected a connection interruption, saw none")
        if exp.get("no_rebuild") and r["connection_interruptions"] > 0:
            problems.append(
                f"{r['name']}: unexpected connection interruption "
                f"({r['connection_interruptions']})")
        if "min_late" in exp and r["srt_late_dropped"] < exp["min_late"]:
            problems.append(
                f"{r['name']}: late TSBPD drops {r['srt_late_dropped']} "
                f"< expected {exp['min_late']} (over-budget loss not shown)")
        if "min_loss" in exp and r["max_loss_interval_s"] < exp["min_loss"]:
            problems.append(
                f"{r['name']}: loss interval {r['max_loss_interval_s']} s "
                f"< expected {exp['min_loss']}")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--duration", type=float, default=None,
                    help="override all scenario durations with one value "
                         "(default: per-scenario durations from the scenario "
                         "table; the outage case needs enough time to rebuild)")
    ap.add_argument("--skip-run", action="store_true",
                    help="contract audit only (no processes)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    audit = audit_contract()
    problems = audit_evidence()
    if problems:
        audit["ok"] = False
        audit["problems"] = audit["problems"] + problems
    report = {"audit": audit, "scenarios": []}
    rc = 0 if audit["ok"] else 1

    print("== contract audit ==")
    if audit["ok"]:
        print("  OK: units, defaults, ranges and docs agree")
    else:
        for p in audit["problems"]:
            print(f"  FAIL: {p}")
    print(f"  pacer {PACER_BYTES_DEFAULT} bytes = "
          f"{audit['pacer_default_media_s']} s of media "
          f"(not a latency budget)")

    if not args.skip_run:
        scen = supported_scenarios()
        print(f"\n== running {len(scen)} supported-range scenarios "
              f"(per-scenario durations) ==")
        results = []
        for name, imp, latency, dur, _ in scen:
            r = run_scenario(name, imp, latency,
                             args.duration if args.duration else dur)
            results.append(r)
            print(f"  {name:14s} lat={latency:4d}ms/us={r['srt_latency_us']:7d} "
                  f"frames={r['frames_decoded']:4d} "
                  f"late={r['srt_late_dropped']:4d} "
                  f"rebuild={r['session_restarts']} "
                  f"loss={r['max_loss_interval_s']:.2f}s "
                  f"recover={r['max_picture_recovery_s']:.2f}s")
        report["scenarios"] = results
        problems = check_expectations(results)
        if problems:
            rc = 1
            print("\n  scenario expectation failures:")
            for p in problems:
                print(f"   FAIL: {p}")
        else:
            print("\n  OK: every scenario started and matched its expectation")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.json_out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
