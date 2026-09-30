# music2servo —— 音乐驱动的单舵机 PWM 生成

把一段音乐（本地文件或 URL）离线分析后，转换成可以直接喂给舵机的 50Hz PWM 帧序列，
并同时产出一张用于调参的三行对比图。

- **输入**：音频文件或 http(s) URL
- **输出**：`pwm_frames.csv`（逐帧脉宽 µs）+ 调参报告图
- **自由度**：单舵机、仅左右 1 轴
- **形态**：离线处理，摘取高潮段，最长 20s；不做实时流式

> 硬件尚未定型，当前只做软件侧。所有输出都是与硬件无关的「控制帧」，
> 换舵机只需要改 `ServoConfig` 的标定参数，算法链路一行不用动。

## 数据流

```text
 音频文件 / URL
      │
      ▼
 ┌─────────────┐   单声道 PCM + 统一重采样
 │  source     │────────────────────────────────────────┐
 └─────────────┘                                        │
      │                                                 │
      ▼  整首                                             │
 ┌─────────────┐   五项帧级特征（同一时间栅格）              │
 │  features   │──► FeatureTrack                        │
 └─────────────┘   rms_db / spectral_flux /             │
      │            spectral_centroid / band_ratio / f0   │
      ▼                                                 │
 ┌─────────────┐   平滑打分曲线                            │
 │  segment    │──► score                               │
 └─────────────┘                                        │
      │                                                 │
      ▼  高潮段 Clip（pcm + 起止时间）                      │
      │◄────────────────────────────────────────────────┘
      ▼  片段再次过 features（时间轴归零）
 ┌─────────────┐   [-1,1] 目标行程
 │  mappers    │──► raw        envelope / brightness / pitch / beat
 └─────────────┘
      ▼
 ┌─────────────┐   [-1,1] 控制帧 @25Hz
 │  shaping    │──► ShapedTrack   低通 / 降采样 / 限速 / 死区 / 回中
 └─────────────┘
      ▼
 ┌─────────────┐   50Hz PWM 帧
 │  pwm        │──► PwmTrack  脉宽 600~2400µs
 └─────────────┘
      │
      ├──► pwm_frames.csv
      └──► tuning_report.png（三行对比图）
```

## 分层速览

| 层 | 模块 | 输入 → 输出 | 核心契约 |
|---|---|---|---|
| 输入 | `source` | 文件/URL → 单声道 PCM | 统一重采样到 `sr` |
| 特征 | `features` | PCM → `FeatureTrack` | 所有特征共享同一 hop 栅格 |
| 截取 | `segment` | PCM + 特征 → `Clip` | 滑窗选「最响且不断有新声音」的一段 |
| 映射 | `mappers` | 片段特征 → `np.ndarray` | **强制归一化到 [-1,1]**，-1/+1 = 左右极限 |
| 成形 | `shaping` | 映射输出 → `ShapedTrack` | 所有物理约束集中在此，顺序固定 |
| PWM | `pwm` | 控制帧 → `PwmTrack` | 50Hz 插值 + 脉宽钳制 |
| 编排 | `pipeline` | 源 → `PipelineResult` | 只传数据，不含算法 |
| CLI | `__main__` | 参数解析 + 报告 | `python -m music2servo <源> [选项]` |

## 文档索引

- [project-review.md](docs/project-review.md) —— 项目汇报、需求实现对照、问题复盘与后续开发事项（2026-09-29 快照，含现有文档与实现的差异）
- [architecture.md](docs/architecture.md) —— 分层职责、接口契约、关键设计决策与理由
- [algorithms.md](docs/algorithms.md) —— 每层的算法与实现细节
- [tuning.md](docs/tuning.md) —— 参数速查、验收判据、实测基线与调参发现

## 环境

`ROOT/.venv` 是 `--system-site-packages` 的虚拟环境，继承 anaconda 的：

| 依赖 | 版本 | 用途 |
|---|---|---|
| numpy | 2.1.3 | 全部数值运算 |
| scipy | 1.15.3 | STFT、低通、滑窗 |
| matplotlib | 3.10.0 | 调参报告出图 |
| soundfile | 0.14.0 | 音频解码 |

不支持 **M4A / AAC**（soundfile 限制），其余 MP3 / FLAC / WAV / OGG 均可。

**刻意不装 librosa**：特征全部用 numpy/scipy 自实现，YIN 基频估计约 40 行。

### 中文字体

出图字体优先序：`Hiragino Sans GB` → `STHeiti` → `Arial Unicode MS` → `Songti SC` → `DejaVu Sans`。

注意 `Hiragino Sans GB` 缺 `µ`（U+00B5，微字符），图中一律写「微秒」而不是 `µs`，
否则 matplotlib 会刷缺字警告。终端输出不受影响，可继续用 `µs`。

## 运行

```bash
.venv/bin/python -m music2servo <文件|URL> [选项]

# 四种映射一次跑完并叠加对比
.venv/bin/python -m music2servo song.mp3 --mode all -o report.png --pwm-csv frames.csv

# 只用基频映射，手动指定区间
.venv/bin/python -m music2servo song.mp3 --mode pitch --start 62 --end 82

# 节拍摆：自动测出节拍网格，左右极值都落在拍点上
.venv/bin/python -m music2servo song.mp3 --mode beat -o report.png
```

### 端到端验证

```bash
.venv/bin/python tools/verify_segment.py
```

合成一段结构已知的 80s 音频（副歌 25–55s），断言高潮落点、映射值域、
限速与回中、脉宽钳制、PWM 帧数等契约。判据清单见 [tuning.md](docs/tuning.md#验收判据)。

### Web 可视化界面

```bash
.venv/bin/python -m web.server          # 默认 http://127.0.0.1:8000/，自动开浏览器
.venv/bin/python -m web.server --port 8765 --no-browser
```

页面上可以改任意参数、四种映射模式叠加对比，并让一条尾巴按 PWM 脉宽实时摆动：

- **四张图**：整首波形包络（含截取区间高亮）、打分曲线（score / energy / novelty）、
  片段映射层与成形层（细线 = raw，粗线 = shaped）、PWM 脉宽（含中位脉宽参考线）。
  选了 `beat` 时，后两张图上会叠出后端给出的节拍点竖线，肉眼就能核对极值有没有踩在拍上。
- **尾巴动效**：以 `<audio>.currentTime` 为时钟，用 `requestAnimationFrame` 查表插值驱动旋转，
  所以画面与声音严格对齐，不会越走越偏。只做屏幕上的仿真，不驱动真实硬件。
- **分层缓存**：只有 `source` / `analysis` / `segment` 变了才重跑两遍特征提取（含两遍 YIN）；
  只改 `mapping` / `shaping` / `servo` 时直接跳到后段。实测 496ms → 40ms。

实现上是一个薄后端加原生 HTML/SVG：`web/server.py` 用标准库 `http.server`（零新增依赖）薄封装
`pipeline.run()`，`web/report.py` 把 `PipelineResult` 降采样成 JSON，前端不重写任何 DSP，
时间轴一律沿用后端算好的 `times_s`。
