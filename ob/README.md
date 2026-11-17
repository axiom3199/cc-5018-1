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
- **结果区**：1.5s 轮询 `/api/status`，展示已解码帧数、端到端延迟 p50、有界队列丢弃、
  SRT 晚到丢包、帧间隙、**最近无画面区间与画面恢复时间**、连接状态，并显示演播室
  实时接收画面（烧录帧号/时间码）；
- **失败反馈**：任何内核操作失败（含超出支持范围的参数）都以
  `{"ok": false, "detail": ...}` 回到页面结果区，例如未启动会话时点“热更新”会显示
  `no running session`，延迟预算超出 `[20, 8000] ms` 会显示 `ValueError`。

控制接口（也可直接 curl）：`POST /api/control`，`action ∈ {start,stop,apply,reset}`；
状态 `GET /api/status`；画面 `GET /snapshot?run=<run_dir>`（仅接受 `runs/` 下的运行目录）。

**现场操作步骤（启停发送 / 调丢包 / 核对画面与恢复延迟）**：

1. 点“启动发送”（页面 `profile=healthy` 即可），等结果区显示连接状态 `connected`、
   已解码帧数持续增长，右侧接收画面出现烧录帧号。
2. 需要弱网复现时，在参数区改抖动/乱序/突发丢包/黑窗或直接改 `loss_pct`，点“热更新损伤”，
   `apply` 返回的新损伤字典会显示在结果区；不需要停机。
3. 观察 KPI“最近无画面区间(s)”和“最近画面恢复(s)”，并与右侧画面死锁时间对照——这两项
   由 `ob.evidence` 从本次运行的 `frames.csv`/`events.csv` 实时复算（见 §7 的字段口径）。
4. 需要复现一次断网时，设一个短于预算的黑窗（如 100 ms）验证“预算内吸收”，或设长于会话
   存活期的黑窗验证会话重建，恢复后核对上面两个 KPI 与画面是否回到活边。
5. 点“停止”结束；产物在结果区给出的 `runs/<id>/`，可用 §2 的 `check` 复算。

> 命令行仅辅助外场回传：现场启停发送、调整丢包、核对接收画面与恢复延迟请用上面的
> 图形控制页；`bin/ob.sh session/experiments` 用于无人值守的可重复取证，不是运维入口。

## 2. 另外两种可重复运行方式（取证/回归，非现场入口）

```bash
# 单次会话（无界面，命令行）
bin/ob.sh session healthy 30 200          # profile 时长秒 SRT延迟预算ms
python3 bin/run_session.py --profile outage --duration 40 --srt-latency 400
python3 bin/run_session.py --unbounded --profile outage --duration 40   # 无界对照

# 完整对照矩阵（健康 + 多种损伤 + 延迟预算扫描 + 无界对照）
bin/ob.sh experiments 30                  # 写出 runs/experiments.json 与 .csv

# 配置契约与弱网场景维护校验（不启动进程，仅审计单位/默认值/文档一致性）
bin/ob.sh check --skip-run
# 审计 + 依受支持范围生成弱网场景并验证启动诊断（按场景表各自的时长运行）
bin/ob.sh check

# 旧样例（历史参考，非运维入口）：已改用契约常量与 artifacts 复算，可从任意目录运行
python3 scratch/test_outage.py    # 断网重建 → OUTAGE_RECONNECT_OK
python3 scratch/test_reset.py     # 时间轴重置不重放旧 epoch → RESET_E2E_OK
python3 scratch/test_backlog.py   # 有界 vs 无界积压对照（各约 35s）
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
（日志 `RCV-DROPPED ... delayed for X ms`，解析进 `srt_receiver.csv`）。

**单位约定（契约）**：操作者/命令行/界面输入的延迟预算单位是 **毫秒**；libsrt 的
`srt://...?latency=` 参数单位是 **微秒**。二者之间只在 `ob.contract.srt_latency_us()`
换算一次（`latency_url_value()`），`pipeline.py` 两个 SRT URL 都必须经过它。历史实现
曾把毫秒数直接塞进 `latency=`，于是“200 ms”实际只申请了 200 µs 缓冲——这会让预算形同
虚设，且所有“加大预算”的结论都失真。维护校验器 `bin/ob.sh check` 会静态拦截这种回归。

支持的预算范围：**`[20, 8000] ms`**，默认 **200 ms**。超出范围的输入在启动前被拒绝
（页面显示 `ValueError`，命令行非零退出），不会静默回落到默认值。

| SRT 延迟预算(ms) | URL latency(µs) | 预期端到端 p50 抬升 |
|---|---|---|
| 80  | 80,000  | 最低，但重传窗口最小 |
| 200 | 200,000 | 约 +200 ms 量级的 TSBPD 扣留 |
| 400 | 400,000 | 更大 |
| 800 | 800,000 | 最大 |

预算加大确实扩大了可重传的时间窗，但缓冲本身就是延迟预算——**不能把缓存无限加大后
仍称低延迟**：预算越大，被 TSBPD 扣在接收端的画面离现场越远。`runs/experiments.json`
里 `e2e_ms_p50/p95/max` 与 `srt_late_dropped` 并列给出，二者是同一笔预算的两面。
实测数值以最近一次 `bin/ob.sh experiments` 的产物为准，本文不复制历史快照。

### 5.1 哪些丢包无法在预算内恢复

- **晚到超过预算的包**：SRT 只能在其 TSBPD 预算内重传。若一次丢包后的可用重传时间
  超过预算，包即使最终到达也会被判晚到并丢弃（`RCV-DROPPED`）。这类丢失**不可能**
  在给定预算内恢复；要恢复只能加大预算（换取更大延迟）或降低丢包。
- **超出突发窗口的连续黑窗**：双向全丢的时段长于预算时，没有任何重传能落在预算内；
  本原型对超过 SRT 会话存活期的黑窗按“连接中断”重建会话（见 §7），重建本身还会再
  损失一段画面。**不要**把“有界队列 + SRT 重传”写成“断网可无缝/零丢失”——它做不到。
- **有界队列溢出**：编码器实时生产而 SRT 堵死时，超过 `pacer_bytes` 的最旧 TS 包被
  主动丢弃；这部分是**有意丢弃**（保活边），不是可恢复丢包。
- **端到端丢包率 ≥ 100%/双向全断且持续**：没有任何机制能在预算内恢复，只能等链路恢复
  后从活边重建。

因此运维指引只能写“预算内可恢复的丢包由 SRT 重传 + TSBPD 承担；超预算与时长的丢失
会表现为无画面区间，恢复时间由重建路径决定”，不能承诺有界预算下的无损。

## 6. 有界队列与“恢复后不先播旧画面”

- `BoundedPacer` 在编码器与 SRT 网关之间维持**有界字节队列**（默认 **600000 字节**）。
  该值单位是**字节**，不是时间；在 1.5 Mbps 视频 + 96 kbps 音频下约合 **3.0 s 媒体**
  （`contract.pacer_media_seconds()` 由实际码率换算，不要把它写成 0.4 s 或任何延迟
  预算）。支持范围 **`[188, 67108864]` 字节**。网关管道设为非阻塞：SRT 堵死时编码器继续
  实时生产，超出上限的最旧 TS 包在 0x47 同步字节处对齐丢弃，每次丢弃写入 `drops.csv`
  （原因 `queue_overflow`）。
- 黑窗（`outage`：第 8s 起持续 15s）导致 SRT 会话断裂，监督线程协调重建整个 SRT 会话，
  并**重启编码器开启新的媒体世代（PTS 归零）**，重接前把队列残留以 `session_reconnect`
  原因记为丢弃。因此恢复点直接落在活边，不会先排空几分钟积压。
- **无界对照（`--unbounded`）**：重连时保留编码器与全部积压。同一条 30s 黑窗 profile
  实测对比如下（见 `runs/experiments.json`）：

| 30s outage 对照 | 队列峰值 | 后 1/3 时段解码帧 | 媒体世代 | 重连/重置丢弃 |
|---|---|---|---|---|
| 有界队列（默认） | 14,852 B（<0.1 s 媒体） | 137 | 2 | 335 + 79 |
| 无界队列（反例） | 3,061,208 B（≈15 s 媒体） | 0 | 1（积压世代） | 0（从不丢弃） |

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
- **恢复时间与丢失区间以执行证据为准**：`ob.evidence.recoveries()` 从 `events.csv` 与
  `frames.csv` 复算每次会话丢失的 `session_restore_s`（中断→会话重建完成）、
  `picture_recovery_s`（中断→重建后第一帧解码）、`loss_interval_s`（最后一帧旧画面→
  第一帧恢复画面）；`loss_intervals()` 给出所有“无解码画面”区间（阈值 0.4 s，高于单帧
  步长，避免把解码抖动当成断流）。`/api/status` 与控制页 KPI 实时给出“最近无画面区间”
  与“最近画面恢复”，`runs/experiments.json`（`max_loss_interval_s`、
  `max_picture_recovery_s`、`recovery_count`、`recoveries`）给出可复算的汇总。无界对照
  组的 `picture_recovery_s` 为 `null` 是正确的：会话重建后它仍在排空旧积压，没有新画面，
  这正是不应采用无界队列的证据。以最近一次矩阵为例：15 s
  黑窗（第 8s 起）的有界组实测无画面区间约 **13.0 s**、画面恢复约 **12.9 s**（其中黑窗
  剩余约 10 s 由 `await_blackhole_end` 等待），数值随黑窗结束时刻与重建路径变化，不是
  固定承诺。
- **两个字段含义不同，不能混用**：`duration_s` 是墙钟上的“演播室没有新画面”时长，
  包含“帧晚到但仍到达”的冻结（大预算把包扣在接收端也会造成冻结）；`missing_frames`
  由 **媒体时间轴**（同 epoch 的 PTS 步长）计算，只有发送端根本没产出的帧才计入，
  跨 epoch（重建/重置）时 PTS 归零、无法用差值判断，此时该字段为 `null` 并另给
  `missing_frames_estimate`。接收端 `t_wall` 是 ffmpeg 解复用块（约 0.2 s 媒体）读完时
  打的时间戳，故墙钟时长有约 0.2 s 的量化粒度；对“是否真的丢了媒体”请以
  `missing_frames`/`media_skipped_s` 为准。实测中 `severe` 会出现 0.4–0.5 s 冻结但
  `missing_frames=0`（帧晚到未丢），而重建路径才会出现 `missing_frames` 不可计算的大区间。
- **短断网（`short_outage`，默认预算 200 ms 下 100 ms 黑窗）**：黑窗短于预算/存活期，
  预期**不触发**连接中断，SRT 在预算内吸收；维护校验器的场景表专门覆盖它。
- **超预算重传（`over_budget`，抖动 300 ms + 基础 20 ms、预算取下限 20 ms）**：重传
  窗口远小于抖动，包无法在预算内到达，表现为晚到丢弃与无画面区间；校验器把它作为
  “预算内不可恢复”的正面用例，而不是声称能无损。

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
重置，保证一次运行只触发一次。各字段单位与支持范围见 `ob.contract.IMPAIRMENT_FIELDS`
（pct=% / *_ms=毫秒 / burst_k=包），越界值在启动前被拒绝。

> **keepalive 说明（勿照抄旧文）**：本 libsrt 构建静默忽略 `keepidle/keepinterval`
> 查询参数，ffmpeg 也未暴露 keepalive socket 选项，因此 `config.py` 里的
> `SRT_KEEP_*` 是**无效钩子**，不能用来宣称“15s 黑窗不会让连接超时”。实际行为是：
> 超过会话存活期的黑窗**会**断开 SRT，由 `supervise_srt()` 以连接中断路径重建。

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
| `meta.json`、`status.json`、`*.log` | 运行参数（含 `srt_latency_ms` 与 `srt_latency_us` 双单位）、最终状态、各 ffmpeg 原始日志 |

汇总矩阵写在 `runs/experiments.json` / `experiments.csv`，并新增
`loss_intervals` / `recoveries` / `max_loss_interval_s` / `max_picture_recovery_s` /
`max_loss_missing_frames`（媒体时间轴上真丢的帧；跨 epoch 为 `null`，另给
`max_loss_missing_frames_estimate`）执行证据，供 §7 的恢复结论复算。

## 9.1 配置契约与维护校验

`ob.contract` 是单位/默认值/支持范围的唯一来源，页面、命令行、文档与校验器都从它取：

| 参数 | 单位 | 默认 | 支持范围 |
|---|---|---|---|
| SRT 延迟预算 | ms（URL 传 µs） | 200 ms | [20, 8000] ms |
| 有界队列 | bytes | 600000 B | [188, 67108864] B |
| 损伤 pct 字段 | % | 按 profile | [0, 100] % |
| 损伤 *_ms 字段 | ms | 按 profile | 见 `contract.IMPAIRMENT_FIELDS` |

运行 `bin/ob.sh check --skip-run` 做纯静态审计（单位换算点、CLI/UI 默认值、文档陈述、
过时符号、profile 越界，以及恢复/丢失区间字段的语义回归：墙钟冻结不得被当成丢帧、
跨 epoch 不得编造帧数）；`bin/ob.sh check --duration N` 再按受支持范围生成
`healthy / burst_loss / over_budget / short_outage / outage` 五个弱网场景，真实启动并
从各自产物复算丢失区间与恢复时间，任一场景未启动或与预期不符即非零退出。契约不靠默认值
遮错：越界输入一律在启动前被拒绝，校验器失败会指名具体参数。

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
  contract.py     单位/默认值/支持范围的单一来源（ms→µs 唯一换算点）
  evidence.py     从 events/frames 复算丢失区间与恢复时间
  pipeline.py     四进程编排、SRT 监督、连接/时间轴两类中断、活边恢复
  pacer.py        有界队列、非阻塞写、对齐丢弃、管道重接
  impairment.py   双向 UDP 损伤中继（突发/抖动/乱序/黑窗）
  frameparse.py   showinfo/ashowinfo 解析、PTS 间隙、epoch 延迟配对
  srtparse.py     该 libsrt 实际输出的 SRT 告警/生命周期解析
  subtitles.py    烧录帧号/时间码/run_id 的 ASS 生成
  resources.py    /proc CPU/RSS 采样
  stats.py        线程安全统计与 CSV/JSON 导出
  webapp.py       标准库 http.server 控制页与 API
bin/              ob.sh 启动器、单次会话、实验矩阵、Web 服务、契约校验器
                  (run_session.py / run_experiments.py / serve_web.py /
                   check_contract.py)
```

## 12. 外场回传陈旧材料的处置

- `scratch/test_outage.py`、`scratch/test_reset.py`、`scratch/test_backlog.py` 是早期
  一次性脚本，未纳入 `bin/` 入口，仅保留作历史参考，**不作为**运维或验收依据。为让它们
  在新的契约下仍能跑到底，三者已改用 `ob.contract` 常量、`ob.evidence` 复算，且
  `test_backlog.py` 不再直接读私有 `stats.frames`/`stats.events`（改从 `events.csv`/
  `frames.csv` 复算）；但正式复算仍统一走 `ob.evidence` 与 `bin/ob.sh check`。
- `scratch/web_final_live.jpg` 是某次演示的静态截图，与任何 `runs/<run_id>/snapshots/`
  都不对应（无法复算），已删除（需要回溯可从 git 历史取）。接收画面一律取当前运行的
  `snapshots/live.jpg`，本文与代码都不再引用该文件。
- `runs/` 下的历史运行目录按任务基线保留；其中延迟预算扫描的旧结论（毫秒被当作微秒
  传给 SRT）已被 §5 的单位契约取代，引用时请以 `bin/ob.sh experiments` 的新产物为准。
