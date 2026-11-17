#!/usr/bin/env bash
# Reproducible one-shot launcher for the OB SRT return-link prototype.
# Usage:
#   bin/ob.sh web                 # start the control page (http://0.0.0.0:8080)
#   bin/ob.sh session [profile] [duration_s] [srt_latency_ms]
#   bin/ob.sh experiments [duration_s]
#   bin/ob.sh check [--skip-run]  # contract maintenance checker
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=python3
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

cmd="${1:-web}"; shift || true
case "$cmd" in
  web)
    exec "$PY" "$HERE/bin/serve_web.py" "${1:-8080}"
    ;;
  session)
    profile="${1:-healthy}"
    duration="${2:-30}"
    latency="${3:-200}"
    exec "$PY" "$HERE/bin/run_session.py" \
      --profile "$profile" --duration "$duration" --srt-latency "$latency"
    ;;
  experiments)
    duration="${1:-30}"
    exec "$PY" "$HERE/bin/run_experiments.py" --duration "$duration"
    ;;
  check)
    exec "$PY" "$HERE/bin/check_contract.py" "$@"
    ;;
  *)
    echo "unknown command: $cmd"
    echo "use: web | session | experiments | check" >&2
    exit 2
    ;;
esac
