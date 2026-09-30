"""音乐 → 舵机 PWM 核心算法库。

闭环：输入获取 → 解码归一化 → 特征提取 → 高潮段截取 →
映射（四种可切换策略）→ 成形（物理约束）→ 50Hz PWM 帧序列。

各层接口独立、可叠加对比，`run()` 里可以一次跑多种映射模式。
"""

from .config import (
    AnalysisConfig,
    MappingConfig,
    SegmentConfig,
    ServoConfig,
    ShapingConfig,
    SourceConfig,
)
from .features import FeatureTrack, extract
from .mappers import REGISTRY as MAPPERS
from .mappers import map_track, resolve
from .pipeline import MappingResult, PipelineConfig, PipelineResult, build_mapping, run
from .pwm import PwmTrack, render, write_frames_csv
from .segment import Clip, detect, score_curve
from .shaping import ShapedTrack, shape
from .source import AudioLoadError, load_audio
from .viz import plot_tuning_report

__all__ = [
    "AnalysisConfig",
    "MappingConfig",
    "SegmentConfig",
    "ServoConfig",
    "ShapingConfig",
    "SourceConfig",
    "FeatureTrack",
    "extract",
    "MAPPERS",
    "map_track",
    "resolve",
    "MappingResult",
    "PipelineConfig",
    "PipelineResult",
    "build_mapping",
    "run",
    "PwmTrack",
    "render",
    "write_frames_csv",
    "Clip",
    "detect",
    "score_curve",
    "ShapedTrack",
    "shape",
    "AudioLoadError",
    "load_audio",
    "plot_tuning_report",
]