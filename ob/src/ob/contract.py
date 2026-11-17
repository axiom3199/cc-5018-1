"""Return-link configuration contract: units, defaults and supported ranges.

This module is the single source of truth shared by the control page, the
headless CLI, the documentation and the maintenance checker. Three things it
pins down, because the prototype previously drifted here:

1. The operator/CLI/UI latency budget is in **milliseconds**. libsrt's
   ``latency=`` URL option is in **microseconds**, so it must be converted
   exactly once (:func:`srt_latency_us`). Passing the millisecond number
   straight into ``latency=`` silently asks for a 200 *microsecond* buffer,
   not 200 ms.
2. The bounded pacer queue is measured in **bytes**; its equivalent media
   time depends on the actual payload rate, so it is derived, never asserted
   from a stale constant.
3. The impairment fields each have an explicit unit and supported range, so a
   scenario the relay cannot represent cannot be generated or accepted.

Nothing here starts processes or reads evidence; it is pure validation so the
checker, the UI and the CLI can reject an out-of-contract setting before any
encoder/SRT peer is spawned.
"""
from . import config

# ---------------------------------------------------------------- units
# Media payload rate the pacer actually carries: video + audio at config
# settings, in bits per second. Used only to express queue bytes as media time.
MEDIA_BITS_PER_SECOND = (
    int(config.VIDEO_BITRATE[:-1]) * 1000 + int(config.AUDIO_BITRATE[:-1]) * 1000
)
TS_BYTES_PER_PACKET = 188

# ---------------------------------------------------------------- latency
LATENCY_UNIT = "ms"
LATENCY_URL_UNIT = "us"
LATENCY_MS_DEFAULT = config.SRT_LATENCY_MS_DEFAULT
LATENCY_MS_MIN = config.SRT_LATENCY_MS_MIN
LATENCY_MS_MAX = config.SRT_LATENCY_MS_MAX

# ---------------------------------------------------------------- pacer
PACER_UNIT = "bytes"
PACER_BYTES_DEFAULT = config.PACER_BYTES_DEFAULT
PACER_BYTES_MIN = TS_BYTES_PER_PACKET          # a cut shorter than one TS packet is bogus
PACER_BYTES_MAX = 64 * 1024 * 1024

# ---------------------------------------------------------------- impairment
# name -> (unit, min, max, integer?)
IMPAIRMENT_FIELDS = {
    "loss_pct": ("percent", 0.0, 100.0, False),
    "burst_k": ("packets/state", 0, 1000, True),
    "burst_loss_pct": ("percent", 0.0, 100.0, False),
    "reorder_pct": ("percent", 0.0, 100.0, False),
    "reorder_delay_ms": ("ms", 0, 5000, True),
    "jitter_ms": ("ms", 0, 2000, True),
    "base_delay_ms": ("ms", 0, 5000, True),
    "blackhole_ms": ("ms", 0, 24 * 3600 * 1000, True),
    "blackhole_at_ms": ("ms", 0, 24 * 3600 * 1000, True),
}


def srt_latency_us(latency_ms):
    """Milliseconds -> microseconds, the unit libsrt's ``latency=`` expects."""
    return int(round(latency_ms * 1000))


def latency_url_value(latency_ms):
    """String form for an ``srt://`` URL ``latency=`` query parameter (us)."""
    return str(srt_latency_us(latency_ms))


def pacer_media_seconds(pacer_bytes, bits_per_second=MEDIA_BITS_PER_SECOND):
    """Equivalent media time of a byte-bounded queue (bytes, not a latency)."""
    if not pacer_bytes:
        return None
    return pacer_bytes * 8 / bits_per_second


def validate_latency_ms(value):
    ms = float(value)
    if not (LATENCY_MS_MIN <= ms <= LATENCY_MS_MAX):
        raise ValueError(
            f"srt latency {value} ms outside supported range "
            f"[{LATENCY_MS_MIN}, {LATENCY_MS_MAX}] ms")
    return int(round(ms))


def validate_pacer_bytes(value, unbounded=False):
    if unbounded:
        return 0
    b = int(value)
    if not (PACER_BYTES_MIN <= b <= PACER_BYTES_MAX):
        raise ValueError(
            f"pacer bytes {value} outside supported range "
            f"[{PACER_BYTES_MIN}, {PACER_BYTES_MAX}] bytes")
    return b


def validate_impairment_fields(fields):
    """Validate and coerce a dict of impairment overrides.

    Returns a new dict with each provided field coerced to its declared type.
    Unknown keys raise, so a typo cannot silently do nothing.
    """
    out = {}
    for key, raw in fields.items():
        if key not in IMPAIRMENT_FIELDS:
            raise ValueError(f"unknown impairment field {key!r}")
        unit, lo, hi, is_int = IMPAIRMENT_FIELDS[key]
        val = float(raw)
        if val < lo or val > hi:
            raise ValueError(
                f"impairment {key}={raw} outside supported range "
                f"[{lo}, {hi}] {unit}")
        out[key] = int(val) if is_int else val
    return out


def contract():
    """Machine-readable contract (used by the checker and the docs test)."""
    return {
        "latency": {
            "unit": LATENCY_UNIT, "url_unit": LATENCY_URL_UNIT,
            "default": LATENCY_MS_DEFAULT,
            "min": LATENCY_MS_MIN, "max": LATENCY_MS_MAX,
            "us_per_unit": 1000,
        },
        "pacer": {
            "unit": PACER_UNIT, "default": PACER_BYTES_DEFAULT,
            "min": PACER_BYTES_MIN, "max": PACER_BYTES_MAX,
            "media_bits_per_second": MEDIA_BITS_PER_SECOND,
            "ts_bytes_per_packet": TS_BYTES_PER_PACKET,
        },
        "impairment_fields": {
            k: {"unit": u, "min": lo, "max": hi, "integer": i}
            for k, (u, lo, hi, i) in IMPAIRMENT_FIELDS.items()
        },
    }
