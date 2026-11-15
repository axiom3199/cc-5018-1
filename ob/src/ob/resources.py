"""CPU/RSS sampling of pipeline processes via /proc (no psutil dependency)."""
import time


def _read_proc(pid):
    base = f"/proc/{pid}"
    try:
        with open(base + "/stat") as f:
            stat = f.read().split()
        # fields after comm (comm may contain spaces, find closing ')')
        rss_pages = int(stat[23])
        utime, stime = int(stat[13]), int(stat[14])
        with open(base + "/io") as f:
            io = {}
            for line in f:
                k, v = line.split()[:2]
                io[k.rstrip(":")] = int(v)
        return utime + stime, rss_pages, io.get("read_bytes", 0) + io.get(
            "write_bytes", 0)
    except (FileNotFoundError, ProcessLookupError, IndexError, PermissionError):
        return None


class ResourceSampler:
    PAGE = 4096

    def __init__(self, stats):
        self.stats = stats
        self.procs = {}          # name -> pid
        self._prev = {}          # pid -> (ticks, mono)
        self._stop = False

    def watch(self, name, pid):
        self.procs[name] = pid

    def sample(self):
        clk = 100.0  # USER_HZ on Linux; ticks per second
        now = time.monotonic()
        for name, pid in self.procs.items():
            data = _read_proc(pid)
            if data is None:
                continue
            ticks, rss_pages, io_bytes = data
            prev = self._prev.get(pid)
            cpu_pct = None
            if prev:
                dt = now - prev[1]
                if dt > 0:
                    cpu_pct = round(
                        max(0.0, (ticks - prev[0]) / clk / dt) * 100, 1)
            self._prev[pid] = (ticks, now)
            self.stats.record_resource({
                "process": name,
                "pid": pid,
                "cpu_pct": cpu_pct,
                "rss_mb": round(rss_pages * self.PAGE / 1e6, 1),
                "io_bytes": io_bytes,
            })
