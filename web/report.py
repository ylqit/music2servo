"""把 PipelineResult 转成前端可消费的 JSON 载荷。

只做三件事：取数组、降采样、序列化。前端不重新实现任何 DSP。

时间轴一律沿用后端算好的 `times_s`，前端只按秒查表：
前端若自己按 `pwm_hz` 重算栅格，会踩到 `pcm.frame_count` 注释里那个浮点陷阱
（同一个时长，除法与乘法能差出一帧）。
"""

from __future__ import annotations

from dataclasses import fields

import numpy as np

from music2servo.mappers import detect_beat_grid
from music2servo.pipeline import PipelineConfig, PipelineResult
from music2servo.segment import normalized_components

WAVE_BINS = 2400
"""整首波形包络的抽点数。长音频按格取峰值，避免 payload 爆掉。"""

CURVE_POINTS = 3000
"""整首打分曲线的抽点数（43.1Hz 栅格下 5 分钟约 1.3 万帧）。"""


def build_payload(result: PipelineResult, job_id: str) -> dict:
    """组装单个 job 的完整前端载荷。"""
    clip = result.clip
    track = result.track
    clip_times = result.clip_track.times_s

    shaping = result.cfg.shaping
    servo = result.cfg.servo

    return {
        "job_id": job_id,
        "source": result.source,
        "sr": result.sr,
        "duration_s": float(result.duration_s),
        "analysis": {
            "grid_hz": 1.0 / track.hop_s,
            "hop_s": track.hop_s,
            "hop_length": track.hop_length,
            "n_frames": track.n_frames,
        },
        "clip": {
            "start_s": float(clip.start_s),
            "end_s": float(clip.end_s),
            "duration_s": float(clip.duration_s),
            "auto": bool(clip.auto),
            "score": float(clip.score),
        },
        "wave": _wave(result.pcm, result.sr),
        "score": _score_curves(result),
        "audio_url": f"/api/clip.wav/{job_id}",
        "csv_url": f"/api/frames.csv/{job_id}",
        "shaping": {
            "control_hz": float(shaping.control_hz),
            "max_rate_per_s": float(shaping.max_rate_per_s),
            "step_limit": float(shaping.max_rate_per_s / shaping.control_hz),
            "lpf_hz": float(shaping.lpf_hz),
            "deadzone": float(shaping.deadzone),
        },
        "servo": {
            "pwm_hz": float(servo.pwm_hz),
            "pulse_min_us": float(servo.pulse_min_us),
            "pulse_max_us": float(servo.pulse_max_us),
            "pulse_center_us": float(servo.pulse_center_us),
            "angle_max_deg": float(servo.angle_max_deg),
        },
        "modes": {
            item.mode: {
                "raw": {"t": _round(clip_times), "v": _round(item.raw)},
                "shaped": {
                    "t": _round(item.shaped.times_s),
                    "v": _round(item.shaped.values),
                },
                "pwm": {
                    "t": _round(item.pwm.times_s),
                    "pulse_us": _round(item.pwm.pulse_us, 2),
                    "angle_deg": _round(item.pwm.angle_deg, 3),
                    "v": _round(item.pwm.values),
                },
            }
            for item in result.mappings
        },
        # 只在真的跑了 beat 模式时才回头做一次检测：它比其它模式贵（要扫
        # 一遍周期），而且网格是后端唯一知道的东西，前端不该自己拟合。
        "beat": _beat_info(result) if any(i.mode == "beat" for i in result.mappings) else None,
    }


def _beat_info(result: PipelineResult) -> dict | None:
    """节拍网格：摆动周期、推算 BPM、片段内所有极值时刻。判不出节拍时为 None。

    `beats_s` 直接给出极值时刻，前端只画不算 —— 所有 DSP 结果都由后端产出，
    网页与 CLI 才不会各算一套。极值时刻即 `相位 + k × 摆动周期`。
    """
    grid = detect_beat_grid(result.clip_track, result.cfg.mapping)
    if grid is None:
        return None
    swing_s, phase_s = grid
    if swing_s <= 0.0:
        return None

    duration_s = max(float(result.clip.duration_s), 0.0)
    index = np.arange(0, int(duration_s / swing_s) + 2, dtype=np.float64)
    beats = phase_s + index * swing_s
    beats = beats[beats <= duration_s + 1e-9]
    return {
        "swing_s": round(swing_s, 4),
        "bpm": round(60.0 / swing_s, 2),
        "phase_s": round(float(phase_s), 4),
        "auto": result.cfg.mapping.beat_swing_s is None,
        "beats_s": _round(beats),
    }


def defaults_payload() -> dict:
    """六组配置的默认值，用来自动生成前端表单。

    默认值只在 `config.py` 里写一次，前端不复制一份，
    避免改了 Python 默认值而页面还显示旧数字。
    """
    cfg = PipelineConfig()
    return {
        name: _dataclass_defaults(getattr(cfg, name))
        for name in ("source", "analysis", "segment", "mapping", "shaping", "servo")
    }


def _dataclass_defaults(obj) -> dict:
    out: dict = {}
    for field in fields(obj):
        value = getattr(obj, field.name)
        if isinstance(value, tuple):
            out[field.name] = list(value)
        else:
            out[field.name] = value
    return out


def _round(values, digits: int = 4) -> list:
    return np.round(np.asarray(values, dtype=np.float64), digits).tolist()


def _index(count: int, target: int) -> np.ndarray:
    """等距抽点索引。打分曲线是平滑曲线，等距抽样足够，无需保峰。"""
    if count <= target:
        return np.arange(count)
    return np.linspace(0, count - 1, target).astype(np.int64)


def _wave(pcm: np.ndarray, sr: int, bins: int = WAVE_BINS) -> dict:
    """整首波形包络：每格取峰值，长音频也能快速画出来。"""
    total = int(pcm.shape[0])
    if total == 0:
        return {"t": [0.0], "peak": [0.0]}

    if total <= bins:
        times = np.arange(total, dtype=np.float64) / sr
        return {"t": _round(times), "peak": _round(np.abs(pcm))}

    edges = np.linspace(0, total, bins + 1).astype(np.int64)
    peaks = np.empty(bins, dtype=np.float64)
    for index in range(bins):
        start = int(edges[index])
        stop = max(start + 1, int(edges[index + 1]))
        peaks[index] = float(np.max(np.abs(pcm[start:stop])))
    centers = (edges[:-1] + edges[1:]) / 2.0 / sr
    return {"t": _round(centers), "peak": _round(peaks)}


def _score_curves(result: PipelineResult) -> dict:
    """打分 / 能量 / 新颖度三条曲线，共用同一抽点索引，保证对齐。"""
    track = result.track
    energy, novelty = normalized_components(track, result.cfg.segment)
    index = _index(track.n_frames, CURVE_POINTS)
    return {
        "t": _round(track.times_s[index]),
        "score": _round(result.score[index]),
        "energy": _round(energy[index]),
        "novelty": _round(novelty[index]),
    }
