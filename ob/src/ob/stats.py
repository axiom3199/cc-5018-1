"""Thread-safe statistics store with CSV/JSON export."""
import csv
import json
import threading
import time
from pathlib import Path


class Stats:
    def __init__(self, run_dir: Path, fps: int = 25):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.fps = fps
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.events = []          # (t_mono, t_wall, kind, detail dict)
        self.drops = []           # DropEvent-like rows
        self.frames = []          # decoded receiver frames
        self.srt_ticks = {}       # role -> list of parsed srt dicts
        self.series = []          # per-second snapshots
        self.resources = []       # cpu/mem samples
        self._last_second = -1

    def _elapsed(self):
        return time.monotonic() - self.t0

    def event(self, kind, **detail):
        with self.lock:
            self.events.append((time.monotonic(), time.time(), kind, detail))

    def record_drop(self, ev):
        with self.lock:
            self.drops.append({
                "t_mono": ev.t_mono, "t_wall": ev.t_wall,
                "elapsed": ev.t_mono - self.t0,
                "bytes": ev.bytes, "packets": ev.packets,
                "reason": ev.reason, "queue_bytes": ev.queue_bytes,
            })

    def record_frame(self, rec):
        with self.lock:
            rec["elapsed"] = time.monotonic() - self.t0
            self.frames.append(rec)

    def record_srt(self, role, tick):
        with self.lock:
            self.srt_ticks.setdefault(role, []).append(tick)

    def tick_second(self, sample):
        with self.lock:
            sec = int(self._elapsed())
            if sec != self._last_second:
                self._last_second = sec
                sample = dict(sample)
                sample["second"] = sec
                sample["elapsed"] = self._elapsed()
                self.series.append(sample)

    def record_resource(self, rec):
        with self.lock:
            rec["elapsed"] = self._elapsed()
            self.resources.append(rec)

    # ---- export ----
    def export(self):
        with self.lock:
            with open(self.run_dir / "events.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["elapsed", "t_wall", "kind", "detail"])
                for tm, tw, kind, detail in self.events:
                    w.writerow([f"{tm - self.t0:.3f}", tw, kind,
                                json.dumps(detail, ensure_ascii=False)])
            if self.drops:
                with open(self.run_dir / "drops.csv", "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(self.drops[0].keys()))
                    w.writeheader()
                    w.writerows(self.drops)
            if self.frames:
                with open(self.run_dir / "frames.csv", "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(self.frames[0].keys()))
                    w.writeheader()
                    w.writerows(self.frames)
            if self.series:
                with open(self.run_dir / "timeseries.csv", "w", newline="") as f:
                    keys = sorted({k for s in self.series for k in s})
                    w = csv.DictWriter(f, fieldnames=keys)
                    w.writeheader()
                    w.writerows(self.series)
            if self.resources:
                with open(self.run_dir / "resources.csv", "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(self.resources[0].keys()))
                    w.writeheader()
                    w.writerows(self.resources)
            for role, ticks in self.srt_ticks.items():
                if not ticks:
                    continue
                with open(self.run_dir / f"srt_{role}.csv", "w", newline="") as f:
                    keys = sorted({k for t in ticks for k in t})
                    w = csv.DictWriter(f, fieldnames=keys)
                    w.writeheader()
                    w.writerows(ticks)

    def _latency_ms(self):
        slot = 1.0 / self.fps
        sender = {}
        for fr in self.frames:
            if fr["kind"] == "video" and fr["side"] == "sender":
                sender[(fr.get("epoch"), round(fr["pts_time"] / slot))] = fr["t_wall"]
        out = []
        for fr in self.frames:
            if fr["kind"] == "video" and fr["side"] == "receiver":
                sw = sender.get((fr.get("epoch"), round(fr["pts_time"] / slot)))
                if sw is not None:
                    out.append((fr["t_wall"] - sw) * 1000)
        return out

    def summary(self):
        with self.lock:
            drops_by_reason = {}
            drop_bytes = 0
            for d in self.drops:
                drops_by_reason[d["reason"]] = drops_by_reason.get(
                    d["reason"], 0) + d["packets"]
                drop_bytes += d["bytes"]
            lat = self._latency_ms()
            ordered = sorted(lat)
            return {
                "elapsed_s": round(self._elapsed(), 1),
                "frames_decoded": sum(
                    1 for f in self.frames
                    if f["side"] == "receiver" and f["kind"] == "video"),
                "drop_packets": sum(drops_by_reason.values()),
                "drop_bytes": drop_bytes,
                "drops_by_reason": drops_by_reason,
                "events": len(self.events),
                "e2e_ms_avg": round(sum(lat) / len(lat), 1) if lat else None,
                "e2e_ms_p50": round(ordered[len(ordered) // 2], 1)
                if ordered else None,
                "e2e_ms_p95": round(ordered[int(len(ordered) * 0.95)], 1)
                if ordered else None,
                "e2e_ms_max": round(max(lat), 1) if lat else None,
            }
