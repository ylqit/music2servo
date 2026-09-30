"""调参可视化。

这张图是本项目唯一的“验收依据”：高潮截取切得对不对、打分曲线被哪个特征主导、
四种映射策略差在哪里、成形与限速有没有起作用、脉宽有没有顶到钳位边界，
都必须能一眼看出来，否则参数只能盲调。

三行自上而下：
  1) 整首波形 + 截取区间，并在同一格里叠打分曲线（右轴），保住截取阶段的诊断能力
  2) 片段内映射层输出（细线）+ 成形层输出（粗线），多模式叠加对比
  3) 50Hz PWM 脉宽序列（多模式叠加）
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (必须在 use("Agg") 之后)
import numpy as np  # noqa: E402
from matplotlib import rcParams  # noqa: E402

from .segment import normalized_components  # noqa: E402

if TYPE_CHECKING:
    from .pipeline import PipelineResult

_CJK_FONTS = [
    "Hiragino Sans GB",
    "STHeiti",
    "Arial Unicode MS",
    "Songti SC",
    "DejaVu Sans",
]

_INK = "#3F3F46"
_MUTED = "#A1A1AA"
_PRIMARY = "#4B3FE3"
_SECONDARY = "#22A5F7"
_TERTIARY = "#F59E0B"
_QUATERNARY = "#10B981"

_MODE_COLORS = [_PRIMARY, _SECONDARY, _TERTIARY, _QUATERNARY]


def _setup_fonts() -> None:
    rcParams["font.sans-serif"] = _CJK_FONTS
    rcParams["axes.unicode_minus"] = False


def plot_tuning_report(
    result: "PipelineResult",
    out_path: str | Path,
    *,
    dpi: int = 140,
) -> Path:
    """输出三行调参报告，多模式叠加在同一坐标上直接对比。"""
    _setup_fonts()

    clip = result.clip
    track = result.track
    duration_s = result.duration_s

    figure, (wave_ax, map_ax, pwm_ax) = plt.subplots(
        3,
        1,
        figsize=(11, 8.6),
        sharex=False,
        gridspec_kw={"height_ratios": [1.6, 1.4, 1.2], "hspace": 0.34},
    )

    # ---- 第一行：整首波形 + 截取区间，右轴叠打分曲线 ----
    times_s, amplitude = _envelope(result.pcm, bins=2400)
    times_s = times_s * duration_s
    wave_ax.fill_between(times_s, -amplitude, amplitude, color=_INK, linewidth=0)
    _shade_window(wave_ax, clip)
    wave_ax.set_ylabel("振幅")
    wave_ax.set_ylim(-1.05, 1.05)
    wave_ax.set_xlim(0, max(duration_s, 0.1))
    wave_ax.set_xlabel(f"时间 (秒) · 整首 {duration_s:.2f}s")
    wave_ax.set_title(_title(result), loc="left", fontsize=12)

    score_ax = wave_ax.twinx()
    frame_times_s = track.times_s
    energy, novelty = normalized_components(track, result.cfg.segment)
    score_ax.plot(frame_times_s, result.score, color=_PRIMARY, linewidth=1.3, label="打分")
    score_ax.plot(
        frame_times_s, energy, color=_SECONDARY, linewidth=0.8, alpha=0.65, label="能量"
    )
    score_ax.plot(
        frame_times_s, novelty, color=_MUTED, linewidth=0.8, alpha=0.9, label="新颖度"
    )
    score_ax.set_ylim(-0.05, 1.05)
    score_ax.set_ylabel("归一化得分")
    score_ax.legend(loc="upper right", fontsize=8, frameon=False, ncols=3)

    # ---- 第二行：映射层 + 成形层 ----
    clip_times_s = result.clip_track.times_s
    for level in (-1.0, 0.0, 1.0):
        map_ax.axhline(
            level, color=_MUTED, linewidth=0.7, linestyle=":", alpha=0.8, zorder=0
        )
    for index, item in enumerate(result.mappings):
        color = _color(index)
        map_ax.plot(
            clip_times_s,
            item.raw,
            color=color,
            linewidth=0.8,
            alpha=0.45,
            label=f"{item.mode}·映射",
        )
        map_ax.plot(
            item.shaped.times_s,
            item.shaped.values,
            color=color,
            linewidth=1.8,
            label=f"{item.mode}·成形",
        )
    # 上限留出一条空带，图例不压数据；±1 虚点线标出行程极限
    map_ax.set_ylim(-1.45, 1.45)
    map_ax.set_ylabel("归一化行程")
    map_ax.set_title("映射层（细线）与成形层（粗线）", loc="left", fontsize=11)
    map_ax.legend(loc="upper right", fontsize=7, frameon=False, ncols=6)
    map_ax.set_xlim(0, max(clip.duration_s, 0.1))

    # ---- 第三行：PWM 脉宽 ----
    pwm_ax.axhline(
        result.cfg.servo.pulse_center_us,
        color=_MUTED,
        linewidth=0.8,
        linestyle="--",
        alpha=0.8,
    )
    for index, item in enumerate(result.mappings):
        pwm_ax.plot(
            item.pwm.times_s,
            item.pwm.pulse_us,
            color=_color(index),
            linewidth=1.4,
            label=item.mode,
        )
    margin_us = 120.0
    pwm_ax.set_ylim(
        result.cfg.servo.pulse_min_us - margin_us,
        result.cfg.servo.pulse_max_us + margin_us,
    )
    pwm_ax.set_yticks(
        [
            result.cfg.servo.pulse_min_us,
            result.cfg.servo.pulse_center_us,
            result.cfg.servo.pulse_max_us,
        ]
    )
    pwm_ax.set_ylabel("脉宽 (微秒)")
    pwm_ax.set_xlabel(f"片段时间 (秒) · 片段时长 {clip.duration_s:.2f}s")
    pwm_ax.set_title(
        f"PWM 帧序列 · {result.cfg.servo.pwm_hz:.0f}Hz", loc="left", fontsize=11
    )
    pwm_ax.legend(loc="upper right", fontsize=8, frameon=False, ncols=3)
    pwm_ax.set_xlim(0, max(clip.duration_s, 0.1))

    destination = Path(out_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(destination, dpi=dpi, bbox_inches="tight")
    plt.close(figure)
    return destination


def _color(index: int) -> str:
    return _MODE_COLORS[index % len(_MODE_COLORS)]


def _shade_window(axes, clip) -> None:
    axes.axvspan(clip.start_s, clip.end_s, color=_PRIMARY, alpha=0.12, zorder=0)
    axes.axvline(clip.start_s, color=_PRIMARY, linewidth=1.0, alpha=0.85)
    axes.axvline(clip.end_s, color=_PRIMARY, linewidth=1.0, alpha=0.85)


def _title(result: "PipelineResult") -> str:
    clip = result.clip
    name = _display_name(result.source)
    if clip.auto:
        detail = f"自动截取 · 得分 {clip.score:.4f}"
    else:
        detail = "手动指定区间"
    modes = " / ".join(result.modes)
    return (
        f"{name} · 片段 {clip.start_s:.2f}-{clip.end_s:.2f}s"
        f" ({clip.duration_s:.2f}s) · {detail} · 映射 {modes}"
    )


def _display_name(source: str) -> str:
    lowered = source.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return source
    return Path(source).name


def _envelope(pcm: np.ndarray, bins: int = 2400) -> tuple[np.ndarray, np.ndarray]:
    """按包络降采样，长音频也能快速画图。

    返回 (归一化时间 0-1, 每格峰值幅度)。
    """
    total = pcm.shape[0]
    if total == 0:
        return np.zeros(1), np.zeros(1)
    if total <= bins:
        return np.arange(total) / max(1, total - 1), np.abs(pcm)

    edges = np.linspace(0, total, bins + 1).astype(np.int64)
    peaks = np.empty(bins, dtype=np.float32)
    for index in range(bins):
        start = edges[index]
        stop = max(start + 1, edges[index + 1])
        peaks[index] = np.max(np.abs(pcm[start:stop]))
    centers = (edges[:-1] + edges[1:]) / 2.0 / total
    return centers, peaks