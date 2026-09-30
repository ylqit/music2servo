"""成形层：把所有物理约束集中在这里，顺序固定，不可调换。

分位数归一化 → NaN 填充 → 低通 → 降采样 → 限速 → 死区 → 首尾回中 → 限速（最终钳位）

首步的分位拉满可以用 `rescale=False` 关掉，只给 beat 用（见 `shape`）。

为什么顺序不能乱：
  - 降采样必须排在低通之后。先抽点再滤波，高频会折叠成低频假信号（混叠），
    混进来的假摆动再怎么限速也去不掉。
  - 限速必须排在降采样之后。控制帧间隔变长后单步允许的位移才变小，
    在原始 43Hz 栅格上做限速等于没限。
  - 死区排在限速之后，用软死区（减去宽度而非直接归零）保证仍然连续。
  - 首尾回中排在最后，且回中斜坡长度会按限速反推，避免回中自己大幅超速。

关于末尾那次额外的限速：回中斜坡虽然按限速反推了长度，但当片段开头的信号
本身还在快速上升时，斜坡与信号叠乘仍可能产生超过单步上限的位移。
物理约束是硬约束，不能因为「顺序好看」而破例，所以回中之后再压一次限速，
这一遍只做安全钳位，正常情况下几乎不改变波形。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.signal as sps

from .config import ShapingConfig


@dataclass
class ShapedTrack:
    """成形后的控制帧序列。

    时间轴相对片段起点（0 = 片段开头），单位秒。
    """

    times_s: np.ndarray
    values: np.ndarray
    """[-1, 1] 的行程信号。"""

    @property
    def control_hz(self) -> float:
        if self.times_s.size < 2:
            return 0.0
        return float(1.0 / (self.times_s[1] - self.times_s[0]))

    @property
    def duration_s(self) -> float:
        return float(self.times_s[-1]) if self.times_s.size else 0.0


def shape(
    values: np.ndarray,
    hop_s: float,
    cfg: ShapingConfig | None = None,
    rescale: bool = True,
) -> ShapedTrack:
    """把映射层输出（片段帧栅格、[-1,1]）整形成可驱动舵机的控制帧。

    `rescale=False` 时跳过分位拉满。连续模式需要拉满（映射层可能没铺开），
    但 beat 不能：它的摆幅是**按限速刻意算小的**（见 `mappers._beat_amplitude`），
    再拉满就会让斜率超过限速，被限速器削成一条更小、极值漂到拍间的三角波。
    """
    cfg = cfg or ShapingConfig()
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return ShapedTrack(times_s=np.zeros(0), values=np.zeros(0))

    if rescale:
        values = _percentile_rescale(values, cfg.low_pct, cfg.high_pct)
    values = _fill_nan(values)
    values = _lowpass(values, hop_s, cfg)

    control_times, control_values = _resample(values, hop_s, cfg.control_hz)
    control_values = _rate_limit(control_values, cfg.control_hz, cfg.max_rate_per_s)
    control_values = _deadzone(control_values, cfg.deadzone)
    control_values = _force_center(
        control_values, cfg.control_hz, cfg.center_ms, cfg.max_rate_per_s
    )
    # 回中之后再压一次限速，保证输出严格满足物理约束（见模块文档）
    control_values = _rate_limit(control_values, cfg.control_hz, cfg.max_rate_per_s)
    return ShapedTrack(times_s=control_times, values=np.clip(control_values, -1.0, 1.0))


def _percentile_rescale(values: np.ndarray, low_pct: float, high_pct: float) -> np.ndarray:
    """按分位点把行程拉满，避免实际只用到很小一段角度。"""
    low = float(np.percentile(values, low_pct))
    high = float(np.percentile(values, high_pct))
    if high - low < 1e-9:
        return np.zeros_like(values)
    return np.clip((values - low) / (high - low) * 2.0 - 1.0, -1.0, 1.0)


def _fill_nan(values: np.ndarray) -> np.ndarray:
    """线性插值补 NaN，两端用最近的有限值外推。"""
    finite = np.isfinite(values)
    if finite.all():
        return values
    if not finite.any():
        return np.zeros_like(values)
    index = np.arange(values.size, dtype=np.float64)
    return np.interp(index, index[finite], values[finite])


def _lowpass(values: np.ndarray, hop_s: float, cfg: ShapingConfig) -> np.ndarray:
    """零相位低通。舵机只有 3~5Hz 带宽，高于此的摆动是无效行程。"""
    sample_rate = 1.0 / hop_s
    if cfg.lpf_hz >= 0.45 * sample_rate:
        return values
    b, a = sps.butter(cfg.lpf_order, cfg.lpf_hz / (0.5 * sample_rate), btype="low")
    padlen = 3 * max(len(a), len(b))
    if values.size <= padlen:
        return values
    return sps.filtfilt(b, a, values)


def _resample(
    values: np.ndarray, hop_s: float, control_hz: float
) -> tuple[np.ndarray, np.ndarray]:
    """低通之后才降采样到控制帧率。"""
    duration_s = (values.size - 1) * hop_s
    dt = 1.0 / control_hz
    count = max(2, int(round(duration_s / dt)) + 1)
    control_times = np.arange(count, dtype=np.float64) * dt
    frame_times = np.arange(values.size, dtype=np.float64) * hop_s
    return control_times, np.interp(control_times, frame_times, values)


def _rate_limit(values: np.ndarray, control_hz: float, max_rate_per_s: float) -> np.ndarray:
    """限速：单步位移不超过 max_rate_per_s / control_hz。"""
    if values.size < 2:
        return values
    step = max_rate_per_s / control_hz
    limited = np.empty_like(values)
    limited[0] = values[0]
    for index in range(1, values.size):
        delta = values[index] - limited[index - 1]
        limited[index] = limited[index - 1] + float(np.clip(delta, -step, step))
    return limited


def _deadzone(values: np.ndarray, deadzone: float) -> np.ndarray:
    """软死区：把 |x| < deadzone 的部分压到 0，其余整体平移，保持连续。"""
    if deadzone <= 0.0:
        return values
    magnitude = np.abs(values)
    shrunk = np.maximum(magnitude - deadzone, 0.0)
    return np.sign(values) * np.minimum(shrunk, 1.0)


def _force_center(
    values: np.ndarray, control_hz: float, center_ms: int, max_rate_per_s: float
) -> np.ndarray:
    """首尾回中。斜坡长度取 max(center_ms, 按限速反推的最短时间)，
    否则回中本身会突破限速，硬件上表现为开头/结尾猛甩一下。"""
    count = values.size
    if count < 2:
        return values

    def ramp_samples(anchor: float) -> int:
        needed = int(np.ceil(abs(anchor) / max(max_rate_per_s, 1e-9) * control_hz))
        return max(int(round(center_ms / 1000.0 * control_hz)), needed, 1)

    head = min(ramp_samples(float(values[0])), count)
    tail = min(ramp_samples(float(values[-1])), count - head)

    out = values.copy()
    out[:head] = values[:head] * np.linspace(0.0, 1.0, head)
    if tail > 0:
        out[-tail:] = values[-tail:] * np.linspace(1.0, 0.0, tail)
    return out