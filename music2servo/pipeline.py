"""编排层：把各层串起来，只负责传递数据，不含算法细节。

两级特征提取是刻意的：
  - 对整首做一次，只服务「高潮截取」（需要看全局才能判断哪段最像副歌）；
  - 对截出的片段再做一次，服务「映射 → 成形 → PWM」。
    映射必须只看片段，否则特征的分位归一化会被整首的动态范围污染，
    而且片段起点的时间轴也要归零，才能和 PWM 帧对齐。

多模式：`modes` 决定这次跑哪几种映射。四种映射共用同一份片段特征、
同一套 shaping / servo 配置，产物放在 `mappings` 里并排，便于 A/B 对比。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from . import features, mappers, pwm, segment, shaping, source
from .config import (
    AnalysisConfig,
    MappingConfig,
    SegmentConfig,
    ServoConfig,
    ShapingConfig,
    SourceConfig,
)
from .features import FeatureTrack
from .pwm import PwmTrack
from .segment import Clip
from .shaping import ShapedTrack


@dataclass
class PipelineConfig:
    source: SourceConfig = field(default_factory=SourceConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    mapping: MappingConfig = field(default_factory=MappingConfig)
    shaping: ShapingConfig = field(default_factory=ShapingConfig)
    servo: ServoConfig = field(default_factory=ServoConfig)


@dataclass
class MappingResult:
    """单一映射模式的完整链路产物。"""

    mode: str
    raw: np.ndarray
    """映射层输出，片段帧栅格，[-1,1]。"""

    shaped: ShapedTrack
    """成形后的控制帧。"""

    pwm: PwmTrack
    """50Hz PWM 帧序列。"""


@dataclass
class PipelineResult:
    source: str
    cfg: PipelineConfig
    """本次运行所用的配置，随结果一起带走，便于复现与出图。"""

    pcm: np.ndarray
    """整首单声道 PCM。"""

    sr: int
    track: FeatureTrack
    score: np.ndarray
    """与 track 同栅格的平滑打分曲线，供可视化复用，避免重算。"""

    clip: Clip
    clip_track: FeatureTrack
    """片段自身的特征，时间轴已归零。"""

    mappings: list[MappingResult]

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / self.sr

    @property
    def modes(self) -> list[str]:
        return [item.mode for item in self.mappings]


def run(
    source_spec: str,
    cfg: PipelineConfig | None = None,
    modes: list[str] | None = None,
) -> PipelineResult:
    cfg = cfg or PipelineConfig()

    pcm, sr = source.load_audio(source_spec, cfg.source)
    track = features.extract(pcm, sr, cfg.analysis)
    score = segment.score_curve(track, cfg.segment)
    clip = segment.detect(pcm, sr, track, score, cfg.segment)

    clip_track = features.extract(clip.pcm, sr, cfg.analysis)
    selected = list(modes) if modes else [cfg.mapping.mode]
    mappings = [build_mapping(mode, clip_track, cfg) for mode in selected]

    return PipelineResult(
        source=source_spec,
        cfg=cfg,
        pcm=pcm,
        sr=sr,
        track=track,
        score=score,
        clip=clip,
        clip_track=clip_track,
        mappings=mappings,
    )


def build_mapping(mode: str, clip_track: FeatureTrack, cfg: PipelineConfig) -> MappingResult:
    """跑「映射 → 成形 → PWM」三层的单模式链路。

    单独公开出来是为了支持分层缓存：片段的特征（clip_track）只要
    analysis / segment 没变就可以复用，改 mapping / shaping / servo 时
    不必重跑两遍特征提取（含两遍 YIN），只调这个函数即可。
    """
    mappers.resolve(mode)  # 提前报错，避免跑完一半才发现模式名写错
    mapping_cfg = replace(cfg.mapping, mode=mode)

    # beat 要按限速反推可达摆幅（map_track 的 rate_per_s），且摆幅不能被成形层
    # 的分位拉满再放大回来（rescale=False）—— 否则斜率超限、极值会漂到拍间。
    raw = mappers.map_track(clip_track, mapping_cfg, cfg.shaping.max_rate_per_s)
    shaped = shaping.shape(raw, clip_track.hop_s, cfg.shaping, rescale=mode != "beat")
    frames = pwm.render(shaped, cfg.servo)
    return MappingResult(mode=mode, raw=raw, shaped=shaped, pwm=frames)