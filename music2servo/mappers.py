"""映射层：把帧级特征压成单舵机的目标角度信号。

契约（不可违反，否则四种模式无法叠加对比）：
  Mapper(feat, cfg, rate_per_s) -> np.ndarray，形状 (n,)，值严格落在 [-1, 1]
  -1 / +1 = 左右行程极限，0 = 中位。

归一化在映射层内部完成。因此切换策略时 shaping / pwm 完全不用改，
`resolve()` 拿到同名函数、`map_track()` 统一做收尾，就能保证
四种模式输出到下游时是同一种东西。

`rate_per_s` 是 shaping 的限速（归一化行程/秒），只有 beat 用得到 ——
它要靠这个上限反推摆幅，见 `_beat_amplitude`。其余三种忽略该参数。

本版四种策略：
  envelope   能量包络 → 响的地方偏一侧，安静处回落，中位附近居中
  brightness 音色明暗（谱质心 + 高频占比）→ 明亮处偏一侧
  pitch      基频高低（对数刻度）→ 高音偏一侧，无基频段保持前值
  beat       节拍摆 → 每个节拍一个极值、左右交替，极值刚好落在拍点上

前三种都是「跟着某个特征走」的连续映射，摆动时机由音乐内容决定，天然对不上
节拍；beat 反过来由网格生成信号，是唯一以「踩点」为目标的策略。

连续三种一律**以中位数为 0 点**做双侧归一化。若像以前那样把 P2~P98 线性拉到
±1，中位数会落在哪一侧完全由特征的偏态决定：实测 pitch 的成形输出是
`[+0.00, +1.00]`，尾巴整段停在最右边，只剩轻微抖动 —— 这就是「摆动全集中在
一侧」。以中位数居中后，静止位与摆动都回到中间，两侧行程才是对称的。
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

from .config import MappingConfig, ShapingConfig
from .features import FeatureTrack

MapperFn = Callable[[FeatureTrack, MappingConfig, float], np.ndarray]
"""映射函数签名。`rate_per_s` = shaping 的限速上限，只有 beat 会用到。"""

_PERIOD_SCAN_STEP_S = 0.001
"""beat 模式扫描节拍周期的步长（秒），对应 0.2% 的相对分辨率。
onset 本身量化在 23ms 的帧栅格上，再细也分不出来。"""


def _map_envelope(track: FeatureTrack, cfg: MappingConfig, rate_per_s: float) -> np.ndarray:
    """能量包络。dB 域做以中位数为 0 点的双侧归一。"""
    return _centered_bipolar(track.rms_db, cfg)


def _map_brightness(track: FeatureTrack, cfg: MappingConfig, rate_per_s: float) -> np.ndarray:
    """音色明暗。谱质心（亮度）与高频能量占比取平均，抵消单一指标的偏科。"""
    centroid = _centered_bipolar(track.spectral_centroid, cfg)
    high_ratio = _centered_bipolar(track.band_ratio, cfg)
    return 0.5 * (centroid + high_ratio)


def _map_pitch(track: FeatureTrack, cfg: MappingConfig, rate_per_s: float) -> np.ndarray:
    """基频高低。对数刻度、以几何中值为 0 点，无基频帧用相邻有效帧插值补齐。"""
    f0 = track.f0.astype(np.float64)
    voiced = f0 > 0.0
    if not voiced.any():
        return np.zeros(track.n_frames, dtype=np.float64)

    filled = np.interp(
        np.arange(track.n_frames, dtype=np.float64),
        np.nonzero(voiced)[0].astype(np.float64),
        f0[voiced],
    )
    floor = max(cfg.pitch_floor_hz, 1.0)
    ceil = max(cfg.pitch_ceil_hz, floor * 1.01)
    # 以 floor / ceil 的几何中值（对数中点）为 0：floor → -1、ceil → +1。
    # 这样中位音高落在中位，而不是像线性拉到 ±1 那样被偏态推到一侧。
    middle = 0.5 * (np.log2(floor) + np.log2(ceil))
    half = max(0.5 * (np.log2(ceil) - np.log2(floor)), 1e-9)
    clipped = np.clip(filled, floor, ceil)
    return np.clip((np.log2(clipped) - middle) / half, -1.0, 1.0)


def _map_beat(track: FeatureTrack, cfg: MappingConfig, rate_per_s: float) -> np.ndarray:
    """节拍摆。每个节拍一个极值、左右交替，极值刚好落在拍点上。

    与另外三种策略的本质区别：它们都是「跟着某个特征走」的连续映射，
    所以摆动的时机由音乐内容决定，天然对不上节拍；这一种反过来，
    先把节拍网格定出来，再生成一条摆到极值刚好落在网格点上的信号。

    摆幅不是满量程：每个节拍都要换一次边，舵机就得在一拍内走完 2.0 的往返，
    而限速把它压到 `rate_per_s × 一拍` 以内。硬撑满量程只会被限速器削成一条
    更小的三角波，且极值漂到两拍正中间（听感上就是「尾巴慢半拍」）。
    所以这里直接按限速反推可达摆幅，让三角波斜率不超过限速 ——
    限速器全程不动作，极值才严格落在拍点上。
    """
    grid = detect_beat_grid(track, cfg)
    if grid is None:
        return np.zeros(track.n_frames, dtype=np.float64)
    swing_s, phase_s = grid
    return beat_wave(track.times_s, swing_s, phase_s, _beat_amplitude(swing_s, rate_per_s))


def _beat_amplitude(swing_s: float, rate_per_s: float) -> float:
    """一拍内可达的最大摆幅。三角波从 +A 到 -A 耗时 swing_s，斜率 2A/swing_s ≤ rate。"""
    if swing_s <= 0.0 or rate_per_s <= 0.0:
        return 1.0
    return float(min(1.0, rate_per_s * swing_s / 2.0))


def detect_beat_grid(track: FeatureTrack, cfg: MappingConfig) -> tuple[float, float] | None:
    """返回 (摆动周期, 首个极值时刻)，单位秒。检测不出节拍时返回 None。

    摆动周期取**一拍**：相邻两个极值隔一拍，左右交替，于是每个节拍都有一个
    极值（7 个音的乐句 → 左右左右左右左，奇数拍让下一句自然从另一侧起）。
    以前取两拍是为了绕开限速、保满行程，代价是每隔一拍才到一次边，
    听感上「节拍卡不准」；现在改由 `_beat_amplitude` 按限速定摆幅来解决。

    onset 帧号要还原成事件时刻再拟合：stft 不做补零居中，第 i 帧覆盖
    [i*hop, i*hop+n_fft)，鼓点落在窗内哪一点是未知的，直接当 i*hop 用会
    系统性早半个窗（2048/22050/2 ≈ 46ms，120BPM 下接近 1/10 拍）。
    取窗中心是残差最小的估计（实测残差从 57ms 降到 11ms）。
    """
    if cfg.beat_swing_s is not None:
        swing = float(cfg.beat_swing_s)
        return (swing, 0.0) if swing > 0 else None

    # 先验一条「有没有节拍」：响度必须有明显起伏。
    # 持续音/长音垫/白噪的谱通量只是浮点噪声，但那噪声在 220Hz 正弦与 23ms 帧栅格
    # 的相互作用下也是准周期的，逐帧判据（峰值/中位、局部极大）全都拦不住 ——
    # 实测持续音能检出 43 个假 onset，并拟合出 0.98 的行程。
    # 响度动态范围是唯一分得开的量：实测真实歌曲 6.6 / 7.0 / 21.9dB，
    # 而持续音 0.03dB、稳态三和弦 0.35dB、颤音 0.68dB、白噪 0.44dB。
    if _loudness_spread_db(track.rms_db) < cfg.beat_min_dynamics_db:
        return None

    strength = _onset_strength(track)
    frames = _pick_onsets(strength, track.hop_s, cfg)
    # 至少 4 个 onset：两个点总能被某个周期的网格拟合上，不构成证据
    if frames.size < 4:
        return None

    window_center_s = 0.5 * track.n_fft / track.sr
    onset_times = frames * track.hop_s + window_center_s
    coarse = _scan_beat_period(onset_times, cfg)
    if coarse is None:
        return None
    period, phase = _fit_grid(onset_times, coarse)

    return period, float(phase % period)


def beat_wave(
    times_s: np.ndarray, swing_s: float, phase_s: float, amplitude: float = 1.0
) -> np.ndarray:
    """在 times_s 上生成「极值落在节拍点、三角波过渡」的对称摆信号。

    用三角波而不是半余弦：三角波的斜率恒为 2·amplitude/swing，把摆幅按
    `_beat_amplitude` 定好后就正好等于限速上限，是限速下唯一能按时到点的形状。
    半余弦在极值处斜率为 0、起步太慢，限速器追不上就一路滞后 ——
    实测半余弦的极值比拍点晚 80ms，三角波则是 0ms。
    三角波被成形层的 4Hz 低通磨圆了拐角，观感并不生硬。

    相邻极值隔 swing_s 且符号交替，所以 swing_s 取一拍时，每个节拍都到一次边。
    """
    times_s = np.asarray(times_s, dtype=np.float64)
    if swing_s <= 0:
        return np.zeros_like(times_s)
    rel = times_s - phase_s
    index = np.floor(rel / swing_s)
    tau = rel - index * swing_s
    target = np.where(np.mod(index, 2.0) == 0.0, 1.0, -1.0)
    return amplitude * target * (1.0 - 2.0 * tau / swing_s)


def _onset_strength(track: FeatureTrack) -> np.ndarray:
    """onset 强度包络：谱通量的非负部分。"""
    return np.maximum(np.asarray(track.spectral_flux, dtype=np.float64), 0.0)


def _loudness_spread_db(rms_db: np.ndarray) -> float:
    """响度动态范围（dB）：RMS 的 95 与 5 分位之差。

    用分位而不是峰谷差：单个爆音或一段静音就能把峰谷差拉到很大，
    而分位差反映的是「整段都在起伏」。
    """
    values = np.asarray(rms_db, dtype=np.float64)
    if values.size == 0:
        return 0.0
    return float(np.percentile(values, 95) - np.percentile(values, 5))


def _pick_onsets(strength: np.ndarray, hop_s: float, cfg: MappingConfig) -> np.ndarray:
    """自适应阈值 + 局部极大 + 最小间隔，返回 onset 帧号（升序）。

    阈值取局部中位数的若干倍而不是全局常数：一段里同时有安静和人声齐鸣时，
    定值阈值要么在安静段全是假阳性，要么在齐鸣段集体漏检。
    """
    if strength.size == 0:
        return np.zeros(0, dtype=np.int64)

    window = max(3, int(round(0.35 / hop_s)) | 1)
    baseline = median_filter(strength, size=window, mode="nearest")
    candidate_mask = strength > np.maximum(baseline * cfg.beat_threshold, 1e-9)

    left = np.empty_like(strength)
    right = np.empty_like(strength)
    left[0], right[-1] = np.inf, np.inf
    left[1:], right[:-1] = strength[:-1], strength[1:]
    candidate_mask &= (strength >= left) & (strength > right)

    candidates = np.nonzero(candidate_mask)[0]
    if candidates.size == 0:
        return candidates

    # 一个 onset 可能连着好几帧都超阈值，按强度贪心保留，彼此至少隔一个最短节拍
    bpm_max = max(float(cfg.beat_bpm_range[1]), 1.0)
    min_gap = max(1, int(round((60.0 / bpm_max) / hop_s)))
    taken = np.zeros(strength.size, dtype=bool)
    kept: list[int] = []
    for index in candidates[np.argsort(strength[candidates])[::-1]]:
        low = max(0, index - min_gap + 1)
        high = min(strength.size, index + min_gap)
        if taken[low:high].any():
            continue
        taken[index] = True
        kept.append(int(index))
    return np.sort(np.asarray(kept, dtype=np.int64))


def _scan_beat_period(onset_times: np.ndarray, cfg: MappingConfig) -> float | None:
    """在 onset 时刻上扫描候选周期，返回最可信的节拍周期（秒）。

    相位用圆周均值求（结果与「第几拍」无关），残差取每个 onset 到最近网格点的
    距离。真周期附近残差陡降，3% 的周期误差会把残差从 8ms 抬到 100ms，甄别度足够。

    合格者里取**周期最大**的，而不是最小的：任何真节拍的整数分之一都同样能
    「踩中」所有 onset（比如 100BPM 的 0.6s，其一半 0.3s 也完全拟合），
    取小的会系统性地把速度翻倍；而整数倍（1.2s）会让落在拍间的那一拍错过网格，
    残差很大，所以「取最大的合格者」正好停在真节拍上。
    """
    low_bpm = max(float(cfg.beat_bpm_range[0]), 1.0)
    high_bpm = max(float(cfg.beat_bpm_range[1]), low_bpm * 1.01)
    slow_s = 60.0 / low_bpm
    fast_s = 60.0 / high_bpm
    if slow_s <= fast_s or onset_times.size < 2:
        return None

    periods = np.arange(fast_s, slow_s, _PERIOD_SCAN_STEP_S)
    if periods.size == 0:
        return None
    residuals = np.array([_grid_residual(onset_times, p) for p in periods])

    best = float(residuals.min())
    tolerance = max(2.0 * best, best + 0.02)
    acceptable = np.nonzero(residuals <= tolerance)[0]
    return float(periods[int(acceptable[-1])])


def _grid_residual(times: np.ndarray, period_s: float) -> float:
    """onset 时刻到「周期为 period_s 的最佳网格」的残差（秒，RMS）。

    网格相位取圆周均值，即把每个 onset 折算到 [0, period) 后求平均方向，
    这样不必知道它是第几拍，也不会因为拍号取整而滑拍。
    """
    phase = _circular_phase(times, period_s)
    offset = (times - phase) / period_s
    return float(np.sqrt(np.mean(np.square(offset - np.round(offset)))) * period_s)


def _circular_phase(times: np.ndarray, period_s: float) -> float:
    """网格最佳相位：各 onset 在周期内的位置取圆周均值。"""
    angle = 2.0 * np.pi * (times / period_s)
    mean_angle = float(np.angle(np.mean(np.exp(1j * angle))))
    return float((mean_angle / (2.0 * np.pi)) * period_s % period_s)


def _fit_grid(onset_times: np.ndarray, period_s: float) -> tuple[float, float]:
    """把规则网格最小二乘拟合到 onset 时刻上，返回 (周期, 首个节拍时刻)。

    必须做这一步：自相关只能给出整数帧的周期（43Hz 栅格下约 23ms），
    20 秒片段上累积能漂掉将近 1 秒，网格到后半段就完全对不上鼓点了。
    对 (拍号, 时刻) 做线性回归可以直接得到小数周期，把漂移收掉。
    """
    phase = float(onset_times[0])
    period = float(period_s)
    for _ in range(3):
        index = np.round((onset_times - phase) / period)
        if index.size < 2 or float(np.ptp(index)) < 1.0:
            break
        design = np.stack([np.ones_like(index), index], axis=1)
        solution, *_ = np.linalg.lstsq(design, onset_times, rcond=None)
        new_phase, new_period = float(solution[0]), float(solution[1])
        if not np.isfinite(new_period) or new_period <= 1e-3:
            break
        phase, period = new_phase, new_period
    return period, phase


REGISTRY: dict[str, MapperFn] = {
    "envelope": _map_envelope,
    "brightness": _map_brightness,
    "pitch": _map_pitch,
    "beat": _map_beat,
}
"""策略名 → 实现。CLI 的 --mode 直接取这里的键。"""

ORDER: list[str] = ["envelope", "brightness", "pitch", "beat"]
"""策略的展示顺序。--mode all 与对比图都按这个顺序，
保证同一首歌每次跑出来的曲线颜色、图例位置都一样，方便跨次对照。"""


def resolve(mode: str) -> MapperFn:
    try:
        return REGISTRY[mode]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"未知映射模式 {mode!r}，可选：{known}") from None


def map_track(
    track: FeatureTrack,
    cfg: MappingConfig | None = None,
    rate_per_s: float | None = None,
) -> np.ndarray:
    """按 cfg.mode 选策略，再统一收尾，保证输出严格落在 [-1, 1]。

    `rate_per_s` 不传时取 shaping 的默认限速，保证直接调用（如验证脚本）
    与流水线算出的摆幅一致；流水线会把实际的 `cfg.shaping.max_rate_per_s` 传进来。
    """
    cfg = cfg or MappingConfig()
    rate = ShapingConfig().max_rate_per_s if rate_per_s is None else float(rate_per_s)
    raw = np.asarray(resolve(cfg.mode)(track, cfg, rate), dtype=np.float64)

    raw = np.nan_to_num(raw, nan=0.0, posinf=1.0, neginf=-1.0)

    window = max(1, int(round(cfg.smooth_s / track.hop_s)))
    if window > 1:
        raw = uniform_filter1d(raw, size=window, mode="nearest")

    if cfg.invert:
        raw = -raw
    raw = raw * cfg.gain

    return np.clip(raw, -1.0, 1.0)


def _centered_bipolar(values: np.ndarray, cfg: MappingConfig) -> np.ndarray:
    """按分位点做**中位数居中**的双侧归一化，返回 [-1,1]。

    P(low) → -1、中位数 → 0、P(high) → +1，两侧各自线性铺开。
    与旧的「P2~P98 线性拉到 ±1」相比，区别只在中位数落点：旧做法下中位数
    偏到哪一侧完全由特征偏态决定，尾巴就会长期停在那侧；居中后静止位回到中位。
    仍用分位而非 min/max，单个爆音不会压扁整条曲线。
    """
    values = np.asarray(values, dtype=np.float64)
    low = float(np.percentile(values, cfg.norm_low_pct))
    high = float(np.percentile(values, cfg.norm_high_pct))
    middle = float(np.percentile(values, 50.0))
    if high - low < 1e-9:
        return np.zeros_like(values)
    above = np.clip((values - middle) / max(high - middle, 1e-9), 0.0, 1.0)
    below = np.clip((middle - values) / max(middle - low, 1e-9), 0.0, 1.0)
    return above - below