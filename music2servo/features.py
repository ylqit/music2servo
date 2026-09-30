"""特征分析层：在统一时间栅格上提取帧级特征。

产出五项帧级特征，全部长度一致、时间轴一致：
  rms_db            能量包络（dB）
  spectral_flux     谱通量（新颖度）
  spectral_centroid 谱质心（Hz，音色明暗）
  band_ratio        高频能量占比 [0,1]（低频 vs 高频的配比）
  f0                基频（Hz，0 表示无基频）
前两项服务「高潮截取」，后三项服务「映射层」。

f0 用自实现的 YIN（FFT 加速的自相关 + CMND），不依赖 librosa。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.signal as sps

from .config import AnalysisConfig


@dataclass
class FeatureTrack:
    """帧级特征集合，所有数组长度一致、时间轴一致。

    hop_s = hop_length / sr，第 i 帧对应时间 i * hop_s。
    """

    sr: int
    hop_length: int
    n_fft: int
    """分析窗长。第 i 帧覆盖 [i*hop, i*hop+n_fft)（stft 不做补零居中），
    所以窗内事件的真实时刻最早可能是 i*hop，最晚是 i*hop+n_fft。
    节拍检测靠它把帧号还原成事件时刻，见 mappers.detect_beat_grid。"""

    rms_db: np.ndarray
    """每帧能量，dB。"""

    spectral_flux: np.ndarray
    """每帧谱通量（只累加正向变化），反映“新声音出现”的强度。"""

    spectral_centroid: np.ndarray
    """每帧谱质心（Hz）。越高越“亮”，适合驱动快速摆动。"""

    band_ratio: np.ndarray
    """高频段能量占（低频段 + 高频段）的比例，[0,1]。0 = 全在低频。"""

    f0: np.ndarray
    """每帧基频（Hz）。0 表示该帧无可靠基频（静音或噪声）。"""

    @property
    def n_frames(self) -> int:
        return int(self.rms_db.shape[0])

    @property
    def hop_s(self) -> float:
        return self.hop_length / self.sr

    @property
    def times_s(self) -> np.ndarray:
        """帧时间轴，起点对齐到 0。"""
        return np.arange(self.n_frames, dtype=np.float64) * self.hop_s

    @property
    def duration_s(self) -> float:
        return self.n_frames * self.hop_s


def extract(pcm: np.ndarray, sr: int, cfg: AnalysisConfig | None = None) -> FeatureTrack:
    """对整段 PCM 做一次性帧化与特征提取。"""
    cfg = cfg or AnalysisConfig()
    pcm = np.ascontiguousarray(pcm, dtype=np.float32)
    if pcm.size < cfg.n_fft:
        raise ValueError(
            f"音频太短：需要至少 {cfg.n_fft} 个采样点（约 "
            f"{cfg.n_fft / sr:.3f} s），当前只有 {pcm.size} 个"
        )

    noverlap = cfg.n_fft - cfg.hop_length
    freqs, _, spec = sps.stft(
        pcm,
        fs=sr,
        nperseg=cfg.n_fft,
        noverlap=noverlap,
        boundary=None,
        padded=False,
    )
    n_frames = int(spec.shape[1])
    if n_frames < 2:
        raise ValueError("音频太短，无法产生足够的分析帧")

    magnitude = np.abs(spec)
    return FeatureTrack(
        sr=sr,
        hop_length=cfg.hop_length,
        n_fft=cfg.n_fft,
        rms_db=_frame_rms_db(pcm, cfg.n_fft, cfg.hop_length, n_frames),
        spectral_flux=_spectral_flux(magnitude),
        spectral_centroid=_spectral_centroid(magnitude, freqs),
        band_ratio=_band_ratio(magnitude, freqs, cfg.band_low_hz, cfg.band_high_hz),
        f0=_f0_track(
            pcm,
            sr,
            cfg.n_fft,
            cfg.hop_length,
            n_frames,
            cfg.f0_min_hz,
            cfg.f0_max_hz,
            cfg.f0_voiced_ceiling,
        ),
    )


def _frame_rms_db(
    pcm: np.ndarray, frame_length: int, hop_length: int, n_frames: int
) -> np.ndarray:
    """累积和法计算逐帧 RMS，O(n) 且与 stft 的帧切分完全一致。

    stft 在 boundary=None 时不做补零居中，第 i 帧覆盖
    [i*hop, i*hop + frame_length)，这里沿用同一约定。
    """
    squared = np.cumsum(np.square(pcm.astype(np.float64)))
    squared = np.concatenate(([0.0], squared))

    starts = np.arange(n_frames) * hop_length
    ends = starts + frame_length
    energy = squared[ends] - squared[starts]
    rms = np.sqrt(np.maximum(energy, 0.0) / frame_length)
    return (20.0 * np.log10(np.maximum(rms, 1e-10))).astype(np.float32)


def _spectral_flux(magnitude: np.ndarray) -> np.ndarray:
    """正向谱通量。先做 log1p 压缩，避免单帧爆音主导整条曲线。"""
    compressed = np.log1p(magnitude)
    diff = np.diff(compressed, axis=1)
    flux = np.sum(np.maximum(diff, 0.0), axis=0)
    # 首帧没有前一帧可比，补 0，保持与其它特征等长
    return np.concatenate(([0.0], flux)).astype(np.float32)


def _spectral_centroid(magnitude: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """谱质心 = 幅度加权的频率平均，逐帧计算。"""
    total = np.sum(magnitude, axis=0)
    weighted = magnitude.T @ freqs
    centroid = np.divide(weighted, total, out=np.zeros_like(weighted), where=total > 1e-12)
    return centroid.astype(np.float32)


def _band_ratio(
    magnitude: np.ndarray,
    freqs: np.ndarray,
    low_hz: tuple[float, float],
    high_hz: tuple[float, float],
) -> np.ndarray:
    """高频段能量 / (低频段 + 高频段) 能量，落在 [0,1]。"""
    power = np.square(magnitude)
    low_mask = (freqs >= low_hz[0]) & (freqs < low_hz[1])
    high_mask = (freqs >= high_hz[0]) & (freqs < high_hz[1])
    low = np.sum(power[low_mask], axis=0)
    high = np.sum(power[high_mask], axis=0)
    denominator = low + high
    ratio = np.divide(
        high, denominator, out=np.zeros_like(high), where=denominator > 1e-12
    )
    return ratio.astype(np.float32)


def _f0_track(
    pcm: np.ndarray,
    sr: int,
    frame_length: int,
    hop_length: int,
    n_frames: int,
    f0_min_hz: float,
    f0_max_hz: float,
    voiced_ceiling: float,
) -> np.ndarray:
    """逐帧 YIN 基频估计，返回 Hz（无基频为 0）。"""
    f0 = np.zeros(n_frames, dtype=np.float32)
    tau_min = max(1, int(np.floor(sr / f0_max_hz)))
    tau_max = min(int(np.floor(sr / f0_min_hz)), frame_length // 2)
    if tau_max <= tau_min:
        return f0

    for index in range(n_frames):
        start = index * hop_length
        frame = pcm[start : start + frame_length]
        if frame.size < frame_length:
            break
        f0[index] = _yin_frame(
            frame.astype(np.float64), sr, tau_min, tau_max, voiced_ceiling
        )
    return f0


def _yin_frame(
    frame: np.ndarray,
    sr: int,
    tau_min: int,
    tau_max: int,
    voiced_ceiling: float,
    threshold: float = 0.15,
) -> float:
    """单帧 YIN：自相关求差分函数 → CMND → 阈值取首个谷 → 抛物线插值。

    搜索范围内最小的 CMND 若仍高于 voiced_ceiling，说明这一帧根本没有
    周期性（噪声、气声、纯打击），宁可返回 0 也不要编一个假基频出来。
    """
    n = frame.size
    cumulative = np.concatenate(([0.0], np.cumsum(frame * frame)))

    # 差分函数 d(tau) = sum_j (x[j] - x[j+tau])^2，用一次 FFT 自相关求交叉项
    size = 1
    while size < 2 * n:
        size <<= 1
    spectrum = np.fft.rfft(frame, size)
    autocorr = np.fft.irfft(spectrum * np.conj(spectrum), size)[: tau_max + 1]

    taus = np.arange(tau_max + 1)
    difference = cumulative[n - taus] + (cumulative[n] - cumulative[taus]) - 2.0 * autocorr
    difference = np.maximum(difference, 0.0)

    # CMND：对累计均值归一，避免高 tau 处差分天然偏小
    running = np.cumsum(difference)
    cmnd = np.ones_like(difference)
    denominator = running[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        cmnd[1:] = np.where(
            denominator > 1e-12,
            difference[1:] * np.arange(1, difference.size) / denominator,
            1.0,
        )

    window = cmnd[tau_min : tau_max + 1]
    if window.size == 0:
        return 0.0

    below = np.nonzero(window < threshold)[0]
    if below.size:
        tau = tau_min + int(below[0])
        while tau + 1 <= tau_max and cmnd[tau + 1] < cmnd[tau]:
            tau += 1
    else:
        tau = tau_min + int(np.argmin(window))

    if cmnd[tau] > voiced_ceiling:
        return 0.0

    # 抛物线插值细化谷底位置，提升音高分辨率
    if 0 < tau < tau_max:
        previous, current, following = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
        curvature = previous - 2.0 * current + following
        if abs(curvature) > 1e-12:
            tau = tau + 0.5 * (previous - following) / curvature
    if tau <= 0:
        return 0.0
    return float(sr / tau)