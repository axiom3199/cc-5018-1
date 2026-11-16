"""Pipeline orchestrator: real two-end processes bridged by SRT over an
impaired UDP path.

Process graph (all on 127.0.0.1 in this prototype):

  [encoder ffmpeg] --MPEG-TS--> [BoundedPacer] --MPEG-TS--> [srt-gateway ffmpeg]
        (OB truck)                       (bounded queue,        (SRT caller)
                                         drop logging)                  |
                                                              UDP datagrams
                                                                       v
                                              [ImpairmentRelay: jitter /
                                               reorder / burst loss /
                                               blackhole, both directions]
                                                                       |
                                                                       v
                                              [receiver ffmpeg: SRT listener,
                                               real decode -> null + jpeg
                                               snapshots + ashowinfo]
                                                          (studio)
"""
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import config, contract
from .contract import latency_us, recovery_contract, validate_session
from .frameparse import FrameTracker
from .impairment import ImpairmentRelay
from .pacer import BoundedPacer
from .resources import ResourceSampler
from .srtparse import SrtLogParser
from .stats import Stats
from .subtitles import generate as gen_ass


class Pipeline:
    def __init__(self, impairment, srt_latency_ms, pacer_bytes, duration_s=0,
                 run_id=None, unbounded=False):
        # Validate before spawning anything: an out-of-range budget or queue
        # size must fail loudly here, not be silently replaced by a default.
        srt_latency_ms, pacer_bytes = validate_session(
            srt_latency_ms, pacer_bytes, unbounded)
        self.run_id = run_id or uuid.uuid4().hex[:8]
        # media generation inside one run: bumped whenever the encoder restarts
        # (operator reset or post-outage SRT rebuild), because a fresh encoder
        # restarts PTS at zero and old PTS values would otherwise pair/collide
        self._media_gen = 0
        self.run_dir = config.RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.run_id}"
        self.stats = Stats(self.run_dir, fps=config.FPS)
        self.imp = impairment
        self.srt_latency_ms = srt_latency_ms
        # libsrt takes microseconds; convert once here and use everywhere
        self.srt_latency_us = latency_us(srt_latency_ms)
        self.unbounded = unbounded
        self.pacer_bytes = 0 if unbounded else pacer_bytes
        self.duration_s = duration_s
        self.procs = {}
        self.pacer = None
        self.relay = None
        self.sampler = ResourceSampler(self.stats)
        self.parsers = {
            "gateway": SrtLogParser("gateway", self.stats),
            "receiver": SrtLogParser("receiver", self.stats),
        }
        self.trackers = {
            "sender_video": FrameTracker("sender", self.stats),
            "receiver_video": FrameTracker("receiver", self.stats),
            "receiver_audio": FrameTracker("receiver", self.stats, audio=True),
        }
        self.threads = []
        self._stop = threading.Event()
        # generation counter bumped by every coordinated teardown (reconnect or
        # timeline reset); supervise_srt uses it to ignore exits it did not see.
        # Starts at 1 so the first genuine peer exit (gen captured as 1) is not
        # mistaken for a rebuild that already happened.
        self._epoch = 1
        self._epoch_lock = threading.Lock()
        # set while an operator-driven timeline reset is tearing the peers down,
        # so the SRT supervisor never reports those deliberate exits as
        # connection interruptions
        self._reset_in_progress = threading.Event()
        # serialises the two rebuild paths (SRT supervisor and timeline reset);
        # a plain Lock because _epoch_lock is already held when reconnect runs
        self._rebuild_lock = threading.Lock()
        self._conn_state = "idle"
        self._conn_lock = threading.Lock()
        self.meta = {
            "run_id": self.run_id,
            "started_wall": time.time(),
            "impairment": impairment.to_dict(),
            "srt_latency_ms": srt_latency_ms,
            "srt_latency_us": self.srt_latency_us,
            "pacer_bytes": self.pacer_bytes,
            "resolution": f"{config.WIDTH}x{config.HEIGHT}",
            "fps": config.FPS,
            "video_bitrate": config.VIDEO_BITRATE,
            "audio_bitrate": config.AUDIO_BITRATE,
        }

    # ---------------------------------------------------------- lifecycle
    def start(self):
        rd = self.run_dir
        (rd / "snapshots").mkdir()
        for tr in self.trackers.values():
            tr.reset_epoch(self.run_id, self._media_gen)
        ass_path = gen_ass(self.run_id, rd / "markers.ass",
                           self.duration_s or 36000)
        self.stats.event("run_start", run_id=self.run_id)

        # 1) impairment relay (start first; SRT peers need fixed ports)
        self.relay = ImpairmentRelay(
            config.RELAY_IN_PORT, config.SRT_RECEIVER_PORT, self.imp, self.stats)
        self.relay.start()
        # the relay anchors the blackhole window at construction, not at
        # start(); record it so evidence checks compare against the real window
        self.meta["relay_started_wall"] = time.time()

        # 2) receiver (SRT listener; retries accepting until caller appears)
        recv_cmd = self._receiver_cmd()
        self.procs["receiver"] = self._spawn("receiver", recv_cmd, [
            self.parsers["receiver"], self.trackers["receiver_video"],
            self.trackers["receiver_audio"]])

        # 3) SRT gateway (caller; reconnect=1 keeps the SRT session alive
        #    across a blackhole without rebuilding the pipeline)
        gw_cmd = self._gateway_cmd()
        self.procs["gateway"] = self._spawn("gateway", gw_cmd, [
            self.parsers["gateway"]])

        # 4) encoder -> pacer -> gateway stdin
        enc_cmd = self._encoder_cmd(ass_path)
        self.procs["encoder"] = self._spawn("encoder", enc_cmd, [
            self.trackers["sender_video"]], stdout=subprocess.PIPE)
        self.pacer = BoundedPacer(
            self.procs["encoder"], self.procs["gateway"],
            self.pacer_bytes, self.stats)
        self.pacer.start()

        for name, p in self.procs.items():
            self.sampler.watch(name, p.pid)
        (rd / "meta.json").write_text(json.dumps(self.meta, indent=2))
        threading.Thread(target=self.supervise_srt, name="supervise-srt",
                         daemon=True).start()
        self._start_background()

    def _spawn(self, name, cmd, parsers, stdout=None):
        log = open(self.run_dir / f"{name}.log", "wb")
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=stdout, stderr=subprocess.PIPE,
            bufsize=0, env=dict(os.environ, LD_BIND_NOW="1"))

        def pump():
            for raw in proc.stderr:
                # once the process is dead, any trailing buffered stderr belongs
                # to a torn-down generation: never feed it to live trackers or
                # it would fabricate frames/gaps in the new media epoch
                if proc.poll() is not None:
                    log.write(raw)
                    continue
                log.write(raw)
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                for p in parsers:
                    p.feed(line)
                # only fatal open/IO failures are events; per-packet decoder
                # warnings on a truncated stream (decode_slice_header etc.) are
                # expected during loss/reconnect and recover on their own
                low = line.lower()
                if "error opening" in low or "input/output error" in low \
                        or "immediate exit" in low:
                    self.stats.event("ffmpeg_error", process=name,
                                     line=line[:300])
            log.flush()
            log.close()

        t = threading.Thread(target=pump, name=f"log-{name}", daemon=True)
        t.start()
        self.threads.append(t)
        self.stats.event("process_start", process=name, pid=proc.pid,
                         cmd=" ".join(cmd))
        return proc

    def _start_background(self):
        def loop():
            while not self._stop.is_set():
                self.sampler.sample()
                self._publish_second()
                self._watch_processes()
                time.sleep(1.0)
        threading.Thread(target=loop, name="orchestrator", daemon=True).start()

    # ---------------------------------------------------------- commands
    def _encoder_cmd(self, ass_path):
        return [
            config.FFMPEG, "-hide_banner", "-loglevel", "info",
            "-re",
            "-f", "lavfi", "-i",
            f"testsrc2=size={config.WIDTH}x{config.HEIGHT}:rate={config.FPS}",
            "-f", "lavfi", "-i",
            f"sine=frequency=1000:sample_rate={config.SAMPLE_RATE}",
            "-filter_complex",
            (f"[0:v]showinfo,subtitles='{ass_path}':fontsdir=/usr/share/fonts,"
             f"format=yuv420p[v];"
             f"[1:a]aformat=sample_rates={config.SAMPLE_RATE}:"
             f"channel_layouts=stereo[a]"),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-b:v", config.VIDEO_BITRATE, "-maxrate", config.VIDEO_BITRATE,
            "-bufsize", str(int(config.VIDEO_BITRATE[:-1]) * 1000 // 2),
            "-g", str(config.GOP), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", config.AUDIO_BITRATE, "-ar",
            str(config.SAMPLE_RATE),
            "-f", "mpegts", "pipe:1",
        ]

    def _srt_keepalive(self):
        # This libsrt build silently ignores keepidle/keepinterval query
        # options and exposes no keepalive socket option in ffmpeg, so long
        # blackholes break the SRT session by design; supervise_srt() rebuilds
        # it. Kept as a hook for builds that do honour these options.
        return ""

    def _gateway_cmd(self):
        url = (contract.gateway_url(self.run_id, self.srt_latency_ms)
               + self._srt_keepalive())
        return [
            config.FFMPEG, "-hide_banner", "-loglevel", "debug",
            # pipe probing would block output (SRT connect) for several
            # seconds while find_stream_info analyses the MPEG-TS input
            "-analyzeduration", "500000", "-probesize", "1000000",
            "-f", "mpegts", "-i", "pipe:0",
            "-c", "copy", "-f", "mpegts", url,
        ]

    def _receiver_cmd(self):
        url = (contract.receiver_url(self.run_id, self.srt_latency_ms)
               + self._srt_keepalive())
        snap = str(self.run_dir / "snapshots" / "live.jpg")
        # Real decode on both outputs: null muxer consumes every decoded video
        # and audio frame (proof of decode), image2 keeps overwriting one jpeg
        # for the web page (the burned-in timecode/frame number is visible).
        return [
            config.FFMPEG, "-hide_banner", "-loglevel", "debug",
            "-y",
            # keep stream probing short: default find_stream_info stalls the
            # SRT reader for ~5 s and overflows the receiver TSBPD buffer
            "-analyzeduration", "500000", "-probesize", "1000000",
            # low_delay disables frame-thread decoder buffering (~400 ms of
            # extra latency otherwise); genpts keeps media timestamps intact
            "-fflags", "+genpts", "-flags", "low_delay",
            "-i", url,
            "-filter_complex",
            "[0:v]showinfo,split=2[dec][snap];"
            "[snap]fps=1,scale=640:-1[snapout];"
            "[0:a]ashowinfo[aout]",
            "-map", "[snapout]",
            "-f", "image2", "-update", "1", "-frames:v",
            str(10 ** 9), snap,
            "-map", "[dec]", "-map", "[aout]",
            "-f", "null", "-",
        ]

    # ---------------------------------------------------------- monitoring
    def _watch_processes(self):
        # encoder exit is the only unmanaged transition; the SRT session has
        # its own coordinated supervisor handling connection interruptions
        enc = self.procs.get("encoder")
        if enc is None:
            return
        if enc.poll() is not None and not self._stop.is_set() \
                and id(enc) != getattr(self, "_last_enc_exit_id", None):
            self._last_enc_exit_id = id(enc)
            self.stats.event("encoder_exit", rc=enc.poll())

    def supervise_srt(self):
        """Coordinate the two SRT peers as one reconnectable session.

        Connection interruption = either peer lost while the run_id and media
        timeline continue. The whole session is rebuilt: receiver must be
        listening before the gateway dials, and all in-flight stale bytes in
        the pacer and relay are flushed so the new epoch starts clean.

        Timeline resets kill the same peers on purpose; the generation counter
        ensures those deliberate exits are never double-reported as connection
        interruptions or rebuilt twice.
        """
        while not self._stop.is_set():
            # a timeline reset is deliberately tearing down / rebuilding peers
            if self._reset_in_progress.is_set():
                time.sleep(0.2)
                continue
            recv = self.procs.get("receiver")
            gw = self.procs.get("gateway")
            if recv is None or gw is None:
                time.sleep(0.2)
                continue
            if recv.poll() is None and gw.poll() is None:
                time.sleep(0.2)
                continue
            # at least one peer is dead and no reset owns the teardown
            with self._rebuild_lock:
                # re-check under the lock: a reset/rebuild may have replaced
                # the dead peers while we waited
                recv2 = self.procs.get("receiver")
                gw2 = self.procs.get("gateway")
                if recv2 is None or gw2 is None \
                        or (recv2.poll() is None and gw2.poll() is None) \
                        or self._reset_in_progress.is_set():
                    time.sleep(0.2)
                    continue
                self.stats.event(
                    "connection_interruption",
                    receiver_rc=recv2.poll(), gateway_rc=gw2.poll(),
                    note="SRT session lost; rebuilding whole SRT session")
                self._rebuild_srt_session()

    def _kill_proc(self, proc):
        """Immediate teardown of a peer being replaced.

        SIGTERM can block for seconds inside a connected libsrt socket; these
        processes are discarded wholesale on rebuild/reset, so SIGKILL is both
        acceptable and required to keep recovery fast.
        """
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _restart_encoder(self, ass_name=None):
        """Start a fresh encoder for a new media generation.

        The old producer kept emitting live PTS through the outage; reusing it
        for a rebuilt SRT session makes the receiver buffer and replay the
        backlog. A new encoder restarts the media timeline at zero so recovery
        is provably at the live edge.
        """
        old_enc = self.procs.get("encoder")
        if old_enc and old_enc.poll() is None:
            self._last_enc_exit_id = id(old_enc)  # deliberate, not an error
            self._kill_proc(old_enc)
        self._media_gen += 1
        for tr in self.trackers.values():
            tr.reset_epoch(self.run_id, self._media_gen)
        ass_path = self.run_dir / (ass_name or "markers.ass")
        if not ass_path.exists():
            gen_ass(self.run_id, ass_path, self.duration_s or 7200)
        enc = self._spawn(
            "encoder", self._encoder_cmd(ass_path),
            [self.trackers["sender_video"]], stdout=subprocess.PIPE)
        self.procs["encoder"] = enc
        self.sampler.watch("encoder", enc.pid)
        return enc

    def _rebuild_srt_session(self):
        # called only from _rebuild_lock critical sections; bump the generation
        # without re-entering _epoch_lock (which the caller may already hold)
        self._epoch += 1
        if self.unbounded:
            # Anti-pattern comparison only: keep the encoder and every byte it
            # produced during the outage, so after reconnect the receiver must
            # drain a growing backlog (old pictures first) instead of resuming
            # at the live edge. Only the SRT peers are replaced.
            for name in ("gateway", "receiver"):
                self._kill_proc(self.procs.get(name))
        else:
            # default live-edge policy: tear down peers AND the producer so the
            # new media generation restarts PTS at zero with no replayable past
            for name in ("gateway", "receiver", "encoder"):
                old = self.procs.get(name)
                if old and old.poll() is None:
                    if name == "encoder":
                        self._last_enc_exit_id = id(old)
                    self._kill_proc(old)
        # only the receiver binds a fixed local port; the gateway is a UDP
        # caller on an ephemeral port, while RELAY_IN_PORT belongs to the long-lived
        # impairment relay and must never be waited on
        self._await_port_free("receiver")
        # If the impairment relay is still inside a blackhole, any new SRT
        # connection would be strangled at birth; wait for the window to end.
        if self.relay is not None:
            wait_s = self.relay.blackhole_ends_in()
            if wait_s > 0:
                self.stats.event("await_blackhole_end", wait_s=round(wait_s, 1))
                deadline = time.monotonic() + wait_s + 0.5
                while not self._stop.is_set() and time.monotonic() < deadline:
                    time.sleep(0.2)
        if self.unbounded:
            # keep pacer contents and relay heaps: the backlog is the point of
            # this comparison, so it must survive the reconnect
            enc = self.procs.get("encoder")
        else:
            # drop all stale buffered media; recovery resumes at the live edge
            if self.pacer:
                self.pacer.reset_timeline()
            if self.relay:
                self.relay.reset_timeline()
            # fresh producer: the new media generation starts PTS at zero, so no
            # pre-outage frame can be replayed into the rebuilt session
            enc = self._restart_encoder()
        attempt = 0
        while not self._stop.is_set():
            try:
                recv = self._spawn_peer("receiver")
                if not self._wait_bind(config.SRT_RECEIVER_PORT, 2.0):
                    self.stats.event("peer_restart_failed", process="receiver",
                                     attempt=attempt, note="no UDP bind")
                    recv.kill()
                    self._await_port_free("receiver")
                    attempt += 1
                    time.sleep(0.8)
                    continue
                gw = self._spawn_peer("gateway")
                if self._wait_peer_exit(gw, 1.5) is not None:
                    self.stats.event("peer_restart_failed", process="gateway",
                                     attempt=attempt, rc=gw.poll())
                    gw.kill()
                    recv.kill()
                    self._await_port_free("receiver")
                    attempt += 1
                    time.sleep(0.8)
                    continue
            except OSError as exc:
                self.stats.event("peer_restart_failed", attempt=attempt,
                                 error=str(exc))
                attempt += 1
                time.sleep(1.0)
                continue
            if self.pacer:
                if not self.unbounded:
                    self.pacer.reattach_src(enc)
                    self.pacer.reattach_dst(gw,
                                            flush_reason="session_reconnect")
                else:
                    # keep the same encoder and its accumulated backlog; only
                    # point the writer at the new gateway without flushing
                    self.pacer.reattach_dst(gw)
            self.sampler.watch("receiver", recv.pid)
            self.sampler.watch("gateway", gw.pid)
            self.stats.event("srt_session_restarted", attempt=attempt,
                             receiver_pid=recv.pid, gateway_pid=gw.pid,
                             encoder_pid=enc.pid, media_generation=self._media_gen,
                             live_edge=not self.unbounded)
            return
        self.stats.event("srt_session_restart_giveup")

    def _wait_bind(self, port, timeout):
        import socket
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(("127.0.0.1", port))
                bound = False
            except OSError:
                bound = True          # ffmpeg already holds the listener port
            finally:
                s.close()
            if bound:
                return True
            time.sleep(0.1)
        return False

    def _wait_peer_exit(self, proc, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                return rc
            time.sleep(0.1)
        return None

    def _await_port_free(self, name="receiver", timeout=5.0):
        # the only fixed local port a peer binds is the receiver's listener
        import socket
        port = config.SRT_RECEIVER_PORT
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.2)
            finally:
                s.close()

    def _spawn_peer(self, name):
        parsers = [self.parsers[name]]
        if name == "receiver":
            parsers += [self.trackers["receiver_video"],
                        self.trackers["receiver_audio"]]
            cmd = self._receiver_cmd()
        else:
            cmd = self._gateway_cmd()
        proc = self._spawn(name, cmd, parsers)
        self.procs[name] = proc
        return proc

    def reset_timeline(self, reason="operator_reset"):
        """Media-timeline interruption: new run_id, new epoch, no stale packets.

        Distinct from a connection interruption: the media timeline itself is
        invalid, so the run_id changes and every in-flight byte is discarded.
        The whole SRT session is rebuilt under the new streamid so neither the
        gateway socket nor the receiver can replay the previous epoch.
        """
        with self._epoch_lock, self._rebuild_lock:
            self._reset_in_progress.set()
            try:
                new_id = uuid.uuid4().hex[:8]
                self.stats.event("timeline_reset", old_run=self.run_id,
                                 new_run=new_id, reason=reason)
                self._epoch += 1
                self.run_id = new_id
                self._media_gen = 0
                self.meta["run_id"] = new_id
                self.meta["timeline_resets"] = self.meta.get("timeline_resets", 0) + 1
                # tear down both SRT peers (the encoder is restarted below)
                for name in ("gateway", "receiver"):
                    self._kill_proc(self.procs.get(name))
                self._await_port_free("receiver")
                # flush all stale media at every queue before rebuilding
                if self.pacer:
                    self.pacer.reset_timeline()
                if self.relay:
                    self.relay.reset_timeline()
                # fresh producer, new run_id markers, PTS restarts at zero
                ass_name = f"markers-{new_id}.ass"
                gen_ass(new_id, self.run_dir / ass_name,
                        self.duration_s or 7200)
                enc = self._restart_encoder(ass_name=ass_name)
                # rebuild the SRT session (new streamid includes the new run_id)
                recv, gw = self._build_srt_peers()
                # rewire the bounded queue onto both fresh process pipes and
                # revive its threads (the old encoder EOF had stopped them)
                if self.pacer:
                    self.pacer.reattach_src(enc)
                    self.pacer.reattach_dst(gw, flush_reason="timeline_reset")
            finally:
                self._reset_in_progress.clear()
        self._wait_connected(timeout=8.0)
        self.stats.event("timeline_epoch_ready", new_run=new_id)

    def _wait_connected(self, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            snap = self.parsers["receiver"].snapshot()
            if snap.get("connection_state") == "connected":
                return True
            time.sleep(0.2)
        self.stats.event("timeline_connect_timeout",
                         note="new epoch SRT session not confirmed in time")
        return False

    def _build_srt_peers(self):
        """Spawn receiver (listening) then gateway (calling); used by resets."""
        recv = self._spawn_peer("receiver")
        if not self._wait_bind(config.SRT_RECEIVER_PORT, 2.0):
            self.stats.event("peer_restart_failed", process="receiver",
                             note="no UDP bind during timeline reset")
        gw = self._spawn_peer("gateway")
        if self._wait_peer_exit(gw, 1.5) is not None:
            self.stats.event("peer_restart_failed", process="gateway",
                             note="gateway died during timeline reset",
                             rc=gw.poll())
        self.sampler.watch("receiver", recv.pid)
        self.sampler.watch("gateway", gw.pid)
        return recv, gw

    def update_impairment(self, imp):
        self.imp = imp
        self.meta["impairment"] = imp.to_dict()
        self.relay.update(imp)
        self.stats.event("impairment_update", profile=imp.to_dict())

    def _publish_second(self):
        pacer = self.pacer.snapshot() if self.pacer else {}
        relay = self.relay.snapshot() if self.relay else {}
        srt_gw = self.parsers["gateway"].snapshot()
        srt_rcv = self.parsers["receiver"].snapshot()
        sample = {"frames_decoded": self.trackers["receiver_video"].count,
                  "frame_gaps": self.trackers["receiver_video"].gaps}
        for k, v in pacer.items():
            sample[f"pacer_{k}"] = v
        for direction, ct in relay.items():
            if isinstance(ct, dict):
                for k, v in ct.items():
                    sample[f"relay_{direction}_{k}"] = v
            else:
                sample[f"relay_{direction}"] = ct
        for k, v in srt_gw.items():
            sample[f"srtgw_{k}"] = v
        for k, v in srt_rcv.items():
            sample[f"srtrcv_{k}"] = v
        self.stats.tick_second(sample)

    def status(self):
        from . import recovery
        procs = {name: {"pid": p.pid, "alive": p.poll() is None,
                        "rc": p.poll()}
                 for name, p in self.procs.items()}
        st = self.stats.summary()
        st.update({
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "frame_gaps": self.trackers["receiver_video"].gaps,
            "processes": procs,
            "pacer": self.pacer.snapshot() if self.pacer else None,
            "relay": self.relay.snapshot() if self.relay else None,
            "srt": {"gateway": self.parsers["gateway"].snapshot(),
                    "receiver": self.parsers["receiver"].snapshot()},
            "impairment": self.imp.to_dict(),
            "srt_latency_ms": self.srt_latency_ms,
            "srt_latency_us": self.srt_latency_us,
            "contract": recovery_contract(self.srt_latency_ms,
                                          self.pacer_bytes, self.imp,
                                          self.unbounded),
            "recovery": recovery.summarize(self.stats, self.meta),
            "snapshots": self._snapshot_list(),
        })
        return st

    def _snapshot_list(self):
        p = self.run_dir / "snapshots" / "live.jpg"
        return ["live.jpg"] if p.exists() else []

    def stop(self):
        self._stop.set()
        self.stats.event("run_stop", run_id=self.run_id)
        if self.pacer:
            self.pacer.stop()
        if self.relay:
            self.relay.stop()
        for name, p in list(self.procs.items()):
            try:
                p.terminate()
                p.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()
        # finalize frame-level e2e join and export everything
        self._finalize()
        self.stats.export()

    def _finalize(self):
        from .frameparse import join_latency
        rows = join_latency(self.stats.frames, config.FPS)
        if rows:
            import csv
            with open(self.run_dir / "frames_latency.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=sorted(
                    {k for r in rows for k in r}))
                w.writeheader()
                w.writerows(rows)
        self.meta["stopped_wall"] = time.time()
        (self.run_dir / "meta.json").write_text(
            json.dumps(self.meta, indent=2, ensure_ascii=False))
        (self.run_dir / "status.json").write_text(
            json.dumps(self.status(), indent=2, ensure_ascii=False))
