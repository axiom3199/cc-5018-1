#!/usr/bin/env python3
"""Maintenance check for the SRT latency parameters (real execution evidence).

This is the check that must be run whenever a latency/queue default changes.
It does not read numbers out of the README and trust them; it starts real
sessions and then compares *measured* evidence against the documented contract:

* ``over_budget_retransmit``
    ``short_outage`` (1.2 s hole) with a 250 ms budget.  The hole is far longer
    than the budget, so retransmission cannot repair it: the measured loss
    interval must be of the order of the hole, and the page/status must not
    report anything better than the budget can deliver.
* ``short_outage_within_budget``
    The same 1.2 s hole with a 2 s budget.  The measured loss interval must be
    much shorter than the hole and close to the retransmission window, which is
    the concrete, reproducible meaning of "the budget covered this outage".
* ``outage_rebuild``
    ``outage`` (15 s hole, 200 ms budget).  A session rebuild is expected; the
    check verifies the kernel reports a recovery reference and that the
    receiver resumes decoding, and records the measured loss interval and
    recovery time so the docs can cite execution evidence rather than a
    promise.

Evidence (per-run JSON + the measured values) is written to
``evidence/latency_check.json``.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

from ob import config
from ob.contract import build_impairment, recovery_contract, validate_session
from ob.pipeline import Pipeline

# Documented expectations.  These are tied to the profiles in ob.config; if a
# profile changes, this check must change with it, which is the point.
CASES = [
    {
        "name": "over_budget_retransmit",
        "profile": "short_outage",
        "latency_ms": 250,
        "duration_s": 10,
        "blackhole_ms": 1200,
        # a 1.2 s hole against a 0.25 s budget: the hole cannot be repaired
        "expect_interruption": False,
        "min_loss_interval_s": 0.6,
        "max_recovery_s": 4.0,
        "note": "retransmission window smaller than the loss interval: "
                "the gap is expected and must not be called recovered",
    },
    {
        "name": "short_outage_within_budget",
        "profile": "short_outage",
        "latency_ms": 2000,
        "duration_s": 10,
        "blackhole_ms": 1200,
        "expect_interruption": False,
        # with a 2 s budget the receiver can hold the picture and repair it
        "max_loss_interval_s": 0.9,
        "max_recovery_s": 2.0,
        "note": "budget covers the hole: measured loss interval collapses to "
                "about one retransmission round, not the full outage",
    },
    {
        "name": "outage_rebuild",
        "profile": "outage",
        "latency_ms": 200,
        "duration_s": 36,
        "blackhole_ms": 15000,
        "expect_interruption": True,
        "min_loss_interval_s": 10.0,
        "max_recovery_s": 8.0,
        "note": "hole far exceeds the budget and the session watchdog: a "
                "rebuild is expected and the outage stays a visible gap",
    },
]


def run_case(case):
    imp = build_impairment(case["profile"])
    latency, pacer = validate_session(case["latency_ms"],
                                      config.PACER_BYTES_DEFAULT)
    facts = recovery_contract(latency, pacer, imp)
    pipe = Pipeline(imp, latency, pacer, duration_s=case["duration_s"])
    pipe.start()
    deadline = time.monotonic() + case["duration_s"]
    try:
        while time.monotonic() < deadline and not pipe._stop.is_set():
            time.sleep(1.0)
    finally:
        pipe.stop()
    status = json.loads((pipe.run_dir / "status.json").read_text())
    rec = status["recovery"]
    measured = {
        "case": case["name"],
        "profile": case["profile"],
        "latency_ms": latency,
        "srt_latency_us": status["srt_latency_us"],
        "pacer_media_s": facts["pacer_media_s"],
        "planned_blackhole_ms": case["blackhole_ms"],
        "recoverable_media_loss_ms": facts["recoverable_media_loss_ms"],
        "interruptions": rec["interruptions"],
        "frames": rec["frames"],
        "loss_interval_s": rec["loss_interval_s"],
        "recovery_s": rec["recovery_s"],
        "largest_frame_gap_s": (rec["largest_gap"] or {}).get("duration_s"),
        "evidence_errors": rec["errors"],
        "run_dir": str(pipe.run_dir),
    }

    failures = []
    if rec["frames"] <= 0:
        failures.append("no frames decoded at all")
    if rec["errors"]:
        failures.extend(rec["errors"])
    if case.get("expect_interruption") is not None:
        got = rec["interruptions"] > 0
        if got != case["expect_interruption"]:
            failures.append(
                f"interruptions={rec['interruptions']}, expected "
                f"{'a rebuild' if case['expect_interruption'] else 'none'}")
    loss = rec["loss_interval_s"]
    if loss is None:
        # explicitly a failure: without a measured loss interval the docs
        # cannot claim anything about recovery
        failures.append("no measured loss interval")
    else:
        lo = case.get("min_loss_interval_s")
        hi = case.get("max_loss_interval_s")
        if lo is not None and loss < lo:
            failures.append(
                f"loss_interval_s={loss} shorter than the documented lower "
                f"bound {lo}: the configuration under-promises the outage")
        if hi is not None and loss > hi:
            failures.append(
                f"loss_interval_s={loss} longer than the documented upper "
                f"bound {hi}: a covered outage was not repaired in budget")
    rec_s = rec["recovery_s"]
    if rec_s is None:
        failures.append("no measured recovery time")
    elif rec_s > case["max_recovery_s"]:
        failures.append(
            f"recovery_s={rec_s} exceeds documented bound "
            f"{case['max_recovery_s']}")
    # the documented recoverable-loss bound must be the budget itself
    if facts["recoverable_media_loss_ms"] != latency:
        failures.append(
            "contract recoverable loss bound does not equal the budget")
    measured["failures"] = failures
    measured["ok"] = not failures
    return measured


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", nargs="*", default=None,
                    help="subset of case names; default runs all")
    ap.add_argument("--out", default=str(config.EVIDENCE_DIR /
                                         "latency_check.json"))
    args = ap.parse_args()

    cases = [c for c in CASES
             if args.case is None or c["name"] in args.case]
    if not cases:
        raise SystemExit(f"no matching case in {[c['name'] for c in CASES]}")

    results = []
    for case in cases:
        print(f"[latency-check] {case['name']} "
              f"({case['profile']}, {case['latency_ms']} ms, "
              f"{case['duration_s']} s)", flush=True)
        r = run_case(case)
        results.append(r)
        print("   ->", json.dumps({k: r[k] for k in (
            "loss_interval_s", "recovery_s", "interruptions", "ok")}),
            flush=True)
        print("      evidence:", r["run_dir"], flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cases": results,
        "summary": {
            "total": len(results),
            "failed": sum(1 for r in results if not r["ok"]),
            "ok": all(r["ok"] for r in results),
        },
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"{payload['summary']['total']} cases, "
          f"{payload['summary']['failed']} failed -> {out}")
    return 0 if payload["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
