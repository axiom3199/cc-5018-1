"""HTTP control page for the OB return-link prototype (stdlib only).

The UI is a thin shell: every button calls an endpoint that drives the same
Pipeline object the headless runner uses, so the page can only act through
the return-link kernel. Start/stop, impairment changes and timeline resets
that fail are surfaced back into the result area as kernel-reported errors.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config
from .config import PROFILES, Impairment
from .pipeline import Pipeline


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.pipe = None
        self.last_error = None

    def is_running(self):
        return self.pipe is not None and not self.pipe._stop.is_set()

    def start(self, params):
        with self.lock:
            if self.is_running():
                return False, "session already running"
            profile = params.get("profile", "healthy")
            imp = PROFILES.get(profile, Impairment(profile))
            # optional numeric overrides
            for key in ("loss_pct", "jitter_ms", "reorder_pct",
                        "reorder_delay_ms", "burst_k", "burst_loss_pct",
                        "base_delay_ms", "blackhole_ms", "blackhole_at_ms"):
                if params.get(key) not in (None, ""):
                    setattr(imp, key, float(params[key])
                            if "pct" in key or "delay_ms" in key
                            or "jitter" in key or "blackhole" in key
                            else int(float(params[key])))
            latency = int(params.get("srt_latency_ms",
                                     config.SRT_LATENCY_US // 1000))
            pacer_bytes = int(params.get("pacer_bytes",
                                         config.PACER_BYTES_DEFAULT))
            unbounded = params.get("unbounded") in ("1", "true", "on")
            try:
                self.pipe = Pipeline(imp, latency, pacer_bytes,
                                     duration_s=0, unbounded=unbounded)
                self.pipe.start()
                self.last_error = None
                return True, self.pipe.run_id
            except Exception as exc:  # surface kernel failure to the page
                self.last_error = repr(exc)
                self.pipe = None
                return False, self.last_error

    def stop(self):
        with self.lock:
            if not self.is_running():
                return False, "no running session"
            pipe = self.pipe
            self.pipe = None
        # stop outside the lock so status polling is not blocked
        pipe.stop()
        return True, str(pipe.run_dir)

    def update_impairment(self, params):
        with self.lock:
            if not self.is_running():
                return False, "no running session"
            profile = params.get("profile", "custom")
            imp = PROFILES.get(profile, Impairment(profile))
            for key in ("loss_pct", "jitter_ms", "reorder_pct",
                        "reorder_delay_ms", "burst_k", "burst_loss_pct",
                        "base_delay_ms", "blackhole_ms", "blackhole_at_ms"):
                if params.get(key) not in (None, ""):
                    val = float(params[key])
                    setattr(imp, key, int(val) if key in
                            ("jitter_ms", "reorder_delay_ms", "burst_k",
                             "base_delay_ms", "blackhole_ms",
                             "blackhole_at_ms") else val)
            try:
                self.pipe.update_impairment(imp)
                return True, imp.to_dict()
            except Exception as exc:
                self.last_error = repr(exc)
                return False, self.last_error

    def reset_timeline(self):
        with self.lock:
            if not self.is_running():
                return False, "no running session"
            try:
                self.pipe.reset_timeline("web_operator_reset")
                return True, self.pipe.run_id
            except Exception as exc:
                self.last_error = repr(exc)
                return False, self.last_error

    def status(self):
        with self.lock:
            if not self.pipe:
                return {"running": False, "error": self.last_error}
            st = self.pipe.status()
            st["running"] = True
            return st


WEB_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>外场回传 SRT 控制台</title>
<style>
body{font-family:system-ui,'DejaVu Sans',sans-serif;margin:0;background:#0e1116;color:#e6edf3}
header{background:#161b22;padding:14px 22px;border-bottom:1px solid #30363d}
h1{font-size:18px;margin:0}.wrap{display:grid;grid-template-columns:360px 1fr;gap:18px;padding:18px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:12px;color:#9da7b3;margin:10px 0 4px}
select,input{width:100%;box-sizing:border-box;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:8px}
button{background:#238636;color:#fff;border:0;border-radius:6px;padding:10px 14px;margin:6px 6px 0 0;cursor:pointer;font-weight:600}
button.stop{background:#da3633}button.sec{background:#30363d}
button:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;gap:8px}.row>div{flex:1}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid #21262d}
th{color:#9da7b3;font-weight:500}.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}
.ok{background:#123d1f;color:#56d364}.bad{background:#4a1e1e;color:#ff7b72}
img{width:100%;border:1px solid #30363d;border-radius:8px;background:#000}
.muted{color:#9da7b3;font-size:12px}.err{color:#ff7b72;white-space:pre-wrap}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.kpi div{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px}
.kpi b{font-size:20px;display:block}.kpi span{font-size:11px;color:#9da7b3}
</style></head><body>
<header><h1>体育外场 → 演播室 SRT 回传控制台 <span class="muted" id="runid"></span></h1></header>
<div class="wrap">
 <div>
  <div class="card">
   <h3>会话控制</h3>
   <label>损伤配置</label>
   <select id="profile">
    <option>healthy</option><option>jitter</option><option>reorder</option>
    <option>burst_loss</option><option>severe</option><option>outage</option>
   </select>
   <div class="row"><div><label>随机丢包 %</label><input id="loss_pct" type="number" step="0.5" placeholder="0"></div>
   <div><label>抖动 ms</label><input id="jitter_ms" type="number" placeholder="0"></div></div>
   <div class="row"><div><label>SRT 延迟预算 ms</label><input id="srt_latency_ms" type="number" value="200"></div>
   <div><label>有界队列 bytes</label><input id="pacer_bytes" type="number" value="600000"></div></div>
   <label class="muted"><input type="checkbox" id="unbounded" style="width:auto"> 无界队列（仅对照实验：会在恢复后播旧画面）</label>
   <button onclick="cmd('start')" id="btnstart">启动发送</button>
   <button class="stop" onclick="cmd('stop')" id="btnstop" disabled>停止</button>
   <button class="sec" onclick="cmd('apply')" id="btnapply" disabled>热更新损伤</button>
   <button class="sec" onclick="cmd('reset')" id="btnreset" disabled>时间轴中断/重置</button>
  </div>
  <div class="card"><h3>结果与失败反馈</h3><div id="msg" class="muted">未启动</div></div>
 </div>
 <div>
  <div class="card">
   <div class="kpi">
    <div><b id="k_decoded">–</b><span>已解码视频帧</span></div>
    <div><b id="k_e2e">–</b><span>端到端延迟 p50(ms)</span></div>
    <div><b id="k_drop">–</b><span>有界队列丢弃 TS 包</span></div>
    <div><b id="k_srtdrop">–</b><span>SRT 晚到丢包</span></div>
   </div>
   <div class="kpi" style="margin-top:10px">
    <div><b id="k_gap">–</b><span>时间轴帧间隙</span></div>
    <div><b id="k_q">–</b><span>发送队列 bytes</span></div>
    <div><b id="k_burst">–</b><span>中继突发丢包</span></div>
    <div><b id="k_state">–</b><span>SRT 连接状态</span></div>
   </div>
  </div>
  <div class="card">
   <h3>演播室接收画面（烧录帧号/时间码）</h3>
   <img id="snap" alt="等待接收画面…">
  </div>
  <div class="card"><h3>内核状态</h3><table id="tbl"><tbody></tbody></table></div>
 </div>
</div>
<script>
const $=id=>document.getElementById(id);
function params(){return {profile:$('profile').value,loss_pct:$('loss_pct').value,
 jitter_ms:$('jitter_ms').value,srt_latency_ms:$('srt_latency_ms').value,
 pacer_bytes:$('pacer_bytes').value,unbounded:$('unbounded').checked?'1':''};}
async function cmd(action){
 const body=new URLSearchParams({action,...params()});
 const r=await fetch('/api/control',{method:'POST',body:body});
 const j=await r.json();
 $('msg').innerHTML='<span class="'+(j.ok?'ok':'bad')+' pill">'+action+': '+
   (j.ok?'OK':'失败')+'</span> <pre class="err">'+JSON.stringify(j.detail??j)+'</pre>';
 poll();
}
async function poll(){
 const j=await (await fetch('/api/status')).json();
 const run=j.running;
 $('btnstart').disabled=run;$('btnstop').disabled=!run;
 $('btnapply').disabled=!run;$('btnreset').disabled=!run;
 $('runid').textContent=j.run_id?('run '+j.run_id):'';
 if(run){
   $('k_decoded').textContent=j.frames_decoded;
   $('k_e2e').textContent=j.e2e_ms_avg??'–';
   $('k_drop').textContent=j.drop_packets;
   $('k_srtdrop').textContent=(j.srt&&j.srt.receiver&&j.srt.receiver.rcv_dropped_packets)??'–';
   $('k_gap').textContent=j.frame_gaps??'–';
   $('k_q').textContent=j.pacer?j.pacer.queue_bytes:'–';
   $('k_burst').textContent=j.relay?(j.relay.fwd.drop_burst+j.relay.fwd.drop_random):'–';
   $('k_state').textContent=(j.srt&&j.srt.receiver&&j.srt.receiver.connection_state)||'–';
   if(j.snapshots&&j.snapshots.length)$('snap').src='/snapshot?run='+encodeURIComponent(j.run_dir)+'&t='+Date.now();
   const rows=[['run_id',j.run_id],['SRT 延迟预算(ms)',j.srt_latency_ms],
     ['e2e p95(ms)',j.e2e_ms_p95],['e2e max(ms)',j.e2e_ms_max],
     ['队列丢弃(bytes)',j.pacer?j.pacer.bytes_dropped:'–'],
     ['写阻塞次数',j.pacer?j.pacer.write_stall_events:'–'],
     ['中断/事件数',j.events],['产物目录',j.run_dir]];
   $('tbl').innerHTML=rows.map(r=>'<tr><th>'+r[0]+'</th><td>'+r[1]+'</td></tr>').join('');
 }
}
setInterval(poll,1500);poll();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    controller = None

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, "text/html; charset=utf-8",
                       WEB_HTML.encode("utf-8"))
        elif url.path == "/api/status":
            self._send(200, "application/json",
                       json.dumps(self.controller.status()).encode())
        elif url.path == "/snapshot":
            q = parse_qs(url.query)
            run_dir = q.get("run", [""])[0]
            pic = __import__("pathlib").Path(run_dir) / "snapshots" / "live.jpg"
            if pic.exists():
                self._send(200, "image/jpeg", pic.read_bytes())
            else:
                self._send(404, "text/plain", b"no snapshot yet")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if urlparse(self.path).path != "/api/control":
            self._send(404, "text/plain", b"not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        data = parse_qs(self.rfile.read(length).decode())
        params = {k: v[0] for k, v in data.items()}
        action = params.pop("action", "")
        c = self.controller
        if action == "start":
            ok, detail = c.start(params)
        elif action == "stop":
            ok, detail = c.stop()
        elif action == "apply":
            ok, detail = c.update_impairment(params)
        elif action == "reset":
            ok, detail = c.reset_timeline()
        else:
            ok, detail = False, f"unknown action {action}"
        self._send(200, "application/json",
                   json.dumps({"ok": ok, "detail": detail}).encode())


def serve(port=config.WEB_PORT):
    ctl = Controller()
    _Handler.controller = ctl
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"[web] OB return-link console on http://0.0.0.0:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        ctl.stop()
