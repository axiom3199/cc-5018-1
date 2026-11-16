# 外场回传陈旧材料处置说明（stale material notice）

本文件说明 `ob/runs/` 下**哪些运行材料已作废、为什么作废、以什么为准**。

## 1. 作废原因

在本次修订之前，SRT 延迟预算以**毫秒数值直接写进 ffmpeg `srt://` URL 的 `latency`
参数**，而该参数单位是**微秒**。因此旧材料里标注为 “80 / 200 / 400 / 800 ms” 的运行，
实际 TSBPD 预算只有 0.08 / 0.2 / 0.4 / 0.8 ms，**这些运行不能用于任何时延结论**。
同时旧文档把 600000 字节有界队列写成 “约 0.4 s”，按声明码率实为约 **2.9 s**。

含以下前缀时间戳的运行目录属于**修订前材料**，其 `srt_latency_ms` 与实际下发预算不符：

```
runs/20261115-*        （全部）
runs/20261116-03*      （20261116-034816-a7adf332、20261116-035047-01899aac）
```

`runs/experiments.json`、`runs/experiments.csv` 同样是修订前矩阵，**不得再引用**。

此外，修订初稿曾把“恢复后不先播旧画面”归因于 600000 字节有界队列上限。复核发现：在本仓
全部 `runs/` 中 `queue_overflow` **从未触发**，真正起作用的是会话断裂时**换代重启编码器、
清空队列**；队列上限只对“网关仍在读但读不动”的情况兜底。该错误归因已在 `README.md` §7
更正，并用 `tests/test_pacer.py`（阻塞管道直驱）单独验证上限与对齐丢弃本身有效。

## 2. 处置方式

- 修订前材料**保留原样**（不删除，便于审计对照），但不再作为任何结论来源；
  在本文件明确列出即视为“已标记作废”。
- 修订后材料统一写在 **`ob/evidence/`**，每条记录带 `run_dir` 指回 `runs/` 中的实际
  运行目录，可逐帧复算：

| 文件 | 内容 | 生成方式 |
|---|---|---|
| `evidence/config_check.json` | 单位/范围/样例/弱网场景生成 | `bin/check_config.py` |
| `evidence/config_startup.json` | 真实启动诊断（进程起、接收器连通、解码出帧） | `bin/check_config.py --scenario ...` |
| `evidence/latency_check.json` | 超预算重传 / 短断网 / 长断网重建的实测恢复与丢失区间 | `bin/check_latency.py` |
| `evidence/full_matrix.json` | 全量对照矩阵（含恢复证据） | `bin/run_experiments.py` |

- `runs/` 下的旧材料会在下次 `run_experiments.py` 覆盖 `runs/experiments.*` 时被新数据
  取代；如需彻底清空可自行删除，但**不要**把旧 `experiments.json` 与新证据混用。

## 3. 复核入口

```bash
cd /workspace/ob
python3 -m unittest discover -s tests        # 契约/恢复/有界队列单元测试
python3 bin/check_config.py                  # 静态契约 + 可执行样例
python3 bin/check_config.py --scenario healthy jitter   # 真实启动诊断
python3 bin/check_latency.py                 # 超预算/短断网/重建实测
```

外场回传的图形入口见 `README.md` 第 1 节（`bin/ob.sh web`），命令行仅作辅助。
