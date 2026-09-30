"""命令行入口：python -m music2servo <音频文件或 URL> [选项]"""

from __future__ import annotations

import argparse
import sys

from .config import (
    AnalysisConfig,
    MappingConfig,
    SegmentConfig,
    ServoConfig,
    ShapingConfig,
    SourceConfig,
)
from .mappers import ORDER
from .pipeline import PipelineConfig, run
from .pwm import write_frames_csv
from .source import AudioLoadError
from .viz import plot_tuning_report

_DEFAULT = PipelineConfig()
_MODES = list(ORDER)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music2servo",
        description="音乐 → 单舵机 PWM 帧序列：高潮截取 + 四种可切换映射 + 物理约束。",
    )
    parser.add_argument("input", help="本地音频文件路径，或 http(s) URL")
    parser.add_argument(
        "-o", "--out", default="tuning_report.png", help="调参报告图片路径"
    )
    parser.add_argument(
        "--pwm-csv", default="pwm_frames.csv", help="PWM 帧序列输出路径（CSV）"
    )
    parser.add_argument("--sr", type=int, default=_DEFAULT.source.sr, help="统一采样率")
    parser.add_argument(
        "--max-seconds", type=float, default=None, help="只加载前 N 秒，便于快速调试"
    )

    group = parser.add_argument_group("高潮截取")
    group.add_argument("--window", type=float, default=_DEFAULT.segment.window_s, help="片段长度上限（秒）")
    group.add_argument("--smooth", type=float, default=_DEFAULT.segment.smooth_s, help="打分曲线平滑窗口（秒）")
    group.add_argument("--onset-align", type=float, default=_DEFAULT.segment.onset_align_s, help="起点 onset 吸附搜索范围（秒）")
    group.add_argument("--fade", type=float, default=_DEFAULT.segment.fade_s, help="片段首尾淡入淡出（秒）")
    group.add_argument("--energy-weight", type=float, default=_DEFAULT.segment.energy_weight)
    group.add_argument("--novelty-weight", type=float, default=_DEFAULT.segment.novelty_weight)
    group.add_argument("--start", type=float, default=None, help="手动指定起点（秒），跳过自动检测")
    group.add_argument("--end", type=float, default=None, help="手动指定终点（秒）")

    group = parser.add_argument_group("帧化")
    group.add_argument("--n-fft", type=int, default=_DEFAULT.analysis.n_fft)
    group.add_argument("--hop", type=int, default=_DEFAULT.analysis.hop_length)

    group = parser.add_argument_group("映射层")
    group.add_argument(
        "--mode",
        default=_DEFAULT.mapping.mode,
        choices=_MODES + ["all"],
        help="映射策略；all = 四种全部跑一遍叠加对比",
    )
    group.add_argument("--invert", action="store_true", help="翻转方向")
    group.add_argument("--gain", type=float, default=_DEFAULT.mapping.gain, help="映射输出增益，再裁剪到 [-1,1]")
    group.add_argument("--map-smooth", type=float, default=_DEFAULT.mapping.smooth_s, help="映射层去毛刺平滑（秒）")
    group.add_argument("--norm-low", type=float, default=_DEFAULT.mapping.norm_low_pct, help="映射归一化低分位（%%）")
    group.add_argument("--norm-high", type=float, default=_DEFAULT.mapping.norm_high_pct, help="映射归一化高分位（%%）")
    group.add_argument("--pitch-floor", type=float, default=_DEFAULT.mapping.pitch_floor_hz, help="pitch 模式音高下限（Hz）")
    group.add_argument("--pitch-ceil", type=float, default=_DEFAULT.mapping.pitch_ceil_hz, help="pitch 模式音高上限（Hz）")

    group = parser.add_argument_group("成形层（物理约束）")
    group.add_argument("--lpf", type=float, default=_DEFAULT.shaping.lpf_hz, help="低通截止频率（Hz），舵机带宽 3~5Hz")
    group.add_argument("--lpf-order", type=int, default=_DEFAULT.shaping.lpf_order)
    group.add_argument("--control-hz", type=float, default=_DEFAULT.shaping.control_hz, help="控制帧率（Hz）")
    group.add_argument("--max-rate", type=float, default=_DEFAULT.shaping.max_rate_per_s, help="限速：行程/秒")
    group.add_argument("--deadzone", type=float, default=_DEFAULT.shaping.deadzone, help="软死区宽度")
    group.add_argument("--center-ms", type=int, default=_DEFAULT.shaping.center_ms, help="首尾回中过渡时长（毫秒）")
    group.add_argument("--shape-low", type=float, default=_DEFAULT.shaping.low_pct, help="成形分位归一化低分位（%%）")
    group.add_argument("--shape-high", type=float, default=_DEFAULT.shaping.high_pct, help="成形分位归一化高分位（%%）")

    group = parser.add_argument_group("PWM 层")
    group.add_argument("--pwm-hz", type=float, default=_DEFAULT.servo.pwm_hz)
    group.add_argument("--pulse-min", type=float, default=_DEFAULT.servo.pulse_min_us, help="脉宽下限（µs）")
    group.add_argument("--pulse-max", type=float, default=_DEFAULT.servo.pulse_max_us, help="脉宽上限（µs）")
    group.add_argument("--pulse-center", type=float, default=_DEFAULT.servo.pulse_center_us, help="中位脉宽（µs）")
    group.add_argument("--angle-max", type=float, default=_DEFAULT.servo.angle_max_deg, help="归一化 ±1 对应角度（°）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = PipelineConfig(
        source=SourceConfig(sr=args.sr, max_duration_s=args.max_seconds),
        analysis=AnalysisConfig(n_fft=args.n_fft, hop_length=args.hop),
        segment=SegmentConfig(
            window_s=args.window,
            smooth_s=args.smooth,
            onset_align_s=args.onset_align,
            fade_s=args.fade,
            energy_weight=args.energy_weight,
            novelty_weight=args.novelty_weight,
            manual_start_s=args.start,
            manual_end_s=args.end,
        ),
        mapping=MappingConfig(
            mode=args.mode if args.mode != "all" else _MODES[0],
            invert=args.invert,
            gain=args.gain,
            smooth_s=args.map_smooth,
            norm_low_pct=args.norm_low,
            norm_high_pct=args.norm_high,
            pitch_floor_hz=args.pitch_floor,
            pitch_ceil_hz=args.pitch_ceil,
        ),
        shaping=ShapingConfig(
            low_pct=args.shape_low,
            high_pct=args.shape_high,
            lpf_hz=args.lpf,
            lpf_order=args.lpf_order,
            control_hz=args.control_hz,
            max_rate_per_s=args.max_rate,
            deadzone=args.deadzone,
            center_ms=args.center_ms,
        ),
        servo=ServoConfig(
            pwm_hz=args.pwm_hz,
            pulse_min_us=args.pulse_min,
            pulse_max_us=args.pulse_max,
            pulse_center_us=args.pulse_center,
            angle_max_deg=args.angle_max,
        ),
    )
    modes = _MODES if args.mode == "all" else [args.mode]

    try:
        result = run(args.input, cfg, modes)
    except AudioLoadError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    _report(result)
    for item in result.mappings:
        _report_mode(item)

    report = plot_tuning_report(result, args.out)
    print(f"调参报告   : {report}")

    csv_path = write_frames_csv(
        args.pwm_csv, {item.mode: item.pwm for item in result.mappings}
    )
    print(f"PWM 帧序列 : {csv_path}")
    return 0


def _report(result) -> None:
    clip = result.clip
    print(f"来源       : {result.source}")
    print(f"总时长     : {result.duration_s:.2f} s    采样率 {result.sr} Hz")
    print(
        f"分析栅格   : {1 / result.track.hop_s:.1f} Hz"
        f"（hop={result.track.hop_length}, {result.track.n_frames} 帧）"
    )
    mode = "自动截取" if clip.auto else "手动指定"
    print(
        f"截取区间   : {clip.start_s:.2f} → {clip.end_s:.2f} s"
        f"（{clip.duration_s:.2f} s，{mode}）"
    )
    if clip.auto:
        print(f"窗口得分   : {clip.score:.4f}")
    print(f"控制帧率   : {result.mappings[0].shaped.control_hz:.1f} Hz（成形后）")


def _report_mode(item) -> None:
    shaped = item.shaped.values
    frames = item.pwm
    print(
        f"[{item.mode:<10}] 映射范围 [{item.raw.min():+.3f}, {item.raw.max():+.3f}]"
        f" | 控制帧 {shaped.size}"
        f" | 脉宽 {frames.pulse_us.min():.0f}~{frames.pulse_us.max():.0f}µs"
        f" | PWM 帧 {frames.n_frames}"
    )


if __name__ == "__main__":
    raise SystemExit(main())