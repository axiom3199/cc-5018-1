#!/usr/bin/env python3
"""Executable checker for the return-link configuration contract and examples.

Three layers, all runnable without a camera:

1. **Static contract** - the SRT URLs the pipeline builds are parsed back and
   their ``latency`` parameter must equal ``latency_ms * 1000`` (libsrt
   microseconds).  This is the regression test for the old bug where a
   millisecond value was written into a microsecond field, shrinking a 200 ms
   budget to 0.2 ms.
2. **Supported-range examples** - every example shipped in
   ``examples/return_link_examples.json`` is validated, unknown profiles and
   out-of-range values must be rejected, and a weak-network scenario is
   generated for each supported profile.
3. **Startup diagnostics** - for each profile in ``--scenario`` a real Pipeline
   is started, the processes must come up and the receiver must connect, then
   it is stopped.  This proves the documented launch path works, not just that
   the numbers validate.

Exit code is non-zero if any check fails; the JSON report is written to
``--out`` (default ``evidence/config_check.json``).
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

from ob import config
from ob.contract import (ContractError, build_impairment, describe,
                         gateway_url, pacer_seconds, receiver_url,
                         recovery_contract, validate_session)
from ob.pipeline import Pipeline


def _parse_query(url):
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    return parsed, {k: v[0] for k, v in parse_qs(parsed.query).items()}


def check_static():
    results = []
    for latency_ms in (config.SRT_LATENCY_MS_MIN, 80, 200, 400, 800,
                       config.SRT_LATENCY_MS_MAX):
        gw_parsed, gw = _parse_query(gateway_url("abc12345", latency_ms))
        rc_parsed, rc = _parse_query(receiver_url("abc12345", latency_ms))
        expected = str(latency_ms * 1000)
        results.append({
            "check": "srt_url_latency_unit",
            "latency_ms": latency_ms,
            "expected_us": expected,
            "gateway_us": gw.get("latency"),
            "receiver_us": rc.get("latency"),
            "ok": gw.get("latency") == expected
            and rc.get("latency") == expected,
        })
        results.append({
            "check": "srt_url_ports",
            "gateway_target": f"{gw_parsed.hostname}:{gw_parsed.port}",
            "receiver_bind": f"{rc_parsed.hostname}:{rc_parsed.port}",
            "ok": gw_parsed.port == config.RELAY_IN_PORT
            and rc_parsed.port == config.SRT_RECEIVER_PORT,
        })
        contract = recovery_contract(latency_ms, config.PACER_BYTES_DEFAULT)
        results.append({
            "check": "recovery_bound_matches_budget",
            "latency_ms": latency_ms,
            "recoverable_media_loss_ms": contract["recoverable_media_loss_ms"],
            "ok": contract["recoverable_media_loss_ms"] == latency_ms,
        })
    # pacer seconds must be computed, not asserted: 600 kB is ~3 s, not 0.4 s
    secs = pacer_seconds(config.PACER_BYTES_DEFAULT)
    results.append({
        "check": "pacer_default_media_seconds",
        "pacer_bytes": config.PACER_BYTES_DEFAULT,
        "seconds": round(secs, 2),
        "documented_in_readme_within_0.5s": None,
        "ok": secs > 1.0,
    })
    return results


def check_examples(path):
    results = []
    data = json.loads(path.read_text())
    for example in data["examples"]:
        name = example["name"]
        try:
            imp = build_impairment(example.get("profile", "healthy"),
                                   example.get("impairment"))
            latency, pacer = validate_session(
                example.get("srt_latency_ms", config.SRT_LATENCY_MS_DEFAULT),
                example.get("pacer_bytes", config.PACER_BYTES_DEFAULT),
                example.get("unbounded", False))
            results.append({"check": "example_valid", "name": name,
                            "latency_ms": latency, "pacer_bytes": pacer,
                            "profile": imp.name, "ok": True})
        except ContractError as exc:
            results.append({"check": "example_valid", "name": name,
                            "error": str(exc),
                            "ok": example.get("expect_error", False)})
    for bad in data.get("must_reject", []):
        try:
            build_impairment(bad.get("profile", "healthy"), bad.get("values"))
            results.append({"check": "reject_invalid", "name": bad["name"],
                            "ok": False,
                            "error": "was accepted but must be rejected"})
        except ContractError:
            results.append({"check": "reject_invalid", "name": bad["name"],
                            "ok": True})
    return results


def check_generated_weak_network():
    """Every supported profile must generate a valid, described scenario."""
    results = []
    for name in sorted(config.PROFILES):
        imp = build_impairment(name)
        facts = recovery_contract(config.SRT_LATENCY_MS_DEFAULT,
                                  config.PACER_BYTES_DEFAULT, imp)
        results.append({
            "check": "weak_network_scenario",
            "profile": name,
            "impairment": imp.to_dict(),
            "recoverable_media_loss_ms": facts["recoverable_media_loss_ms"],
            "blackhole_note": facts.get("planned_blackhole_note"),
            "ok": imp.name == name,
        })
    # generate a short-outage scenario across the supported budget range: the
    # same planned hole must be described as recoverable above its length and
    # unrecoverable below it
    for budget in (250, 2000):
        imp = build_impairment("short_outage")
        facts = recovery_contract(budget, config.PACER_BYTES_DEFAULT, imp)
        expect = budget > imp.blackhole_ms
        results.append({
            "check": "generated_blackhole_vs_budget",
            "latency_ms": budget,
            "blackhole_ms": imp.blackhole_ms,
            "expected_recoverable": expect,
            "reported_recoverable": facts["planned_blackhole_recoverable"],
            "ok": facts["planned_blackhole_recoverable"] == expect,
        })
    return results


def check_startup(scenarios, connect_timeout=15.0):
    results = []
    for name in scenarios:
        imp = build_impairment(name)
        latency, pacer = validate_session(config.SRT_LATENCY_MS_DEFAULT,
                                          config.PACER_BYTES_DEFAULT)
        pipe = Pipeline(imp, latency, pacer, duration_s=0)
        record = {"check": "startup_diagnostic", "profile": name,
                  "run_id": pipe.run_id, "run_dir": str(pipe.run_dir)}
        try:
            pipe.start()
            deadline = time.monotonic() + connect_timeout
            connected = False
            decoded = 0
            while time.monotonic() < deadline:
                alive = {n: p.poll() is None for n, p in pipe.procs.items()}
                if not all(alive.values()):
                    break
                state = pipe.parsers["receiver"].snapshot().get(
                    "connection_state")
                decoded = pipe.trackers["receiver_video"].count
                if state == "connected":
                    connected = True
                    # a bound socket is not proof of a working return link:
                    # require the studio to decode real pictures
                    if decoded > 0:
                        break
                time.sleep(0.4)
            st = pipe.status()
            record.update({
                "connected": connected,
                "processes_alive": {n: p.poll() is None
                                    for n, p in pipe.procs.items()},
                "connection_state": st["srt"]["receiver"].get(
                    "connection_state"),
                "srt_latency_us": st["srt_latency_us"],
                "frames_decoded": st["frames_decoded"],
                "recovery_errors": st["recovery"]["errors"],
                "ok": connected and st["frames_decoded"] > 0,
            })
        finally:
            pipe.stop()
        results.append(record)
    return results


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples",
                    default=str(HERE / "examples" /
                                "return_link_examples.json"))
    ap.add_argument("--scenario", nargs="*", default=[],
                    help="profiles to start for a real startup diagnostic; "
                         "empty runs only the static checks")
    ap.add_argument("--out", default=str(config.EVIDENCE_DIR /
                                         "config_check.json"))
    args = ap.parse_args()

    results = {
        "contract": describe(),
        "checks": check_static()
        + check_examples(Path(args.examples))
        + check_generated_weak_network(),
    }
    if args.scenario:
        results["checks"] += check_startup(args.scenario)

    failed = [c for c in results["checks"] if not c.get("ok")]
    results["summary"] = {
        "total": len(results["checks"]),
        "failed": len(failed),
        "ok": not failed,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for c in results["checks"]:
        if not c.get("ok"):
            print("FAIL", json.dumps(c, ensure_ascii=False))
    print(f"{results['summary']['total']} checks, "
          f"{results['summary']['failed']} failed -> {out}")
    return 0 if results["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
