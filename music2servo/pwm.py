"""PWM 层：把控制帧率的行程信号变成 50Hz 舵机帧序列。

控制帧率（20~30Hz）低于舵机刷新率（50Hz），所以这里做的是「插值上采样」：
用 np.interp 在控制帧之间线性过渡，保证舵机每 20ms 都收到一个明确的目标脉宽，
不会因为控制帧稀疏而在两帧之间保持不动再突跳。

标定按中心脉宽分段线性映射，允许 pulse_center_us 不落在 min/max 正中
（真实舵机的中位往往不是 1500µs）。脉宽最后再钳制一次到 [min, max]，
即使上游给到 ±1 之外也不会把舵机顶到机械限位上堵转。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import ServoConfig
from .shaping import ShapedTrack


@dataclass
class PwmTrack:
    """50Hz PWM 帧序列。"""

    times_s: np.ndarray
    pulse_us: np.ndarray
    angle_deg: np.ndarray
    values: np.ndarray
    """插值后的归一化行程，[-1,1]，便于与上游对齐核对。"""

    period_us: float

    @property
    def n_frames(self) -> int:
        return int(self.pulse_us.shape[0])

    @property
    def duration_s(self) -> float:
        return float(self.times_s[-1]) if self.times_s.size else 0.0


def frame_count(duration_s: float, pwm_hz: float) -> int:
    """覆盖 duration_s 需要多少个 PWM 帧（含起点第 0 帧）。

    统一在这里算，避免调用方各自做除法/乘法时因浮点误差差出一帧。
    """
    if duration_s <= 0.0:
        return 1
    return int(np.floor(duration_s * pwm_hz + 1e-6)) + 1


def render(shaped: ShapedTrack, cfg: ServoConfig | None = None) -> PwmTrack:
    """控制帧 → 50Hz PWM 帧。"""
    cfg = cfg or ServoConfig()
    period_s = 1.0 / cfg.pwm_hz

    if shaped.values.size == 0:
        empty = np.zeros(0)
        return PwmTrack(empty, empty, empty, empty, period_s * 1e6)

    count = frame_count(shaped.duration_s, cfg.pwm_hz)
    times_s = np.arange(count, dtype=np.float64) * period_s
    values = np.clip(
        np.interp(times_s, shaped.times_s, shaped.values), -1.0, 1.0
    )

    angle_deg = values * cfg.angle_max_deg
    upward = values * (cfg.pulse_max_us - cfg.pulse_center_us)
    downward = values * (cfg.pulse_center_us - cfg.pulse_min_us)
    pulse_us = cfg.pulse_center_us + np.where(values >= 0.0, upward, downward)
    pulse_us = np.clip(pulse_us, cfg.pulse_min_us, cfg.pulse_max_us)

    return PwmTrack(
        times_s=times_s,
        pulse_us=pulse_us,
        angle_deg=angle_deg,
        values=values,
        period_us=period_s * 1e6,
    )


def write_frames_csv(path: str | Path, tracks: dict[str, PwmTrack]) -> Path:
    """把一路或多路 PWM 帧序列写成 CSV，mode 列便于并排对比。"""
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)

    lines = ["mode,frame,time_s,pulse_us,angle_deg,value"]
    for mode, track in tracks.items():
        for index in range(track.n_frames):
            lines.append(
                f"{mode},{index},{track.times_s[index]:.4f},"
                f"{track.pulse_us[index]:.1f},{track.angle_deg[index]:.2f},"
                f"{track.values[index]:.4f}"
            )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination