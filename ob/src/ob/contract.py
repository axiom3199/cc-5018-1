"""Return-link configuration contract: units, ranges, and recovery bounds.

Every externally supplied knob passes through this module before a process is
started.  The contract exists because the SRT ``latency`` URL option is in
microseconds while the operator/CLI/page speak milliseconds, and because the
pacer bound was repeatedly mis-stated in seconds.  It deliberately **rejects**
out-of-range values and unknown profile names instead of clamping them to a
default: a typo must look like an error, never like a working weak-network
scenario.
"""
from dataclasses import replace

from . import config


class ContractError(ValueError):
    """A configuration value violates the declared return-link contract."""


# ---------------------------------------------------------------- unit helpers
def latency_us(latency_ms):
    """Convert the operator-facing millisecond budget to libsrt microseconds.

    The conversion lives here and nowhere else.  ffmpeg's ``srt://`` URL takes
    ``latency`` in microseconds; passing a millisecond value straight through
    shrinks the TSBPD budget by 1000x (200 -> 0.2 ms), which silently makes
    every retransmission late.
    """
    return int(validate_int("srt_latency_ms", latency_ms,
                            config.SRT_LATENCY_MS_MIN,
                            config.SRT_LATENCY_MS_MAX)) * 1000


def pacer_seconds(pacer_bytes, video_bitrate=None, audio_bitrate=None):
    """Media seconds the bounded queue can hold at the declared envelope."""
    total_bps = (_bps(video_bitrate or config.VIDEO_BITRATE)
                 + _bps(audio_bitrate or config.AUDIO_BITRATE))
    # MPEG-TS adds ~4% framing; round up to a slightly conservative rate
    bytes_per_s = total_bps * 1.04 / 8.0
    return pacer_bytes / bytes_per_s


def _bps(value):
    value = str(value).strip().lower()
    mult = 1
    if value.endswith("k"):
        mult, value = 1000, value[:-1]
    elif value.endswith("m"):
        mult, value = 1_000_000, value[:-1]
    return float(value) * mult


# ---------------------------------------------------------------- validation
def validate_int(name, value, lo, hi):
    if isinstance(value, bool):
        raise ContractError(f"{name}: boolean is not an integer value")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ContractError(f"{name}: {value!r} is not an integer")
    return validate_range(name, number, lo, hi)


def validate_float(name, value, lo, hi):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ContractError(f"{name}: {value!r} is not a number")
    return validate_range(name, number, lo, hi)


def validate_range(name, number, lo, hi):
    if not (lo <= number <= hi):
        raise ContractError(
            f"{name}: {number} outside supported range [{lo}, {hi}]")
    return number


def build_impairment(profile_name="healthy", overrides=None, base=None):
    """Return a fresh Impairment for a named profile plus explicit overrides.

    Unknown profiles are refused.  The previous behaviour (``PROFILES.get(name,
    Impairment(name))``) turned a misspelt profile into a *healthy* link with a
    different label, which is precisely the kind of default-masks-error failure
    this contract is meant to prevent.  ``base`` is used only for the explicit
    ``custom`` profile of a hot update, where the running values are kept.
    """
    if profile_name == "custom" and base is not None:
        imp = replace(base)
        imp.name = "custom"
    elif profile_name not in config.PROFILES:
        raise ContractError(
            f"unknown profile {profile_name!r}; supported: "
            + ", ".join(sorted(config.PROFILES)))
    else:
        imp = replace(config.PROFILES[profile_name])
        imp.name = profile_name
    for key, raw in (overrides or {}).items():
        if raw in (None, ""):
            continue
        if key not in config.IMPAIRMENT_RANGES:
            raise ContractError(f"unknown impairment field {key!r}")
        lo, hi = config.IMPAIRMENT_RANGES[key]
        if key in config.INT_IMPAIRMENT_FIELDS:
            value = validate_int(key, raw, lo, hi)
        else:
            value = validate_float(key, raw, lo, hi)
        setattr(imp, key, value)
    if imp.blackhole_ms and imp.blackhole_at_ms + imp.blackhole_ms > \
            config.IMPAIRMENT_RANGES["blackhole_at_ms"][1]:
        raise ContractError(
            "blackhole_at_ms + blackhole_ms exceeds the supported 1 h window")
    return imp


def validate_session(latency_ms, pacer_bytes, unbounded=False):
    latency_ms = validate_int("srt_latency_ms", latency_ms,
                              config.SRT_LATENCY_MS_MIN,
                              config.SRT_LATENCY_MS_MAX)
    if unbounded:
        pacer_bytes = 0
    else:
        pacer_bytes = validate_int("pacer_bytes", pacer_bytes,
                                   config.PACER_BYTES_MIN,
                                   config.PACER_BYTES_MAX)
    return latency_ms, pacer_bytes


# ------------------------------------------------------- recovery arithmetic
def recovery_contract(latency_ms, pacer_bytes, impairment=None,
                      unbounded=False):
    """Facts an operator may rely on for one configuration.

    ``recoverable_media_loss_ms`` is the largest *contiguous* media loss SRT
    can repair by retransmission: a packet whose retransmission arrives after
    its TSBPD play deadline is dropped, so a loss interval at least as long as
    the budget cannot be repaired for its head.  A session rebuild after a
    longer interruption resumes at the live edge and necessarily leaves a media
    gap; no latency value turns that gap into seamless video.
    """
    budget = int(latency_ms)
    pacer_s = None if unbounded else pacer_seconds(pacer_bytes)
    facts = {
        "srt_latency_ms": budget,
        "srt_latency_us": budget * 1000,
        "pacer_bytes": 0 if unbounded else int(pacer_bytes),
        "pacer_media_s": None if pacer_s is None else round(pacer_s, 2),
        "recoverable_media_loss_ms": budget,
        "rebuild_required_when_loss_exceeds_ms": budget,
        "unrecoverable_loss": (
            "Any contiguous loss interval >= the SRT latency budget cannot be "
            "repaired by retransmission: the retransmitted packets miss their "
            "TSBPD deadline and are dropped (RCV-DROPPED). A loss interval >= "
            "the session watchdog/keepalive horizon tears the session down; "
            "the rebuild resumes at the live edge and the outage itself remains "
            "a visible media gap. Buffers cannot promise continuity."),
    }
    if pacer_s is not None:
        facts["retransmit_source_window_ms"] = int(pacer_s * 1000)
        facts["unrecoverable_loss"] += (
            " The bounded queue only bounds unsent media while the gateway "
            "process is still reading; if the SRT session tears down, the "
            "default pipeline restarts the encoder (new media generation) and "
            "no outage backlog is produced at all. Queue retention is therefore "
            "a fallback for a stalled-but-alive gateway, not a promise that the "
            "outage can be retransmitted later.")
    if impairment is not None and impairment.blackhole_ms:
        covered = impairment.blackhole_ms < budget
        facts["planned_blackhole_ms"] = impairment.blackhole_ms
        facts["planned_blackhole_recoverable"] = covered
        facts["planned_blackhole_note"] = (
            "blackhole shorter than the budget: SRT may retransmit the "
            "withheld packets after the window ends"
            if covered else
            "blackhole at least as long as the budget: the head of the outage "
            "is unrecoverable; expect a media gap (or a session rebuild)")
    return facts


# ---------------------------------------------------------------- SRT URLs
def gateway_url(run_id, latency_ms, oheadbw=25):
    """Caller URL.  ``latency`` is emitted in microseconds, never in ms."""
    return (f"srt://127.0.0.1:{config.RELAY_IN_PORT}?mode=caller"
            f"&latency={latency_us(latency_ms)}"
            f"&connect_timeout={config.SRT_CONNECT_TIMEOUT_MS}"
            f"&peeridle_timeout={config.SRT_PEERIDLE_TIMEOUT_MS}"
            f"&reconnect=1&streamid=ob1-{run_id}&oheadbw={oheadbw}")


def receiver_url(run_id, latency_ms):
    return (f"srt://0.0.0.0:{config.SRT_RECEIVER_PORT}?mode=listener"
            f"&latency={latency_us(latency_ms)}"
            f"&peeridle_timeout={config.SRT_PEERIDLE_TIMEOUT_MS}"
            f"&streamid=studio-{run_id}")


# ---------------------------------------------------------------- description
def describe():
    """Machine-readable contract used by the checker and the README."""
    return {
        "latency": {
            "operator_unit": "ms",
            "srt_url_unit": "us",
            "default_ms": config.SRT_LATENCY_MS_DEFAULT,
            "range_ms": [config.SRT_LATENCY_MS_MIN,
                         config.SRT_LATENCY_MS_MAX],
            "conversion": "latency_us = latency_ms * 1000",
        },
        "pacer": {
            "unit": "bytes of MPEG-TS (188-byte packets)",
            "default": config.PACER_BYTES_DEFAULT,
            "range": [config.PACER_BYTES_MIN, config.PACER_BYTES_MAX],
            "default_media_s_at_declared_envelope": round(
                pacer_seconds(config.PACER_BYTES_DEFAULT), 2),
            "unbounded_is_comparison_only": True,
        },
        "envelope": {
            "resolution": f"{config.WIDTH}x{config.HEIGHT}",
            "fps": config.FPS,
            "video_bitrate": config.VIDEO_BITRATE,
            "audio_bitrate": config.AUDIO_BITRATE,
            "audio_sample_rate": config.SAMPLE_RATE,
            "gop": config.GOP,
        },
        "ports": {
            "srt_receiver_listener": config.SRT_RECEIVER_PORT,
            "impairment_relay_input": config.RELAY_IN_PORT,
            "web_console": config.WEB_PORT,
        },
        "impairment_ranges": {
            k: list(v) for k, v in config.IMPAIRMENT_RANGES.items()},
        "profiles": {
            name: imp.to_dict() for name, imp in config.PROFILES.items()},
    }
