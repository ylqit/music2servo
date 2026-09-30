# music2servo 项目设计、需求实现与问题复盘

审查日期：2026-09-29。审查对象：当前 `music2servo` 目录中的代码、README、三份专题文档及两份验证脚本。本文面向项目汇报、需求对齐、后续开发和维护复盘。

当前目录没有 Git 仓库元数据，无法确认提交号、修复时间或责任人。文中的历史问题来自现有文档和代码注释，现状以当前实现及本次检查为准。本次只新增本文并补充 README 入口，没有修改算法、接口、测试脚本或运行环境。

## 1. 项目概览与完成情况

项目已经形成一条完整的软件处理链：读取音乐，选出片段，提取特征，生成单轴运动目标，施加运动约束，输出按时间排列的 PWM 脉宽数据。CLI 提供 CSV 和调参报告图；本机 Web 界面提供参数编辑、曲线对比、片段试听和尾巴预览。

**现阶段应定位为“已有主体实现的离线算法与调参原型”。** 本次隔离算法检查支持默认参数下的选段、节拍映射和数值约束效果；当前默认 Python 缺少 `soundfile`，常规入口无法完整运行，Web 手动截取还有已确认的序列化冲突。项目尚无真实舵机驱动和实机验收证据。

| 汇报维度 | 当前结论 |
|---|---|
| 已有能力 | 本地文件／URL 读取代码、自动／手动选段、四种映射、成形、PWM 数据导出、CLI、Web 调参和预览。 |
| 本次已验证 | Python／JavaScript 语法；隔离导入后的节拍脚本；合成 PCM 的选段与运动数据；若干边界行为。具体范围见第 5 节。 |
| 当前阻塞 | 缺少 `soundfile`；未提供依赖安装清单；README 所述项目 `.venv` 不存在。 |
| 已知缺口 | Web 手动选段响应、映射参数语义、截取约束和部分文档描述存在问题或差异。 |
| 尚未验收 | 正常入口端到端流程、浏览器完整播放、远程音频、真实歌曲适应性、实际硬件运动。 |

### 1.1 产品边界

- 单舵机、左右一轴，离线处理；当前没有实时音频流、多舵机编排或硬件反馈闭环。
- 输出的是控制数据表，程序没有连接 GPIO、串口或其他硬件控制器，也不直接产生电气 PWM 波形。
- 自动截取默认窗口为 20 秒；窗口可以配置，手动区间可以超过 20 秒。README 的“最长 20s”尚未成为全入口硬约束。
- Web 面向本机单用户，默认监听 `127.0.0.1:8000`。临时分析结果不是持久项目档案。
- 600～2400 微秒、50 Hz、±120°等均为当前默认配置。其中角度用于导出和预览，不能视为已测得的真实舵机行程。

### 1.2 阅读与证据约定

需求表中的“已实现”表示代码路径存在；“局部验证”表示只覆盖指定算法或样本；“待验证”表示没有本次运行证据。三者都不自动等于端到端通过或实机通过。

第 5 节的 `V01～V06` 是本次检查记录，`H01～H02` 是历史文档记录。第 7 节的 `F01～F10` 是后续任务。既有 [架构文档](architecture.md)、[算法文档](algorithms.md)、[调参文档](tuning.md) 保留；与代码有冲突的内容集中列在第 6 节。

## 2. 需求与实现对应表

以下需求从现有 README、专题文档、配置和公开入口整理；项目没有独立、已签署的需求基线，因此不把实现推断当作已确认的产品承诺。

| 需求／目标 | 当前行为 | 状态 | 代码依据 | 验证情况 |
|---|---|---|---|---|
| 读取本地文件或 HTTP(S) 音频 | 后端读取、解码、混为单声道，并重采样；Web 另支持上传音频。 | 已实现，运行受阻 | [source.py](../music2servo/source.py)、[server.py](../web/server.py) | V02：缺解码依赖；本次未验证文件格式或网络下载。 |
| 提取可对齐的音乐特征 | RMS、谱通量、谱质心、高频能量比、YIN 基频共用一个帧栅格。 | 已实现，局部验证 | [features.py](../music2servo/features.py) | V03～V05：合成输入可运行；纯音／噪声检查符合预期。 |
| 自动选出有代表性的片段 | 能量与新颖度加权、平滑、滑窗搜索，再吸附到附近谱通量峰。 | 已实现，局部验证 | [segment.py](../music2servo/segment.py) | V04：80 秒合成音频选中约 29.44～49.44 秒；尚非真实歌曲副歌识别验收。 |
| 手动覆盖区间 | 任一起止参数非空就进入手动分支；得分设为 NaN，区间可超过默认窗口。 | 核心算法已实现；Web 存在缺陷 | [segment.py](../music2servo/segment.py)、[report.py](../web/report.py) | V06：35 秒片段可生成；得分无法按当前 Web 策略序列化。 |
| 切换四种映射 | 支持 envelope、brightness、pitch、beat；允许一次计算多个模式。 | 已实现，语义部分待明确 | [mappers.py](../music2servo/mappers.py)、[pipeline.py](../music2servo/pipeline.py) | V03／V04；pitch 居中和 gain 效果见第 6 节。 |
| 控制运动速度、抖动和首尾过渡 | 低通、控制帧重采样、限速、软死区、首尾回中、最终限速。 | 已实现，默认样本局部验证 | [shaping.py](../music2servo/shaping.py) | V04：四模式最大单步约 0.08；不是任意参数或任意输入的证明。 |
| 导出 PWM 控制数据 | 按中心脉宽分段标定、插值和钳制，CSV 按模式分组输出。 | 已实现，数值生成局部验证 | [pwm.py](../music2servo/pwm.py) | V04：四模式各 995 帧、脉宽在配置范围内；本次未执行 CSV 写盘流程。 |
| 生成调参报告 | 三行 PNG 展示整首与选段、映射与成形、PWM。 | 已实现，本次未验证出图 | [viz.py](../music2servo/viz.py) | H01 为历史记录；本次未检查图片和中文字体。 |
| 提供命令行 | 参数解析、单模式或 all、控制台摘要、PNG 和 CSV 输出。 | 已实现，完整入口未通过 | [__main__.py](../music2servo/__main__.py) | V02 的包导入链被缺失依赖阻塞。 |
| 提供 Web 调参和同步预览 | 六组参数、四张图、音频播放器、尾巴预览与下载链接。 | 已实现，本次未做浏览器验收 | [server.py](../web/server.py)、[app.js](../web/static/app.js) | V01 仅验证脚本语法；V06 发现手动选段响应问题。 |
| 降低重复调参耗时 | 上游配置不变时复用特征，只重跑映射、成形和 PWM。 | 已实现，复用边界待补强 | [server.py](../web/server.py) 的 `_reusable`／`_analyze` | H02 是历史耗时；后端没有独立核验音频身份。 |
| 输入与失败反馈 | 部分解码错误、格式后缀、请求体大小和异常有统一提示。 | 部分实现 | [server.py](../web/server.py)、[config.py](../music2servo/config.py) | 数值范围和跨字段约束缺少统一校验；F02／F03 待处理。 |
| 驱动真实硬件并验证效果 | 当前只有参数、控制帧和屏幕预览。 | 未实现／未验收 | [pwm.py](../music2servo/pwm.py)、[app.js](../web/static/app.js) | 没有实际驱动、负载测试或硬件验收记录。 |

## 3. 架构与接口

### 3.1 数据流与职责

```mermaid
flowchart TD
    A["本地文件 / HTTP(S) URL / Web 上传"] --> S["source：单声道 PCM / 统一采样率"]
    S --> F["features：整首 FeatureTrack"]
    F --> G["segment：打分 / 选段"]
    S --> G
    G --> C["Clip：区间与淡入淡出后的 PCM"]
    C --> CF["features：片段 FeatureTrack / 时间归零"]
    CF --> M["mappers：四种策略 / raw"]
    M --> H["shaping：控制帧 / shaped"]
    H --> P["pwm：标定与插值 / PwmTrack"]
    P --> CSV["CSV 控制数据"]
    F --> R["PipelineResult：配置与各层产物"]
    C --> R
    M --> R
    H --> R
    P --> R
    R --> V["CLI 报告图 / Web JSON、图表与试听预览"]
```

编排由 [pipeline.py](../music2servo/pipeline.py) 负责。整首特征服务全局选段；截取并淡入淡出后的 PCM 再提取一次特征，服务片段映射。当前实现两次都提取五项特征，包括 YIN；双级提取是当前设计选择，不意味着所有特征都被选段算法使用。

映射层输出归一化行程；成形层处理运动约束；PWM 层负责标定、插值和脉宽范围。beat 有明确的跨层约定：映射需要成形配置中的限速值来计算摆幅，并跳过成形的分位拉满。因此不能把各模式的区别概括为“下游行为完全相同”。

Web 主要封装同一套后端算法。前端根据后端返回的时间数组查表，不另算特征、节拍或 PWM。音频下载在后端完成，不依赖浏览器直接跨域取音频；目标站点权限、网络和格式支持仍需另行验证。

### 3.2 六组配置

配置定义见 [config.py](../music2servo/config.py)，由 `PipelineConfig` 组合；参数详表可参照 [tuning.md](tuning.md)，使用时注意第 6 节列出的差异。

| 配置 | 职责 | 当前主要默认值／约定 |
|---|---|---|
| `SourceConfig` | 输入、重采样、调试时长 | `sr=22050`；URL 超时 30 秒；`max_duration_s=None`。时长裁剪发生在完整读取、解码和重采样之后。 |
| `AnalysisConfig` | 频谱与基频分析 | `n_fft=2048`、`hop_length=512`；低频段 30～250 Hz、高频段 2000～8000 Hz；f0 搜索 80～1000 Hz，浊音阈值 0.35。 |
| `SegmentConfig` | 选段与手动覆盖 | 默认窗口 20 秒、平滑 1.5 秒、能量／新颖度权重 0.7／0.3、吸附范围 0.6 秒、淡入淡出 0.3 秒。 |
| `MappingConfig` | 音乐特征到运动目标 | 默认 envelope、gain 1、平滑 0.05 秒；分位 2／98；beat 自动搜索 60～200 BPM，最低响度动态范围 3 dB。 |
| `ShapingConfig` | 运动约束 | 分位 2／98、二阶 4 Hz 低通、25 Hz 控制帧、限速 2/s、软死区 0.02、回中过渡 300 ms。 |
| `ServoConfig` | 输出标定与展示角度 | 50 Hz PWM；最小／中心／最大脉宽 600／1500／2400 微秒；归一化 ±1 对应展示角度 ±120°。 |

这些 dataclass 没有完整的范围和组合校验。默认配置可运行的局部证据，不能推广成任意输入参数都合法；前端数字输入也不能替代后端校验。

### 3.3 时间轴、归一化与物理量

| 层 | 时间约定 | 默认值／注意事项 |
|---|---|---|
| 音频 | 样本时间 `j / sr` | 原始音频和截取音频各有自己的时长。 |
| 分析帧 | 第 i 帧覆盖 `[i*hop, i*hop+n_fft)`，时间标签为帧起点 `i*hop/sr` | 栅格约 43.07 Hz、间隔 23.22 ms；STFT 不补齐尾部。 |
| 节拍事件估计 | onset 帧时间再加 `n_fft/(2*sr)` | 默认补偿约 46.44 ms；这是窗内事件时刻估计，不是逐个鼓点的精确标注。 |
| 控制帧 | 片段相对时间，从 0 开始 | 默认 25 Hz；通过低通后的特征信号插值得到。 |
| PWM 帧 | 片段相对时间，从 0 开始 | 默认 50 Hz；依据 `shaped.duration_s` 生成，后端数组是前端查表依据。 |

RMS 的正确累积和索引为：

```text
S[k] = sum(x[j]^2 for j in [0, k))
start = i * hop
energy[i] = S[start + n_fft] - S[start]
rms[i] = sqrt(max(energy[i], 0) / n_fft)
```

归一化行程的 `-1 / 0 / +1` 分别表示两侧配置边界与中心。单步上限为 `max_rate_per_s / control_hz`，默认 `2/25=0.08`。限速为 2/s 时，中心到一侧距离为 1，至少需要 0.5 秒；从 -1 到 +1 距离为 2，至少需要 1 秒。

降采样前低通是抗混叠措施。截止频率应低于目标控制帧率的一半，也须低于分析采样率的一半，并留过渡带；默认 `4 < 25/2` 满足这一基本关系。当前代码没有对目标控制帧率与截止频率做完整组合校验，短序列等情况下还会跳过滤波，不能据此承诺任意配置都不会混叠。

非空 PWM 的帧数由 `frame_count()` 统一计算：`floor(duration_s * pwm_hz + 1e-6) + 1`，包含起点帧；`render()` 对空控制序列直接返回空结果。完整音频时长、分析时间轴和控制时间轴并不相等。本次 20 秒片段产生 858 个分析帧、995 个 PWM 帧，最后一个 PWM 时间为 19.88 秒。尾巴查表在超出末帧后保持末值，后续硬件播放器的尾部行为尚未定义。

### 3.4 核心对象和调用接口

| 对象／接口 | 含义与调用约定 |
|---|---|
| `PipelineConfig` | 持有 source、analysis、segment、mapping、shaping、servo 六组配置。 |
| `FeatureTrack` | 保存采样率、hop、窗长及五项等长特征；`times_s` 从 0 开始。 |
| `Clip` | 截取 PCM、采样率、原曲起止时间、得分和是否自动；手动模式当前得分为 NaN。 |
| `ShapedTrack` | 控制时间数组和归一化行程；`duration_s` 是最后一个时间点。 |
| `PwmTrack` | 时间、脉宽、展示角度、归一化值和周期微秒数。 |
| `MappingResult` | 一个模式的 `mode / raw / shaped / pwm`，是对比的基本单位。 |
| `PipelineResult` | 来源、配置、整首 PCM／特征／得分、Clip、片段特征及模式结果列表，供后续报告复用。 |
| `run(source_spec, cfg=None, modes=None)` | 完整软件编排；返回 `PipelineResult`。不指定 modes 时使用配置中的单一模式。 |
| `build_mapping(mode, clip_track, cfg)` | 复用片段特征，重跑映射、成形和 PWM；返回 `MappingResult`。 |

CLI 为 `python -m music2servo <输入> [选项]`，默认 envelope，`--mode all` 计算四种模式。Web 默认计算四种模式；部分 beat 参数通过 Python 配置和 Web 提供，没有对应 CLI 开关。

CSV 固定表头为 `mode,frame,time_s,pulse_us,angle_deg,value`。其中 frame 在每个模式内从 0 编号，time_s 相对片段起点，pulse_us 单位为微秒，value 为归一化行程，angle_deg 为 value 乘以配置角度。多模式数据按模式顺序分组写入，不能将所有行直接当作一条单舵机时序，也不能视为多个舵机通道。

### 3.5 Web 接口与结果生命周期

| 方法与路径 | 输入／输出 | 主要行为 |
|---|---|---|
| `GET /api/config` | 返回六组配置的默认值 | 前端使用后端默认值生成参数表单。 |
| `POST /api/analyze` | JSON：`source`，或 `audio_b64 + audio_name`；可带 `config / modes / base_job` | 上传内容优先；完整分析或复用已有片段特征；返回报告数据与下载地址。 |
| `GET /api/clip.wav/{job_id}` | 截取后的 WAV | 已包含淡入淡出；用于片段试听。 |
| `GET /api/frames.csv/{job_id}` | PWM CSV | 返回该 job 的模式结果。 |

分析响应包括 `job_id / source / sr / duration_s / analysis / clip / wave / score / shaping / servo / modes / beat / audio_url / csv_url`。模式数据带各自时间数组；beat 信息包括周期、推算 BPM、相位和极值网格。整首波形最多压为 2400 格、得分曲线最多抽取 3000 点；片段运动数组用于预览查表。

请求体上限为 64 MiB，包含 base64 和 JSON 开销，并非原始音频可上传 64 MiB。服务明确拦截 `.m4a / .m4b / .aac / .mp4` 后缀；本次没有验证其余格式的实际解码能力。

`JobStore` 默认只保留最近 5 个 job，超出时删除旧目录；正常退出时清理临时根目录，旧下载地址会失效。它不提供持久历史、异步任务状态或跨进程恢复。

缓存判定比较 source 配置、analysis 配置和 segment 配置，不比较音频文件内容、路径或 URL。前端在切换上传文件或编辑来源时清空 `base_job`；服务端仍依赖调用方正确使用该字段。已缓存 URL 内容变化、同路径文件替换和直接 API 调用需另行验证。

解码异常按 422、部分参数错误按 400、分析阶段其他异常按 500 返回。JSON 序列化发生在分析异常捕获之外，手动选段的 NaN 可能使请求无法正常返回 JSON；不能把它记为已通过统一错误处理。

## 4. 已处理问题复盘

下表确认的是“当前存在处理机制及其验证范围”。没有修复前版本可供对比时，不推断修复时间、修改次数或改善比例。

| 编号 | 现象 | 原因 | 当前处理 | 验证证据 | 剩余边界 |
|---|---|---|---|---|---|
| D01 | 特征和运动曲线可能错位，难以对比。 | 各特征若采用不同帧定义，时间标签不一致。 | RMS 与 STFT 采用同样的窗起点；片段重新提取特征并归零时间。 | [features.py](../music2servo/features.py)、[pipeline.py](../music2servo/pipeline.py)；V04。 | 对齐栅格不等于每类特征都能精确定位音乐事件；PCM 尾部覆盖见 3.3。 |
| D02 | 噪声也产生“音高”，使 pitch 无意义摆动。 | 只选 YIN 差分最小值会强行给非周期信号分配基频。 | 增加 CMND 浊音阈值，置信不足返回 0；完全无有效基频时 pitch 返回零。 | [features.py](../music2servo/features.py)、[mappers.py](../music2servo/mappers.py)；V05。 | 只验证纯音、白噪和静音等合成信号；复杂和声／人声仍待评估。 |
| D03 | 离群峰值压缩行程，摆动偏向一侧。 | 极值归一化受异常点影响，偏态分布会移动零位。 | 选段与映射使用分位数；envelope、brightness 的基础特征使用中位数双侧归一。 | [mappers.py](../music2servo/mappers.py)；V04，历史描述见 [tuning.md](tuning.md)。 | 属于部分处理：pitch 用配置对数中点；平滑、混合与成形再归一后不保证最终零位和两侧对称。 |
| D04 | 节拍估计出现倍速／半速、量化漂移或固定提前。 | 整数帧周期不够精细，谐波候选有歧义，帧起点不等于事件时刻。 | onset 周期扫描、圆周相位评价、最小二乘精修；事件时间加半窗补偿。 | [mappers.py](../music2servo/mappers.py) 的 `detect_beat_grid`；V03。 | 合成固定 BPM 通过；变速、切分、弱拍和搜索范围边界尚未验收。 |
| D05 | 满幅摆动被限速后，极值迟于拍点。 | 一拍内的目标位移超过限速允许值。 | beat 使用三角波，摆幅取 `min(1, rate*swing/2)`，并跳过成形分位拉满。 | [mappers.py](../music2servo/mappers.py)、[pipeline.py](../music2servo/pipeline.py)；V03。 | 滤波、控制帧采样、首尾过渡仍会带来误差；“每拍一个极值”是目标，不是任意素材的零误差保证。 |
| D06 | 持续音、噪声被误识别为节拍并来回摆动。 | 微小谱通量变化也可能呈准周期。 | 自动 beat 先检查 RMS 的 P95－P5 动态范围，再要求至少 4 个 onset；无法检测则输出零。 | [mappers.py](../music2servo/mappers.py)；V03／V05。 | 手动 `beat_swing_s` 优先于自动门限；关闭门限或输入强压缩音乐的效果另测。 |
| D07 | 高频抖动、重采样混叠，或回中引入额外超速。 | 未滤波降采样会折叠频率；回中斜坡乘原信号可能增大相邻差值。 | 先低通再重采样，限速后软死区和回中，最后再限速；PWM 再钳制。 | [shaping.py](../music2servo/shaping.py)、[pwm.py](../music2servo/pwm.py)；V04。 | 默认样本末端误差不超过一步；并非所有短输入都严格回到零，也没有证明真实机构安全。 |
| D08 | 实现、测试和前端对同一时长算出不同帧数。 | 浮点乘除与取整方式不一致。 | `frame_count()` 集中定义帧数；前端沿用后端时间数组。 | [pwm.py](../music2servo/pwm.py)、[app.js](../web/static/app.js)；V04。 | 当前脚本的帧数断言复用同一函数；后续还需独立覆盖边界与硬件调度语义。 |
| D09 | 改动下游参数仍重复提取特征；切换来源可能保留旧结果。 | 没有区分上游依赖与下游计算，或前端保留旧缓存标识。 | 独立 `build_mapping()`；按三组上游配置复用；输入切换时清理前端缓存标识。 | [server.py](../web/server.py)、[app.js](../web/static/app.js) 源码；H02。 | 本次未测性能或浏览器切换；后端音频身份校验仍缺失。 |
| D10 | 动画若使用独立计时器，可能与音频逐渐偏离。 | 两套时钟的暂停、卡顿和调度状态不同。 | 动画使用 `audio.currentTime` 与 `requestAnimationFrame` 查表。 | [app.js](../web/static/app.js) 的 `tick`；源码确认。 | 尚无播放、拖动、暂停和窗口缩放的浏览器实测，不能宣称端到端声画零延迟。 |

图中文字使用“微秒”避免某些字体缺少 µ 字符，见 [viz.py](../music2servo/viz.py) 和 [README](../README.md)。这是历史环境下的处理；当前字体列表偏向 macOS，不能据此判定 Windows 中文出图已经通过。

## 5. 验证记录

### 5.1 当前环境

| 项目 | 本次核对结果 |
|---|---|
| 工作目录 | `D:\soft\AI\company-project\music2servo` |
| 系统／默认终端 | Windows／PowerShell |
| 默认 Python | `D:\soft\conda\condaSoft\anaconda3\python.exe`，3.13.5 |
| 可用依赖 | numpy 2.1.3、scipy 1.15.3、matplotlib 3.10.0 |
| 缺失依赖 | 默认 Python 无 `soundfile`，导入失败为 `ModuleNotFoundError`。 |
| 项目环境资料 | 当前无 `.venv`，未提供 requirements、pyproject 或 environment 依赖清单。 |
| 版本追溯 | 当前目录无 Git 元数据；本文不绑定提交号。 |

本次没有安装依赖、创建项目虚拟环境或修改系统设置。

### 5.2 本次检查结果

| 编号 | 检查与条件 | 结果 | 证据边界 |
|---|---|---|---|
| V01 | 用 `ast.parse` 解析全部 Python 源码；`node --check web/static/app.js`。 | 16 个 Python 文件与 JavaScript 语法通过。 | 不证明依赖、业务逻辑、浏览器行为或运行入口可用。 |
| V02 | 在项目根目录执行 `python -B tools/verify_beat.py` 和 `python -B tools/verify_segment.py`。 | 两个原生脚本均以非零状态退出，错误为缺少 `soundfile`。 | 完整回归未通过；不是算法断言失败。 |
| V03 | 仅隔离包初始化，加载原算法模块并运行原 `verify_beat.py`；默认参数，20 秒鼓点，起拍 0.37 秒。 | 100／120／140 BPM、手动周期 2 秒及持续音退化检查通过。 | 跳过 `music2servo.__init__`；未覆盖解码、完整 pipeline、CLI、HTTP 和硬件。 |
| V04 | 从原 `verify_segment.py` 读取 `make_song` 函数，生成 80 秒 PCM，直接调用特征、选段、四种映射、成形和 PWM。 | 选段与数值范围符合该样本检查；详细数值见下表。 | 没有执行原脚本 main、文件解码、PNG 或 CSV 写盘，不是原端到端脚本通过。 |
| V05 | 1 秒、22050 Hz，220／660 Hz 正弦、固定随机种子的白噪和全静音。 | 两种正弦各 40/40 帧有效；白噪／静音为 0/40；四种信号自动 beat 均无网格、幅值为 0。 | 只代表这些合成信号。 |
| V06 | 使用 V04 数据检查手动选段、严格 JSON、gain 及 onset 关闭参数。 | 复现第 6 节所列的手动 JSON 冲突、35 秒手动片段、gain 抵消和 0 吸附仍移动。 | JSON 是局部序列化复现，未向 HTTP 服务发起请求；这些结果是缺陷／差异证据，不是修复完成。 |

V03 中的均值是成形后极值到最近真实拍点的平均绝对时间偏差。原脚本还要求最大偏差不超过四分之一拍，不能将下表均值当作最大误差。

| 输入 BPM | 检出相邻极值间隔 | 相位偏差 | 极值平均绝对偏差 | 负／正侧摆幅绝对值 |
|---|---|---|---|---|
| 100 | 约 0.6000 秒 | -9.3 ms | 10.0 ms | 0.5197／0.5197 |
| 120 | 约 0.4999 秒 | -7.9 ms | 10.0 ms | 0.4205／0.4195 |
| 140 | 约 0.4286 秒 | -9.3 ms | 10.2 ms | 0.3492／0.3492 |

V04 采用默认配置。合成高能区为 25～55 秒，检测区间为 29.442902～49.442902 秒，得分约 0.759827。四种模式最大相邻控制位移均约 0.08，PWM 均为 995 帧、末帧时间 19.88 秒。

| 模式 | raw 范围 | shaped 范围 | shaped 末值 | PWM 脉宽范围 |
|---|---|---|---|---|
| envelope | [-1.0000, 0.9918] | [-0.4270, 0.3287] | 0 | 1115.72～1795.85 微秒 |
| brightness | [-0.9948, 0.9781] | [-0.4053, 0.4190] | 0 | 1135.26～1877.08 微秒 |
| pitch | [-0.6755, -0.1965] | [0, 1] | 约 0.00834 | 1500～2400 微秒 |
| beat | [-0.4767, 0.4767] | [-0.4205, 0.4163] | 0 | 1121.53～1874.67 微秒 |

pitch 的 shaped 中位数约 0.96886。这说明当前合成样本仍明显偏向正侧；不能把“归一化已改进”写成“四模式都已保证居中”。这也不足以单独决定产品应采用绝对音高映射还是相对音高映射，需要明确需求。

### 5.3 历史记录与未验证范围

| 编号 | 历史来源 | 原文记录 | 本次处理方式 |
|---|---|---|---|
| H01 | [tuning.md：实测基线与调参发现](tuning.md#实测基线与调参发现)，记录日期 2026-09-27 | 选段约 29.44～49.44 秒、三模式各 995 帧；图片约 402 KB、字体缺字警告为 0。 | 核心数值由本次 PCM 检查部分复核；图片、字体和原生端到端结果没有复核。 |
| H02 | [README：Web 可视化界面](../README.md#web-可视化界面) | 分层缓存实测 496 ms → 40 ms。 | 保留为历史性能记录；没有足够的机器、输入、重复次数信息，本次没有复测。 |

本次未测试音频文件格式矩阵、远程 URL、真实歌曲集合、报告图视觉质量、CSV 文件下载、完整 HTTP 流程、浏览器播放／暂停／拖动／缩放、硬件输出时序和机械效果。已有数值结果不能替代这些验收。

### 5.4 复现隔离算法检查

以下命令在项目根目录的 PowerShell 执行，只使用现有 numpy／scipy 和原源码。它在进程内构造包命名空间以绕过缺失解码依赖的初始化路径，不修改源文件，也不代替正常安装和端到端检查。

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
@'
import ast
import json
import runpy
import sys
import types
from pathlib import Path

import numpy as np

pkg = types.ModuleType("music2servo")
pkg.__path__ = [str(Path("music2servo").resolve())]
sys.modules["music2servo"] = pkg

# 原节拍脚本的断言；仅跳过包初始化，算法未替换。
beat_check = runpy.run_path("tools/verify_beat.py")
assert beat_check["main"]() == 0

from music2servo import features, mappers, pwm, segment, shaping
from music2servo.config import MappingConfig, SegmentConfig

# 只取原合成函数，避免执行 soundfile 导入与原脚本的文件写入。
tree = ast.parse(Path("tools/verify_segment.py").read_text(encoding="utf-8"))
node = next(n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "make_song")
fixture = dict(np=np, SR=22050, INTRO_END=25.0, CHORUS_END=55.0, TOTAL=80.0)
exec(compile(ast.Module(body=[node], type_ignores=[]), "<make_song>", "exec"), fixture)
pcm = fixture["make_song"]()
track = features.extract(pcm, 22050)
cfg = SegmentConfig()
score = segment.score_curve(track, cfg)
clip = segment.detect(pcm, 22050, track, score, cfg)
ct = features.extract(clip.pcm, 22050)
assert clip.auto and 23.0 <= clip.start_s <= 37.0
assert abs(clip.duration_s - 20.0) <= 0.05
print("clip", clip.start_s, clip.end_s, clip.score)

for mode in mappers.ORDER:
    raw = mappers.map_track(ct, MappingConfig(mode=mode), 2.0)
    shaped = shaping.shape(raw, ct.hop_s, rescale=mode != "beat")
    frames = pwm.render(shaped)
    assert raw.size == ct.n_frames and np.all(np.isfinite(raw))
    assert np.max(np.abs(raw)) <= 1.0 + 1e-9
    assert np.all(np.isfinite(shaped.values))
    assert np.max(np.abs(shaped.values)) <= 1.0 + 1e-9
    assert abs(shaped.values[0]) <= 1e-9
    assert abs(shaped.values[-1]) <= 0.08 + 1e-9
    assert np.max(np.abs(np.diff(shaped.values))) <= 0.08 + 1e-9
    assert np.all(np.isfinite(frames.pulse_us))
    assert frames.pulse_us.min() >= 600 and frames.pulse_us.max() <= 2400
    assert frames.n_frames == pwm.frame_count(shaped.duration_s, 50.0)
    print(mode, "raw", raw.min(), raw.max(), "shaped",
          shaped.values.min(), shaped.values.max(), "PWM",
          frames.n_frames, frames.duration_s)

# 以下复现的是现状差异，不是期望长期保持的验收行为。
manual = segment.detect(pcm, 22050, track, score,
    SegmentConfig(manual_start_s=5.0, manual_end_s=40.0))
print("manual_duration", manual.duration_s)
try:
    json.dumps({"clip": {"score": float(manual.score)}}, allow_nan=False)
except ValueError as exc:
    print("manual_json_error", exc)

outputs = []
for gain in (1.0, 0.5):
    raw = mappers.map_track(ct, MappingConfig(mode="envelope", gain=gain))
    outputs.append(shaping.shape(raw, ct.hop_s).values)
print("gain_output_max_diff", np.max(np.abs(outputs[0] - outputs[1])))

start, _ = segment._best_window(score, track.hop_s, 20.0, len(pcm) / 22050)
aligned = segment._align_to_onset(track, start, 0.0, 20.0, len(pcm) / 22050)
print("onset_zero_before_after", start, aligned)
'@ | python -B -
```

该复现段调用了两个内部选段函数以定位问题，它们不是新增公共 API；后续内部重构时应同步调整此诊断示例。V05 的纯音／噪声检查是另一次局部检查，不包含在上述命令中。

## 6. 现有文档与实现的差异

本节集中说明冲突，避免汇报沿用过度承诺。既有专题文档本次不改写；其中的历史经验仍可参考，但下列内容应以代码和本次证据为准。

| 差异 | 代码／本次证据 | 当前应采用的描述 | 后续任务 |
|---|---|---|---|
| README 说已有项目虚拟环境和 soundfile。 | V02；本机核对见 5.1。 | 这是历史环境说明，当前环境未就绪。 | F01 |
| README 说片段“最长 20s”。 | 手动 5～40 秒得到 35 秒；自动窗口也可配置。 | 20 秒是默认值；是否硬上限仍待需求确认。 | F06 |
| 三个连续模式“都走中位数归一”。 | `_map_pitch` 使用配置 floor／ceil 的对数中点；本次 pitch 输出仍偏单侧。 | envelope／brightness 基础特征采用样本中位数；pitch 当前是配置音高参考。 | F05／F08 |
| gain 可以直接用于压缩最终行程。 | V06：envelope 的 gain 1.0／0.5 最终 shaped 最大差为 0。 | 连续模式的二次分位拉满可抵消未裁剪的正增益缩放；不能承诺 gain 就是最终幅度旋钮。 | F05 |
| onset 吸附“只向前”，设 0 可以关闭。 | 搜索实际为前后范围；reach 至少为 1；V06 起点从 29.675102 移到 29.698322 秒。 | 当前双向搜索，0 不会真正禁用；beat 的半窗补偿也未用于此选段吸附函数。 | F06／F08 |
| RMS 累积和终点写成 `(i+1)*hop+n_fft`。 | [features.py](../music2servo/features.py) 使用 `i*hop+n_fft`。 | 使用 3.3 的正确公式，窗口长度是 n_fft。 | F08 |
| 控制帧率“必须低于低通截止的奈奎斯特”。 | 默认控制帧率 25 Hz、截止 4 Hz。 | 低通截止应低于目标控制帧率的一半，并考虑过渡带；需增加组合校验。 | F03／F08 |
| 2/s 可在 0.5 秒走完 -1～+1 全程。 | 全程距离为 2，时间下限是距离除以速度。 | 0.5 秒是中心到一侧；两侧之间至少 1 秒。 | F08 |
| 多模式差异“100% 只来自映射”。 | `build_mapping` 对 beat 设置 `rescale=False`，并将限速传入映射。 | 配置共享，但 beat 存在明确的成形例外。 | F08 |
| Web 手动选段可正常作为兜底。 | `segment.detect` 产生 NaN；`build_payload` 保留它；`_send_json` 禁止 NaN。 | 核心选段可运行，Web 响应路径存在局部确认的序列化缺陷。 | F02 |
| 声画“严格对齐”、字体警告已经解决。 | 只有共享音频时钟的代码依据和历史字体记录。 | 具备同步机制；本机浏览器体验与字体仍待验证。 | F07／F09 |

## 7. 后续开发顺序与完成判据

优先级含义：P1 为恢复可复现运行、打通已公开功能；P2 为明确语义、修补边界并补齐验收；P3 为维护性改进。下表是后续任务登记，本次没有实施这些修复，也没有代替产品或硬件负责人作尚未确定的选择。

| 编号／优先级 | 类型与任务 | 完成判据 |
|---|---|---|
| F01／P1 | 运行阻塞：补齐依赖安装说明与可复现环境，处理验证脚本的 `/tmp` 固定路径。 | 在干净 Windows 环境按文档安装；包、CLI、Web 可正常导入；临时目录不依赖预建固定路径；两个原生验证脚本完成运行，记录退出码及报告产物。 |
| F02／P1 | 功能缺陷：修复手动片段得分的 JSON 表达和前端无得分展示。 | 明确“无自动得分”的接口表示，统一前后端；手动起点、终点、双端三种请求均返回合法 JSON；界面不对空得分调用数值格式化，图表、音频和 CSV 可用。 |
| F03／P2 | 参数边界：建立六组配置的范围、类型和组合校验。 | 覆盖非有限数、非正采样率／帧率、非法窗长／hop、分位顺序、低通与目标帧率关系、脉宽 min／center／max 顺序；错误在计算前以可读方式返回；有效默认输出不变。 |
| F04／P2 | 缓存一致性：明确来源身份、失效和 job 生命周期。 | 明确同路径文件替换与 URL 内容变化的复用政策；切换 A／B 音频、错误 base_job、旧 job 被淘汰时不混用音频和运动；上游变化重算，下游变化确实复用。 |
| F05／P2 | 映射需求：确认 pitch 的绝对／相对音高语义，以及 gain 是否控制最终行程。 | 先记录产品预期，再制定偏态音高、无基频、不同 gain、不同模式的验收；如选择改变算法，保留前后曲线与数值对比，明确节拍幅度约束。 |
| F06／P2 | 截取需求：确认 20 秒是否硬上限，修复 onset 关闭语义并明确吸附方向。 | Python／CLI／Web 的自动、手动行为一致且有说明；0 真正不移动起点；覆盖仅起点、仅终点、越界、反向、短片段和尾部；决定吸附后得分是否重算。 |
| F07／P2 | 浏览器行为：检查窗口重绘、来源切换和播放状态。 | 播放中缩放窗口、暂停、拖动进度、重算和换源符合预期；确认 resize 调用 `drawAll()` 时重新设置 `audio.src` 是否重置播放，并完成必要修复。 |
| F08／P2 | 文档一致性：同步既有专题文档、注释和参数帮助。 | 第 6 节冲突逐项消除；公式、默认值、接口名和模式例外与代码一致；历史基线保留日期与范围；不把本文登记的问题写成已经修复。 |
| F09／P2 | 验收缺口：补真实歌曲、文件格式、端到端界面及硬件测试。 | 建立有样本和判据的音乐集合；完成 CLI 报告、CSV、HTTP 与浏览器操作验证。硬件选型和播放器调度方案确定后，另测脉宽／角度标定、时间同步、尾部回中和负载效果。 |
| F10／P3 | 维护性：控制前端职责集中带来的修改成本。 | 当前 `app.js` 约 936 行；以后涉及明显扩展时，按表单、图表、播放或请求状态等真实职责评估拆分。拆分须有行为验证，不创建仅转发调用的包装模块。 |

F05、F06 的产品语义和 F09 的硬件参数仍属待确认需求。当前默认值可以用于软件实验，不能直接成为硬件能力承诺；具体修复应在后续任务中确定，本次文档工作到此为止。

