# Scratch (one-off validation scripts)

These are the ad-hoc scripts used while building the prototype. They are kept
because the full outage/reset flows take ~40 s each and are handy for manual
runs, but they are **not** the operations entry point: use the control page
(`bin/ob.sh web`) for field return-link work, and `bin/check_config.py` /
`bin/check_latency.py` for the maintained checks.

They have been updated for the corrected latency contract (the `Pipeline`
constructor now takes **milliseconds** and validates ranges). Any stale
recorded outputs from before the microsecond fix live under `runs/` and are
superseded by `evidence/*.json`.

Run from `ob/`:

    python3 scratch/test_outage.py
    python3 scratch/test_backlog.py
    python3 scratch/test_reset.py
