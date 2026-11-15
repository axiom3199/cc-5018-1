# 体育外场 → 演播室 SRT 视频回传原型

真实双进程（外场编码发送 / 演播室解码接收）的 SRT 回传链路，带可配置网络损伤、
有界发送队列、连接中断与媒体时间轴中断的分别处理，以及一个由回传内核驱动的
流媒体控制页。全部 Python 仅用标准库，媒体面使用静态 ffmpeg 7.0.2（`../tools/ffmpeg`）。

## 1. 怎样启动界面（控制页）

```bash
cd /workspace/ob
bin/ob.sh web            # 默认 8080 端口
# 或指定端口：python3 bin/serve_web.py 9000
```

浏览器打开 `http://<主机IP>:8080`（本机即 http://127.0.0.1:8080）。

页面不是独立脚本，而是同一个回传内核（`ob.pipeline.Pipeline`）的薄外壳：

- **启动发送 / 停止**：后端真实拉起或终止编码器、SRT 网关、SRT 接收器、损伤中继四个进程；
- **热更新损伤**：运行中切换抖动/乱序/突发丢包/黑窗，直接下发到中继内核；
- **时间轴中断/重置**：触发内核的媒体时间轴重置（新 run_id、新 epoch、清队列）；
- **结果区**：1.5s 轮询 `/api/status`，展示已解码帧数、端到端延迟、有界队列丢弃、
  SRT 晚到丢包、帧间隙、连接状态，并显示演播室实时接收画面（烧录帧号/时间码）；
- **失败反馈**：任何内核操作失败都以 `{"ok": false, "detail": ...}` 回到页面结果区，
  例如未启动会话时点“热更新”会显示 `no running session`。

控制接口（也可直接 curl）：`POST /api/control`，`action ∈ {start,stop,apply,reset}`；
状态 `GET /api/status`；画面 `GET /snapshot?run=<run_dir>`。

## 2. 另外两种可重复运行方式

```bash
# 单次会话（无界面，命令行）
bin/ob.sh session healthy 30 200          # profile 时长秒 SRT延迟预算ms
python3 bin/run_session.py --profile outage --duration 40 --srt-latency 400
python3 bin/run_session.py --unbounded --profile outage --duration 40   # 无界对照

# 完整对照矩阵（健康 + 多种损伤 + 延迟预算扫描 + 无界对照）
bin/ob.sh experiments 30                  # 写出 runs/experiments.json 与 .csv
```

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
- 端口：接收器 SRT 监听 19001；损伤中继绑定 19002；Web 8080（均在 `config.py`）。

## 4. 音画统一时间基准与端到端延迟测量

- 视频每帧、音频每个 AAC 帧都由 ffmpeg `showinfo`/`ashowinfo` 打出 **媒体 PTS**，
  两端各自再盖一个墙钟时间戳。画面上同时烧录人类可读的 SMPTE 风格时间码、帧号和 run_id。
- 端到端延迟**不用 ping**：发送端与接收端按 `(媒体epoch, 量化到一帧的 PTS)` 配对
  （`frameparse.join_latency`），同一台机器墙钟一致，差值即真实端到端延迟，可由
  `frames.csv` / `frames_latency.csv` 完整复算。
- 接收器加 `-flags low_delay` 关闭解码器帧线程缓冲；不加该标志会稳定多出约 400 ms。
- 帧间隙检测基于 PTS 单调步长（视频 1/25s、音频 1024/48000s），不依赖会在滤镜图间
  重启的 `n` 计数；视频 tracker 精确匹配 `Parsed_showinfo`，不再被 `ashowinfo` 子串污染。

## 5. 时延预算如何约束重传机会

SRT 的 TSBPD 延迟预算就是重传的时间窗：超过预算仍未到的包会被 SRT 判定晚到并丢弃
（日志 `RCV-DROPPED ... delayed for X ms`，解析进 `srt_receiver.csv`）。同一突发丢包
profile（`burst_loss`）下只改延迟预算，实测（每组 30s，见 `runs/experiments.json`）：

| SRT 延迟预算 | SRT 晚到丢包 | 帧间隙 | 端到端 p50 | 端到端 p95 |
|---|---|---|---|---|
| 80 ms | 60 | 1 | 229.3 ms | 344.5 ms |
| 200 ms | 90 | 5 | 228.0 ms | 343.6 ms |
| 400 ms | 60 | 2 | 226.5 ms | 340.5 ms |
| 800 ms | 151 | 8 | 228.4 ms | 346.5 ms |

加大缓冲确实改变了可恢复/晚到丢包的分布，但缓冲本身就是延迟预算——**不能把缓存无限
加大后仍称低延迟**：预算越大，被 TSBPD 扣在接收端的画面离现场越远，且本机环回下稳态
投递时刻已贴近预算下界，再加大也换不回更低延迟，只会让突发时一次性晚到丢弃更多
（800ms 组 151 个）。矩阵里 `e2e_ms_p50/p95/max` 与 `srt_late_dropped` 并列给出，
二者是同一笔预算的两面。

## 6. 有界队列与“恢复后不先播旧画面”

- `BoundedPacer` 在编码器与 SRT 网关之间维持**有界字节队列**（默认 600000 字节，
  约 0.4s 码率）。网关管道设为非阻塞：SRT 堵死时编码器继续实时生产，超出上限的最旧
  TS 包在 0x47 同步字节处对齐丢弃，每次丢弃写入 `drops.csv`（原因 `queue_overflow`）。
- 黑窗（`outage`：第 8s 起持续 15s）导致 SRT 会话断裂，监督线程协调重建整个 SRT 会话，
  并**重启编码器开启新的媒体世代（PTS 归零）**，重接前把队列残留以 `session_reconnect`
  原因记为丢弃（本组实测 336 个 TS 包）。因此恢复点直接落在活边，不会先排空几分钟积压。
- **无界对照（`--unbounded`）**：重连时保留编码器与全部积压。同一条 30s 黑窗 profile
  实测对比如下（见 `runs/experiments.json`）：

| 30s outage 对照 | 队列峰值 | 后 1/3 时段解码帧 | 媒体世代 | 重连/重置丢弃 |
|---|---|---|---|---|
| 有界队列（默认） | 15,792 B（≈0.08s 码率） | 137 | 2 | 336 + 84 |
| 无界队列（反例） | 3,082,564 B（≈16s 码率） | 0 | 1（积压世代） | 0（从不丢弃） |

  无界模式黑窗内队列线性膨胀到 3MB 以上，恢复后接收端被旧画面积压拖住、在后 1/3 时段
  一帧新画面都收不到（`frames_in_last_third=0`）；有界模式同位置仍持续出帧 137 张且
  队列始终贴在低位。这是“无限缓存不等于低延迟”的直接证据，且无界模式仅用于该对照实验。

## 7. 连接中断 vs 媒体时间轴中断（两类事件分开）

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

## 8. 网络损伤模型（`impairment.py`，可热更新）

| profile | 含义 |
|---|---|
| healthy | 无损伤 |
| jitter | 120ms 均匀抖动 + 20ms 基础时延 |
| reorder | 8% 包扣留 90ms 后释放（乱序）+ 30ms 抖动 |
| burst_loss | Gilbert-Elliott：平均突发长度 8，坏态占比 12%，另 1% 随机丢包 |
| severe | 突发长度 12 / 坏态 25% + 150ms 抖动 + 5% 乱序 |
| outage | 第 8s 起 15s 黑窗（双向全丢）+ 0.5% 随机丢包 |

中继用单个 UDP socket 学习对端，双向各一个延时堆，黑窗计时锚定在构造时刻且不被
重置，保证一次运行只触发一次。

## 9. 统计导出（每次运行一个 `runs/<时间戳>-<run_id>/`）

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
| `meta.json`、`status.json`、`*.log` | 运行参数、最终状态、各 ffmpeg 原始日志 |

汇总矩阵写在 `runs/experiments.json` / `experiments.csv`。

## 10. 资源范围（在 1280×720@25 / 1.5Mbps+96kbps 下声明）

来自 `resources.csv` 的实测峰值（10 核 aarch64 容器，单机环回；取全部 10 组 30s 对照的
各进程上包络，健康组 encoder 约 78% CPU / 132MB RSS）：

| 进程 | CPU 峰值 | RSS 峰值 |
|---|---|---|
| encoder（x264 实时编码 + libass 烧录） | ≈ 78% | ≈ 143 MB |
| srt-gateway（仅 `-c copy` 转发） | ≈ 10% | ≈ 34 MB |
| receiver（真实解码 + 快照 + showinfo） | ≈ 28% | ≈ 93 MB |

Python 编排与损伤中继本身 CPU 占用很低（中继为单线程 UDP 转发）。整体在约 8 GB 内存的
容器内稳定运行；无界队列是唯一会让内存随中断时长线性增长的模式，这也是它只作为
反面对照的原因之一。

## 11. 目录与源码

```
src/ob/
  config.py       参数、端口、损伤 profile
  pipeline.py     四进程编排、SRT 监督、连接/时间轴两类中断、活边恢复
  pacer.py        有界队列、非阻塞写、对齐丢弃、管道重接
  impairment.py   双向 UDP 损伤中继（突发/抖动/乱序/黑窗）
  frameparse.py   showinfo/ashowinfo 解析、PTS 间隙、epoch 延迟配对
  srtparse.py     该 libsrt 实际输出的 SRT 告警/生命周期解析
  subtitles.py    烧录帧号/时间码/run_id 的 ASS 生成
  resources.py    /proc CPU/RSS 采样
  stats.py        线程安全统计与 CSV/JSON 导出
  webapp.py       标准库 http.server 控制页与 API
bin/              ob.sh 启动器、单次会话、实验矩阵、Web 服务
```
