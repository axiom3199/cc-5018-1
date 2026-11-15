"""Generate an ASS subtitle file that burns frame number + timecode per frame.

libass renders the text; the timecode is derived from the frame index so the
on-picture marker always matches the media PTS of that frame. The receiver's
snapshots therefore show a human-readable marker while frame-level latency is
measured from the machine-readable showinfo logs on both ends.
"""
from pathlib import Path

from . import config

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TC,DejaVu Sans Mono,34,&H0000FFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,3,1,7,24,24,18,1
Style: FN,DejaVu Sans Mono,28,&H0000FFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,3,1,9,24,24,16,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def generate(run_id: str, path: Path, duration_s: float, fps: int = config.FPS):
    # cap the pre-rendered marker file: an unbounded ASS (e.g. 10 h) is hundreds
    # of MB and libass exhausts memory parsing it before the first frame.
    duration_s = min(duration_s, 7200.0)
    n = int(duration_s * fps) + fps
    frame_dur = 1.0 / fps
    lines = [HEADER.format(w=config.WIDTH, h=config.HEIGHT)]
    for i in range(n):
        start = i * frame_dur
        end = start + frame_dur * 0.95
        total = i // fps
        ff = i % fps
        hh = total // 3600
        mm = (total % 3600) // 60
        ss = total % 60
        tc = f"TC {hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},TC,,0,0,0,,"
            f"{{\\pos(24,18)}}{tc}  RUN {run_id}\n")
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},FN,,0,0,0,,"
            f"{{\\pos({config.WIDTH - 360},22)}}FRAME {i:06d}  LIVE OB-1\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path
