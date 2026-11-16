# 体育外场 → 演播室 SRT 视频回传原型

真实双进程（外场编码发送 / 演播室解码接收）的 SRT 回传链路，带可配置网络损伤、
有界发送队列、连接中断与媒体时间轴中断的分别处理，以及一个由回传内核驱动的
流媒体控制页。全部 Python 仅用标准库，媒体面使用静态 ffmpeg 7.0.2（`../tools/ffmpeg`）。

## 1. 外场回传的图形入口（首选）

日常外场回传请用流媒体控制页，而不是命令行：

```bash
cd /workspace/ob
bin/ob.sh web            # 默认 8080 端口
# 或指定端口：python3 bin/serve_web.py 9000
```

浏览器打开 `http://<主机IP>:8080`（本机即 http://127.0.0.1:8080）。

页面不是独立脚本，而是同一个回传内核（`ob.pipeline.Pipeline`）的薄外壳：

- **启动发送 / 停止**：后端真实拉起或终止编码器、SRT 网关、SRT 接收器、损伤中继四个进程；
- **热更新损伤**：运行中切换抖动/乱序/突发丢包/黑窗，直接下发到中继内核；
  超出支持范围的数值会被拒绝并在结果区显示原因，**不会静默回退默认值**；
- **时间轴中断/重置**：触发内核的媒体时间轴重置（新 run_id、新 epoch、清队列）；
- **结果区**：1.5s 轮询 `/api/status`，展示已解码帧数、端到端延迟 p50、有界队列丢弃、
  SRT 晚到丢包、帧间隙、连接状态、**实测恢复耗时 / 丢失区间 / 最大画面中断**，
  并显示演播室实时接收画面（烧录帧号/时间码）及画面来源 run_id；
- **失败反馈**：任何内核操作失败都以 `{"ok": false, "detail": ...}` 回到页面结果区，
  例如未启动会话时点“热更新”会显示 `no running session`，配置越界会显示
  `loss_pct: 500.0 outside supported range [0.0, 100.0]`。

启动后页面会立即显示本次会话的**回传配置契约**（延迟预算 ms→us、有界队列字节及其
对应媒体秒数、可重传修复的最大连续丢失）。页面里的“恢复耗时/丢失区间/最大画面中断”
都由解码帧墙钟实际运算，不是配置值；若内核对证据判断异常（例如中断后再无解码帧），
页面以红字提示，而不是显示成功。

控制接口（也可直接 curl）：`POST /api/control`，`action ∈ {start,stop,apply,reset}`；
状态 `GET /api/status`；画面 `GET /snapshot?run=<run_dir>`。

## 2. 命令行（仅辅助；可重复运行）

命令行适合实验/回归，不替代上面的外场回传图形入口：

```bash
# 单次会话（无界面）
bin/ob.sh session healthy 30 200          # profile 时长秒 SRT延迟预算ms
python3 bin/run_session.py --profile outage --duration 40 --srt-latency 400
python3 bin/run_session.py --unbounded --profile outage --duration 40   # 无界对照

# 完整对照矩阵（健康 + 多种损伤 + 延迟预算扫描 + 无界对照）
bin/ob.sh experiments 30                  # 写出 runs/experiments.json 与 .csv

# 配置契约检查器：静态单位/范围 + 可执行样例 + 真实启动诊断
python3 bin/check_config.py
python3 bin/check_config.py --scenario healthy jitter reorder burst_loss

# 时延参数维护检查：超预算重传与短断网的真实执行证据
python3 bin/check_latency.py

# 契约单元测试（毫秒级，守护单位/默认值/范围）
python3 -m unittest discover -s tests
```

同一组入口也可经启动器调用：`bin/ob.sh check-config`、`bin/ob.sh check-latency`、
`bin/ob.sh test`。

CLI 的 `--srt-latency` 单位是**毫秒**，范围 20–8000；`--pacer-bytes` 单位是
**MPEG-TS 字节**，范围 1880–67108864。越界会直接报错退出，不会替换成默认值。

## 3. 进程拓扑

```
[encoder ffmpeg] --MPEG-TS--> [BoundedPacer] --MPEG-TS--> [srt-gateway ffmpeg]
 外场卡车：testsrc2+正弦音      有界字节队列、丢包记录       SRT caller(streamid=ob1-<run>)
 烧录帧号/时间码/run_id                 |
                              UDP 数据报（127.0.0.1）
                                        v
                         [ImpairmentRelay：单 socket 双向，
                          Gilbert-Elliott 突发丢包/抖动/乱序/定时黑窗]
                                        v
                         [receiver ffmpeg：SRT listener，
                          真实解码 -> null(每帧)+image2(每秒 jpg)+ashowinfo]
                                   演播室
```

- 媒体参数固定为 **1280×720 @25fps，视频 1.5 Mbps（libx264 veryfast/zerolatency，
  GOP 25），AAC 96 kbps / 48 kHz**，MPEG-TS 承载，188 字节 TS 包。
- 端口：接收器 SRT 监听 **19001**（`SRT_RECEIVER_PORT`）；损伤中继输入 **19002**
  （`RELAY_IN_PORT`，网关的 SRT 目标）；Web 8080（均在 `config.py`）。

## 4. 音画统一时间基准与端到端延迟测量

- 视频每帧、音频每个 AAC 帧都由 ffmpeg `showinfo`/`ashowinfo` 打出 **媒体 PTS**，
  两端各自再盖一个墙钟时间戳。画面上同时烧录人类可读的 SMPTE 风格时间码、帧号和 run_id。
- 端到端延迟**不用 ping**：发送端与接收端按 `(媒体epoch, 量化到一帧的 PTS)` 配对
  （`frameparse.join_latency`），同一台机器墙钟一致，差值即真实端到端延迟，可由
  `frames.csv` / `frames_latency.csv` 完整复算。
- 接收器加 `-flags low_delay` 关闭解码器帧线程缓冲；不加该标志会稳定多出约 400 ms。
- 帧间隙检测基于 PTS 单调步长（视频 1/25s、音频 1024/48000s），不依赖会在滤镜图间
  重启的 `n` 计数；视频 tracker 精确匹配 `Parsed_showinfo`，不再被 `ashowinfo` 子串污染。

## 5. 回传配置契约：单位、默认值、范围

契约的唯一来源是 `src/ob/config.py` + `src/ob/contract.py`，并由
`bin/check_config.py` 执行校验。此前文档与实现不一致的两处，现已按实际修正：

| 参数 | 操作/CLI 单位 | 下发 ffmpeg/libsrt | 默认 | 支持范围 |
|---|---|---|---|---|
| SRT 延迟预算 `latency` | **毫秒** | `srt://...&latency=<毫秒×1000>`，即**微秒** | 200 ms | 20–8000 ms |
| 有界队列 `pacer_bytes` | **字节**（MPEG-TS） | Pacer 内部字节上限 | 600000 B | 1880–67108864 B |

- **单位陷阱（已修复）**：ffmpeg 的 `srt://` URL `latency` 选项单位是**微秒**。旧实现把
  毫秒直接写进 URL，于是“200 ms 预算”实际只给了 **0.2 ms** 的 TSBPD 预算，任何重传都
  必然晚到。现在只在 `contract.latency_us()` 一处做 `ms×1000`，网关与接收器 URL 都由
  `contract.gateway_url()/receiver_url()` 生成，检查器会把 URL 解析回来核对 us 值。
- **默认值口径**：600000 B 在 1.5 Mbps 视频 + 96 kbps 音频（含 TS 开销约 1.6 Mbps
  ≈ 200 kB/s）下约 **2.9 s 媒体**，不是旧文档写的 0.4 s。这个值由
  `contract.pacer_seconds()` 计算，不再手写。
- 每个损伤开关也声明了支持范围（`config.IMPAIRMENT_RANGES`）。越界或未知 profile
  一律**拒绝**，不会退化成“健康链路 + 另一个名字”。

## 6. 时延预算如何约束重传机会（可执行的维护检查）

SRT 的 TSBPD 延迟预算就是重传的时间窗：超过预算仍未到的包会被 SRT 判定晚到并丢弃
（日志 `RCV-DROPPED ... delayed for X ms`，解析进 `srt_receiver.csv`）。因此
**“这段丢失能否被重传修复”是可计算的事实，不是承诺**：

- 连续丢失区间 **≥ 预算** → 该区间头部不可能在预算内重传到达，必然丢帧；
- 连续丢失区间 **< 预算** → SRT 有机会在预算内补齐（ARQ 重传由接收端在预算内发 NAK，
  发送端仍需持有原包；但本仓默认模式在会话断裂时直接换代，不依赖发送端长期保留原文）；
- 断网时长超过会话存活/看门狗窗口 → 会话被重建，**重建从活边恢复，断网本身仍是一段
  可见的画面中断**。

`bin/check_latency.py` 用真实运行来核对上述事实（写入 `evidence/latency_check.json`，
每条都带 `run_dir` 指向证据）。本次在 10 核 aarch64 单机回环实测：

| 场景 | 预算 | 计划黑窗 | 实测丢失区间 | 实测恢复耗时 | 结论 |
|---|---|---|---|---|---|
| 超预算重传 | 250 ms | 1.2 s | ≈1.3 s | ≈0.34 s | 丢失远长于预算，画面缺失≈黑窗长度 |
| 短断网在预算内 | 2000 ms | 1.2 s | ≈0.24 s | ≈0.11 s | 预算覆盖黑窗，PTS 连续、无可见缺失 |
| 长断网重建 | 200 ms | 15 s | ≈12.5 s | ≈2.4 s | 会话重建，断网仍是可见中断 |

（三行分别对应 `evidence/latency_check.json` 的 `run_dir`：`…095909-52ab6b32`、
`…095931-16f08321`、`…095950-a33c5744`，可直接复算。真机回环测量的具体数值每次会有
波动（例如超预算重传的丢失区间在 1.3–1.5 s 之间），**以 `evidence/latency_check.json`
内的 `run_dir` 与实测值为准**，不以本文表格文字为准。）

同一 `burst_loss` profile 只改预算（30s/组，`evidence/full_matrix.json`）也不再是旧文档
那张“80/200/400/800 差不多”的表——修正单位后预算确实迁移了端到端时延：

| SRT 延迟预算 | 端到端 p50 | 端到端 p95 | SRT 晚到丢包 | 帧间隙 |
|---|---|---|---|---|
| 80 ms | 294.8 ms | 410.6 ms | 20 | 0 |
| 200 ms | 416.2 ms | 529.4 ms | 9 | 1 |
| 400 ms | 617.2 ms | 731.7 ms | 0 | 0 |
| 800 ms | 1019.2 ms | 1135.1 ms | 0 | 0 |

这正是“预算即延迟”的直接证据：加大预算换来更少晚到丢包，但画面对现场越来越晚，**不能
既把预算加到 800 ms 又称其为低延迟**。80 ms 组虽延迟最低，却因重传窗太小留下 20 个晚到
丢包；哪一档可接受取决于外场对时延与完整性的取舍，而不是“越大越稳”。

“恢复耗时”= 从黑窗结束（或连接中断事件）到**首个重新解码帧**的墙钟；“丢失区间”=
接收端无任何视频帧的墙钟区间。两者都来自 `frames.csv`，不是配置值。

不可恢复的情形必须写清楚，不能当作可承诺的可靠性：

- 连续丢失 ≥ 预算：重传无法在截止前到达；
- 断网长到会话被拆：重建只能回到活边，断网期间画面必然缺失，**不接受“无缝”表述**；
- 有界队列只在网关进程仍存活、能继续读 stdin 时才起作用；一旦 SRT 会话断裂、网关退出，
  内核会**杀掉编码器**（默认模式），此时积压根本不会生成，队列上限不参与恢复决策
  （见 §7 的实测说明）。

## 7. 有界队列与“恢复后不先播旧画面”

- `BoundedPacer` 在编码器与 SRT 网关之间维持**有界字节队列**（默认 600000 字节，
  在声明码率下约 2.9 s 媒体）。网关管道设为非阻塞：网关读不动时，超出上限的最旧 TS 包
  在 0x47 同步字节处对齐丢弃，每次丢弃写入 `drops.csv`（原因 `queue_overflow`）。
  这条路径由 `tests/test_pacer.py` 用阻塞管道直接驱动验证（本次外场回放中它并未触发，
  原因见下一条）。
- **实测边界（重要）**：在本仓所有 `runs/` 里 `queue_overflow` **一次都没有发生**。原因是
  黑窗持续到网关进程退出时，监督线程会**杀掉编码器并开启新媒体世代**（`pipeline.py`
  的 `_rebuild_srt_session`，默认有界模式），积压根本没有机会长到 600000 字节；实测队列
  峰值只有约 8–16 KB。重建前队列残留会以 `timeline_reset`（旧材料里也见
  `session_reconnect`）为原因被冲刷（如 `runs/20261116-093828-f8ed6438/drops.csv`
  只有一条 `timeline_reset`、无 `queue_overflow`）。因此**“恢复后不先播旧画面”在本仓主要
  来自“换代重启编码器、清空队列”，而不是“队列上限丢包”**；上限是防止网关卡死但未退出
  时的兜底，不能据此宣称它已在现场生效。
- 黑窗（`outage`：第 8s 起持续 15s）导致 SRT 会话断裂，监督线程协调重建整个 SRT 会话，
  并**重启编码器开启新的媒体世代（PTS 归零）**，重接前把队列残留以 `timeline_reset`
  原因记为丢弃。因此恢复点直接落在活边，不会先排空几分钟积压。
- reader 线程在编码器换代（EOF）时保持存活并等待 `reattach_src`，避免换代竞态让有界
  策略在后续黑窗中悄悄失效（症状是队列恒为 0、黑窗内无丢弃）。
- **无界对照（`--unbounded`）**：重连时保留编码器与全部积压。同一条 30s 黑窗 profile
  实测对比如下（`evidence/full_matrix.json`，每条带 `run_dir`）：

| 30s outage 对照 | 队列峰值 | 后 1/3 时段解码帧 | 媒体世代 | 恢复证据 |
|---|---|---|---|---|
| 有界队列（默认） | 15,228 B | 168 | 2（重建时换代） | 丢失区间≈12.5s，恢复耗时≈2.4s |
| 无界队列（反例） | 3,118,472 B（≈15s 媒体） | 0 | 1（保留积压世代） | 两次连接中断，后 1/3 无新帧 |

  两组真正的差别是**重建时是否换代**：默认模式杀掉编码器、重接清队列，恢复即活边；
  无界模式保留编码器与全部积压，队列线性膨胀到 3 MB 以上，恢复后接收端被旧画面拖住，
  后 1/3 时段一帧新画面都收不到（`frames_in_last_third=0`）。这是“无限缓存不等于低延迟”
  的直接证据，且无界模式仅用于该对照实验。**不要把有界模式的低队列归因于 600 KB 上限**：
  上表已说明该上限在外场回放中未触发，起作用的是换代重启（见本节第二条）。

## 8. 连接中断 vs 媒体时间轴中断（两类事件分开）

- **连接中断（connection_interruption）**：run_id 与媒体时间轴仍然有效，只是 SRT 会话
  丢失。监督线程等待黑窗结束（避免新会话在黑窗内被扼杀）→ 重建 receiver(listener) 再
  网关(caller) → 清在途数据 → 活边恢复。事件链：
  `connection_interruption → await_blackhole_end → srt_session_restarted`。
- **媒体时间轴中断（timeline_reset）**：时间轴本身作废（如导播切新信号）。内核更换
  run_id 与 streamid、媒体世代归零、杀编码器并以新标记文件重启、清所有队列，再重建 SRT。
  事件链：`timeline_reset → timeline_epoch_ready`，与连接中断**互不串用**。
- 世代锁 + 进行中标志保证监督线程不会把主动重置误报成连接中断，也不会重复重建；
  帧记录带 `(run_id, generation)` 标签，跨 epoch 的 PTS 即使都从 0 开始也绝不会配对，
  旧包无法污染新一轮播放。

## 9. 网络损伤模型（`impairment.py`，可热更新）

| profile | 含义 |
|---|---|
| healthy | 无损伤 |
| jitter | 120ms 均匀抖动 + 20ms 基础时延 |
| reorder | 8% 包扣留 90ms 后释放（乱序）+ 30ms 抖动 |
| burst_loss | Gilbert-Elliott：平均突发长度 8，坏态占比 12%，另 1% 随机丢包 |
| severe | 突发长度 12 / 坏态 25% + 150ms 抖动 + 5% 乱序 |
| outage | 第 8s 起 15s 黑窗（双向全丢）+ 0.5% 随机丢包 |
| short_outage | 第 4s 起 1.2s 黑窗，用于“预算覆盖/未覆盖”对照 |
| burst_over_budget | 突发长度 32 / 坏态 55% + 2% 随机丢包，用于超预算演示 |

中继用单个 UDP socket 学习对端，双向各一个延时堆，黑窗计时锚定在构造时刻且不被
重置，保证一次运行只触发一次；`meta.json` 记录 `relay_started_wall` 供证据核对。

## 10. 统计导出（每次运行一个 `runs/<时间戳>-<run_id>/`）

| 文件 | 内容 |
|---|---|
| `events.csv` | 连接中断、重置、重建、黑窗等待、致命 IO 等带时间戳事件 |
| `drops.csv` | 每次有界/重置/重连丢弃的字节数、TS 包数、原因、队列水位 |
| `frames.csv` | 两端每个视频/音频帧：side、epoch、n、PTS、墙钟 |
| `frames_latency.csv` | 按 epoch+PTS 配对后的每帧端到端延迟（ms） |
| `timeseries.csv` | 每秒队列/中继/SRT/解码指标快照 |
| `srt_gateway.csv`、`srt_receiver.csv` | SRT 晚到丢包、NAK、重排序距离、RTT、连接状态 |
| `resources.csv` | 各进程 CPU% 与 RSS（MB），读 /proc，无第三方依赖 |
| `snapshots/live.jpg` | 演播室每秒刷新的接收画面 |
| `meta.json`、`status.json`、`*.log` | 运行参数（含 `srt_latency_us`）、最终状态与恢复证据、各 ffmpeg 原始日志 |

汇总矩阵写在 `runs/experiments.json` / `experiments.csv`。

## 11. 资源范围（在 1280×720@25 / 1.5Mbps+96kbps 下声明）

来自 `resources.csv` 的实测峰值（10 核 aarch64 容器，单机环回；取全部 10 组 30s 对照的
各进程上包络）。本机测量受共享宿主负载影响，CPU 数值波动较大，下表同时给出**健康组单
次**读数与**矩阵上包络**，供规划参考而非硬性 SLA：

| 进程 | 矩阵 CPU 上包络 | 健康组单次 CPU | RSS 峰值 |
|---|---|---|---|
| encoder（x264 实时编码 + libass 烧录） | ≈ 180%（多线程瞬时合并） | ≈ 49% | ≈ 144 MB |
| srt-gateway（仅 `-c copy` 转发） | ≈ 16% | ≈ 3% | ≈ 34 MB |
| receiver（真实解码 + 快照 + showinfo） | ≈ 59% | ≈ 19% | ≈ 92 MB |

Python 编排与损伤中继本身 CPU 占用很低（中继为单线程 UDP 转发）。整体在约 8 GB 内存的
容器内稳定运行；无界队列是唯一会让内存随中断时长线性增长的模式，这也是它只作为
反面对照的原因之一。CPU 上包络高于健康组是因为 `severe`/`outage` 重连期间多进程短时
并发；规划容量时应以健康组稳态为主、上包络为突发余量。

## 12. 目录与源码

```
examples/
  return_link_examples.json  可执行配置样例（被 bin/check_config.py 校验）
tests/
  test_contract.py           单位/范围/恢复上限/URL 契约的单元测试
  test_recovery.py           恢复耗时/丢失区间算法的单元测试
  test_pacer.py              有界队列上限、对齐丢弃、换代重接的直驱测试
evidence/
  config_check.json          静态契约 + 样例 + 启动诊断报告
  latency_check.json         时延维护检查的真实执行证据
  full_matrix.json           全量对照矩阵（含恢复证据）
src/ob/
  config.py       参数、端口、损伤 profile（默认值与支持范围）
  contract.py     单位换算、范围校验、恢复上限、SRT URL 生成
  recovery.py     从解码帧/事件计算恢复耗时与丢失区间
  pipeline.py     四进程编排、SRT 监督、连接/时间轴两类中断、活边恢复
  pacer.py        有界队列、非阻塞写、对齐丢弃、管道重接
  impairment.py   双向 UDP 损伤中继（突发/抖动/乱序/黑窗）
  frameparse.py   showinfo/ashowinfo 解析、PTS 间隙、epoch 延迟配对
  srtparse.py     该 libsrt 实际输出的 SRT 告警/生命周期解析
  subtitles.py    烧录帧号/时间码/run_id 的 ASS 生成
  resources.py    /proc CPU/RSS 采样
  stats.py        线程安全统计与 CSV/JSON 导出
  webapp.py       标准库 http.server 控制页与 API
bin/
  ob.sh 启动器；web/session/experiments
  run_session.py 单次会话；run_experiments.py 对照矩阵
  check_config.py 配置契约与启动诊断检查器
  check_latency.py 时延参数维护检查
```

外场回传的**图形入口是第 1 节的 `bin/ob.sh web` 控制页**；命令行只作辅助。
旧的一次性脚本位于 `scratch/`，已按新契约更新，可继续运行但不作为运维入口。
