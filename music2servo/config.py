"""分组配置对象。

所有可调参数集中在这里，算法模块不硬编码任何魔法数字，
这样后续做 A/B 对比（四种映射模式、不同平滑系数）时只需换配置。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SourceConfig:
    """输入获取与解码。"""

    sr: int = 22050
    """统一重采样目标。单声道，所有下游特征都基于这个采样率。"""

    max_duration_s: float | None = None
    """只加载前 N 秒，None 表示完整加载。用于快速试听与调试。"""

    url_timeout_s: float = 30.0


@dataclass
class AnalysisConfig:
    """帧化与特征提取。

    n_fft / hop_length 定义了整条链路唯一的“时间栅格”，
    所有帧级特征（能量、谱质心、谱通量、基频）必须共享它，
    否则后续无法叠加对比、无法切换映射模式。
    """

    n_fft: int = 2048
    hop_length: int = 512

    band_low_hz: tuple[float, float] = (30.0, 250.0)
    band_high_hz: tuple[float, float] = (2000.0, 8000.0)
    """频段能量比的低频段与高频段边界（Hz，左闭右开）。"""

    f0_min_hz: float = 80.0
    f0_max_hz: float = 1000.0
    """f0 搜索范围。低于 f0_min 视为无基频（静音/噪声）。"""

    f0_voiced_ceiling: float = 0.35
    """YIN 的浊音判决上限。搜索范围内最小的 CMND 高于它，就判定为无基频。

    实测：纯音与三和音的 CMND 约 0.00~0.01，白噪声约 0.88、粉噪约 1.6，
    区分度很大，所以 0.35 很宽松。少了这道闸，噪声会被硬塞一个假基频。
    """

    def hop_s(self, sr: int) -> float:
        """帧间隔（秒）。"""
        return self.hop_length / sr


@dataclass
class MappingConfig:
    """映射层：把帧级特征压成单舵机的目标角度信号。

    契约：任何模式输出都必须是形状 (n,) 且严格落在 [-1, 1]，
    -1 / +1 表示左右行程极限。归一化在映射层内部完成，
    因此切换模式时 shaping / pwm 完全不用改，四种模式才能叠加对比。
    """

    mode: str = "envelope"
    """映射策略名，见 mappers.REGISTRY。"""

    invert: bool = False
    """翻转方向。硬件装了反的舵机时用，不影响其它层。"""

    gain: float = 1.0
    """映射后线性增益，再裁剪回 [-1, 1]。用于压行程或推满行程。"""

    smooth_s: float = 0.05
    """映射层内部的轻微去毛刺平滑（秒）。仅去掉逐帧尖峰，
    真正的带宽约束在 shaping 层，这里不要做大平滑。"""

    norm_low_pct: float = 2.0
    norm_high_pct: float = 98.0
    """把特征拉成 [-1,1] 时用的分位点，只定两侧展开的范围；0 点固定取**中位数**。

    连续三种模式的静止位必须落在中位，否则中位数偏到哪一侧完全由特征偏态决定，
    尾巴会长期停在那侧（实测旧做法下 pitch 的成形输出是 [+0.00,+1.00]，
    整段停在最右边，只剩轻微抖动）。

    用分位而非 min/max，防止单个爆音或一个极短的高音把整段行程压扁。
    注意这与 shaping 层的分位归一化是两件事：这里决定「特征 → 行程」的形状，
    那里只是把已归一的信号再拉满、为限速做准备。"""

    pitch_floor_hz: float = 80.0
    pitch_ceil_hz: float = 1000.0
    """pitch 模式的音高映射下限/上限（Hz），对数刻度。"""

    beat_swing_s: float | None = None
    """beat 模式的摆动周期（秒）= 相邻两个极值之间的时间。None = 自动检测。

    自动检测取**一拍**：相邻极值隔一拍、左右交替，所以每个节拍都到一次边
    （7 个音的乐句 → 左右左右左右左，奇数拍让下一句自然从另一侧起）。
    摆幅不是满量程，而是按限速反推的可达值 min(1, max_rate_per_s × 摆动周期 / 2) ——
    舵机在一拍内走不完 2.0 的往返，硬撑满量程只会被限速削波、极值漂到拍间
    （听感「慢半拍」）。反推摆幅后三角波斜率不超过限速，极值才严格落在拍点上。

    想让摆幅更大就把这个值手动填成两拍（半速摆动，摆幅可翻倍），
    或调高 max_rate_per_s（前提是机械与电源扛得住）。"""

    beat_bpm_range: tuple[float, float] = (60.0, 200.0)
    """自动检测的节拍 BPM 搜索范围（左闭右闭）。"""

    beat_threshold: float = 1.5
    """onset 峰值判决系数：谱通量需高于局部中位数 × 该值才计入。
    调低会更敏感（细碎的鼓点也算一拍），调高只保留最重的鼓点。"""

    beat_min_dynamics_db: float = 3.0
    """「有没有节拍」的最低响度动态范围（dB，RMS 的 95 与 5 分位之差）。

    持续音、长音垫、白噪的谱通量只是浮点噪声，却在帧栅格下表现为准周期，
    逐帧判据拦不住；响度动态范围是分得开的量 —— 实测真实歌曲 6.6~21.9dB，
    而持续音 0.03dB、稳态和弦 0.35dB、颤音 0.68dB、白噪 0.44dB。
    低于此值判定为「没有节拍」，beat 模式输出静止居中。
    调成 0 可关闭这道闸。"""


@dataclass
class ShapingConfig:
    """成形层：所有物理约束集中在这里，顺序不可调换。

    分位数归一化 → NaN 填充 → 低通 → 降采样 → 限速 → 死区 → 首尾回中。
    降采样必须排在低通之后，否则会混叠出假信号。
    """

    low_pct: float = 2.0
    high_pct: float = 98.0
    """分位数归一化范围。映射层已归一到 [-1,1]，这里再按分位拉满行程。

    beat 模式会跳过这一步（`shape(..., rescale=False)`）：它的摆幅是按限速
    刻意算小的，再拉满会让斜率超过限速、被限速器削成更小且极值漂到拍间的三角波。"""

    lpf_hz: float = 4.0
    """低通截止频率。舵机带宽只有 3~5Hz，取 4Hz 折中。"""

    lpf_order: int = 2

    control_hz: float = 25.0
    """控制帧率。低通之后才降到这个帧率。"""

    max_rate_per_s: float = 2.0
    """限速：归一化行程每秒最大变化量。2.0 表示 0.5s 走完全程。"""

    deadzone: float = 0.02
    """软死区宽度，抑制零位附近的高频抖动。"""

    center_ms: int = 300
    """首尾回中的过渡时长（毫秒）。"""


@dataclass
class ServoConfig:
    """PWM 层：控制帧 → 50Hz 舵机帧的标定与插值。"""

    pwm_hz: float = 50.0
    pulse_min_us: float = 600.0
    pulse_max_us: float = 2400.0
    pulse_center_us: float = 1500.0
    """脉宽保守取 600~2400µs（不是 500~2500），给机械留余量避免堵转。"""

    angle_max_deg: float = 120.0
    """归一化 ±1 对应的机械角度，即舵机单侧的最大转角（±120° 是舵机极限）。

    用于 CSV 的 angle_deg 列，以及网页尾巴的刻度与旋转幅度；
    不参与脉宽控制 —— 行程的物理边界是 pulse_min/center/max 那段脉宽标定。"""


@dataclass
class SegmentConfig:
    """高潮段截取。"""

    window_s: float = 20.0
    """目标片段长度上限（秒）。整首不足这个长度时取整首。"""

    smooth_s: float = 1.5
    """打分曲线平滑窗口（秒）。太小会切到瞬态噪声上，太大会丢失段落边界。"""

    energy_weight: float = 0.7
    novelty_weight: float = 0.3

    norm_low_pct: float = 5.0
    norm_high_pct: float = 95.0
    """打分归一化分位点。用分位而非 min/max，防止单个爆音把整首曲线压扁。"""

    onset_align_s: float = 0.6
    """起点向前吸附到 onset 的搜索范围（秒），避免从乐句中间切入。"""

    fade_s: float = 0.3
    """片段首尾淡入淡出（秒），避免第一帧角度跳变。"""

    manual_start_s: float | None = None
    manual_end_s: float | None = None
    """手动覆盖区间。任一非空则跳过自动检测。"""