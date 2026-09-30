"""高潮段截取：滑窗搜索最“响且不断有新声音”的一段。

打分 = 归一化能量 * w_e + 归一化谱通量 * w_n，先按秒级平滑再求窗口积分。
平滑这一步不能省：不平滑的话窗口会吸附到单个鼓点上，而不是段落。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import uniform_filter1d

from .config import SegmentConfig
from .features import FeatureTrack


@dataclass
class Clip:
    """截取结果。pcm 已含首尾淡入淡出。"""

    pcm: np.ndarray
    sr: int
    start_s: float
    end_s: float
    score: float
    """窗口平均得分。手动指定区间时为 NaN。"""

    auto: bool
    """False 表示用户手动指定了区间。"""

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def normalized_components(track: FeatureTrack, cfg: SegmentConfig) -> tuple[np.ndarray, np.ndarray]:
    """返回 (归一化能量, 归一化新颖度)，用于诊断打分构成。"""
    energy = _percentile_norm(track.rms_db, cfg.norm_low_pct, cfg.norm_high_pct)
    novelty = _percentile_norm(track.spectral_flux, cfg.norm_low_pct, cfg.norm_high_pct)
    return energy, novelty


def score_curve(track: FeatureTrack, cfg: SegmentConfig) -> np.ndarray:
    """每秒一个值的平滑打分曲线，与 FeatureTrack 帧栅格对齐。"""
    energy, novelty = normalized_components(track, cfg)
    raw = cfg.energy_weight * energy + cfg.novelty_weight * novelty
    window = max(1, int(round(cfg.smooth_s / track.hop_s)))
    return uniform_filter1d(raw.astype(np.float64), size=window, mode="nearest")


def detect(
    pcm: np.ndarray,
    sr: int,
    track: FeatureTrack,
    score: np.ndarray,
    cfg: SegmentConfig | None = None,
) -> Clip:
    """确定片段区间并切出 PCM。"""
    cfg = cfg or SegmentConfig()
    duration_s = len(pcm) / sr

    if cfg.manual_start_s is not None or cfg.manual_end_s is not None:
        end_s = cfg.manual_end_s if cfg.manual_end_s is not None else duration_s
        start_s = (
            cfg.manual_start_s
            if cfg.manual_start_s is not None
            else max(0.0, end_s - cfg.window_s)
        )
        start_s, end_s = _clamp(start_s, end_s, duration_s)
        return _build_clip(pcm, sr, start_s, end_s, float("nan"), cfg, auto=False)

    start_s, best = _best_window(score, track.hop_s, cfg.window_s, duration_s)
    start_s = _align_to_onset(track, start_s, cfg.onset_align_s, cfg.window_s, duration_s)
    end_s = min(start_s + cfg.window_s, duration_s)
    return _build_clip(pcm, sr, start_s, end_s, best, cfg, auto=True)


def _percentile_norm(values: np.ndarray, low_pct: float, high_pct: float) -> np.ndarray:
    low = float(np.percentile(values, low_pct))
    high = float(np.percentile(values, high_pct))
    if high - low < 1e-9:
        return np.zeros_like(values, dtype=np.float64)
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def _best_window(
    score: np.ndarray, hop_s: float, window_s: float, duration_s: float
) -> tuple[float, float]:
    """滑动窗口取积分最大的一段，返回 (起点秒, 窗口平均分)。"""
    n_frames = score.shape[0]
    span = min(max(1, int(round(window_s / hop_s))), n_frames)
    if span >= n_frames:
        return 0.0, float(score.mean()) if n_frames else 0.0

    cumulative = np.concatenate(([0.0], np.cumsum(score)))
    sums = cumulative[span:] - cumulative[:-span]
    index = int(np.argmax(sums))
    best = float(sums[index] / span)

    # 窗口末端不能越界，否则实际切出的片段会短于 window_s
    start_s = min(index * hop_s, max(0.0, duration_s - window_s))
    return max(0.0, start_s), best


def _align_to_onset(
    track: FeatureTrack, start_s: float, search_s: float, window_s: float, duration_s: float
) -> float:
    """把起点吸附到附近最强的谱通量峰值上，使切口落在乐句起始处。"""
    hop_s = track.hop_s
    center = int(round(start_s / hop_s))
    reach = max(1, int(round(search_s / hop_s)))
    low = max(0, center - reach)
    high = min(track.n_frames, center + reach + 1)
    if high <= low:
        return start_s

    peak = low + int(np.argmax(track.spectral_flux[low:high]))
    aligned = peak * hop_s
    aligned = min(aligned, max(0.0, duration_s - window_s))
    return max(0.0, aligned)


def _clamp(start_s: float, end_s: float, duration_s: float) -> tuple[float, float]:
    start_s = min(max(start_s, 0.0), duration_s)
    end_s = min(max(end_s, start_s), duration_s)
    return start_s, end_s


def _build_clip(
    pcm: np.ndarray,
    sr: int,
    start_s: float,
    end_s: float,
    score: float,
    cfg: SegmentConfig,
    auto: bool,
) -> Clip:
    first = max(0, int(round(start_s * sr)))
    last = min(len(pcm), int(round(end_s * sr)))
    segment = np.array(pcm[first:last], dtype=np.float32, copy=True)
    _apply_fade(segment, cfg.fade_s, sr)
    return Clip(
        pcm=segment,
        sr=sr,
        start_s=first / sr,
        end_s=last / sr,
        score=score,
        auto=auto,
    )


def _apply_fade(segment: np.ndarray, fade_s: float, sr: int) -> None:
    """原地线性淡入淡出。片段太短时跳过，避免整段被压没。"""
    n = int(round(fade_s * sr))
    if n <= 0 or segment.size < 2 * n:
        return
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    segment[:n] *= ramp
    segment[-n:] *= ramp[::-1]