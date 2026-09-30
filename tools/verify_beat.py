"""合成带明确节拍的测试音频，端到端验证 beat 映射。

beat 与另外三种映射的本质区别：它的正确性不体现在「跟着特征走」，
而体现在四个可量化的判据上 ——
  1. 检测出的摆动周期 = 一拍（每个节拍一个极值、左右交替）
  2. 左右极值都落在拍点上
  3. 左右行程对称
  4. 摆幅 = 限速给出的物理上限（min(1, 限速 × 一拍 / 2)）

这几个判据都极易被隐蔽的量化误差破坏：hops=23ms 的栅格下 0.5s 是 21.53 帧，
整数 lag 的自相关量不出小数周期，鼓点单帧尖峰又会让自相关锁到倍周期上；
拟合拍号时半拍的累积漂移就足以让取整滑拍。所以用合成鼓点把周期钉死在
整数帧之外（120BPM → 0.5s = 21.53 帧）来回归。

另外验证 `beat_swing_s` 手动覆盖，以及无节拍素材要能安全退化（不回中乱摆）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SR = 22050
DUR_S = 20.0
OFFSET_S = 0.37
"""起拍偏移。故意不落在 0 —— 相位检测必须能还原它。"""

CONTROL_HZ = 25.0
MAX_RATE = 2.0

CASES = (100.0, 120.0, 140.0)
"""三档摆幅（限速 2.0/s 下 = min(1, 2.0 × 一拍 / 2)）：
100BPM → 0.60、120BPM → 0.50、140BPM → 0.43。
限速不够满量程时不再让极值滞后，而是按可达上限缩幅来保卡点。"""


def click_track(bpm: float, dur_s: float = DUR_S) -> np.ndarray:
    """每拍一个衰减脉冲，模拟鼓点。150Hz + 900Hz 让谱通量有干净的峰值。"""
    n = int(dur_s * SR)
    out = np.zeros(n, dtype=np.float64)
    rng = np.random.default_rng(0)
    period = 60.0 / bpm
    onset = OFFSET_S
    while onset < dur_s:
        start = int(onset * SR)
        length = int(0.05 * SR)
        t = np.arange(length) / SR
        envelope = np.exp(-t * 60.0)
        tone = np.sin(2 * np.pi * 150.0 * t) + 0.5 * np.sin(2 * np.pi * 900.0 * t)
        stop = min(n, start + length)
        out[start:stop] += envelope[: stop - start] * tone[: stop - start]
        onset += period
    out += 0.002 * rng.standard_normal(n)
    return out.astype(np.float32)


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from music2servo import features, mappers, shaping
    from music2servo.config import AnalysisConfig, MappingConfig, ShapingConfig

    analysis = AnalysisConfig()
    shaping_cfg = ShapingConfig()
    step_limit = shaping_cfg.max_rate_per_s / shaping_cfg.control_hz
    failures: list[str] = []

    print(f"帧栅格 hop={analysis.hop_length / SR * 1000:.2f}ms，"
          f"控制帧 {shaping_cfg.control_hz:.0f}Hz（单步上限 {step_limit:.3f}）\n")
    print(f"{'BPM':>6} {'真实一拍':>9} {'检出摆动':>9} {'检出BPM':>9} "
          f"{'相位差':>8} {'极值时差':>9} {'负侧':>7} {'正侧':>7} {'对称差':>7} {'摆幅/上限':>10}")

    for bpm in CASES:
        beat = 60.0 / bpm
        amplitude = min(1.0, MAX_RATE * beat / 2.0)
        pcm = click_track(bpm)
        track = features.extract(pcm, SR, analysis)
        cfg = MappingConfig(mode="beat")
        raw = mappers.map_track(track, cfg, shaping_cfg.max_rate_per_s)

        name = f"{bpm:.0f}BPM"
        if raw.size != track.n_frames:
            failures.append(f"{name}: 映射输出长度与帧数不一致")
        if raw.min() < -1.0 - 1e-9 or raw.max() > 1.0 + 1e-9:
            failures.append(f"{name}: 映射输出越界 [{raw.min():.4f}, {raw.max():.4f}]")

        grid = mappers.detect_beat_grid(track, cfg)
        if grid is None:
            failures.append(f"{name}: 检测失败，合成的鼓点应当能被检出")
            print(f"{bpm:6.0f} {beat:9.4f}    检测失败")
            continue
        swing, phase = grid

        # 判据 1：摆动周期 = 一拍
        swing_target = beat
        swing_error = abs(swing - swing_target)
        if swing_error > 0.01 * swing_target:
            failures.append(
                f"{name}: 摆动周期 {swing:.4f}s 偏离一拍 {swing_target:.4f}s 超过 1%"
            )

        # 相位：极值应落在 OFFSET_S 起拍的整数拍上（每拍都有极值）
        nearest = OFFSET_S + round((phase - OFFSET_S) / beat) * beat
        phase_error = phase - nearest
        if abs(phase_error) > 0.03:
            failures.append(f"{name}: 相位偏差 {phase_error:+.4f}s 超过 30ms")

        # 与 pipeline.build_mapping 一致：beat 跳过分位拉满，保住按限速算出的摆幅
        shaped = shaping.shape(raw, track.hop_s, shaping_cfg, rescale=False)
        values, times = shaped.values, shaped.times_s

        # 判据 2：成形后的极值时刻仍落在拍点上（跳过开头回中段）
        offsets: list[float] = []
        for index in range(1, values.size - 1):
            if times[index] < 1.0:
                continue
            if (values[index] - values[index - 1]) * (values[index + 1] - values[index]) <= 0:
                if abs(values[index]) < 0.6 * amplitude:
                    continue
                anchor = OFFSET_S + round((times[index] - OFFSET_S) / beat) * beat
                offsets.append(times[index] - anchor)
        if not offsets:
            failures.append(f"{name}: 成形后找不到任何极值")
        else:
            worst = float(np.max(np.abs(offsets)))
            # 容差取 1/4 拍：限速 2.0/s 下 140BPM 仍有残余圆角与回中影响。
            if worst > 0.25 * beat:
                failures.append(
                    f"{name}: 极值偏离拍点最多 {worst * 1000:.0f}ms，超过 1/4 拍"
                )
        mean_offset = float(np.mean(np.abs(offsets))) if offsets else float("nan")

        neg, pos = -float(values.min()), float(values.max())

        # 判据 3：左右对称
        if abs(neg - pos) > 0.05:
            failures.append(f"{name}: 左右行程不对称，负侧 {neg:.4f} 正侧 {pos:.4f}")

        # 判据 4：摆幅达到限速给出的上限（4Hz 低通与死区会再削掉一点，留 0.12 余量）
        utilization = min(neg, pos)
        if utilization < amplitude - 0.12:
            failures.append(
                f"{name}: 摆幅 {utilization:.3f} 明显低于可达上限 {amplitude:.3f}"
            )

        print(f"{bpm:6.0f} {swing_target:9.4f} {swing:9.4f} {60.0 / swing:9.2f} "
              f"{phase_error:+8.4f} {mean_offset:9.4f} {-neg:7.4f} {pos:7.4f} "
              f"{abs(neg - pos):7.4f} {utilization / amplitude:9.1%}")

    # 手动覆盖：填一个明显不同于一拍的值，应当被原样采用
    track = features.extract(click_track(120.0), SR, analysis)
    override = 2.0
    grid = mappers.detect_beat_grid(track, MappingConfig(mode="beat", beat_swing_s=override))
    if grid is None or abs(grid[0] - override) > 1e-9:
        failures.append(f"beat_swing_s={override} 未被采用，实际 {grid}")
    else:
        print(f"\n手动覆盖 beat_swing_s={override}s → 检出摆动 {grid[0]:.4f}s，相位 {grid[1]:.4f}s")

    # 安全退化：无节拍的持续音不应检出网格，也不应摆出无意义的角度
    sustained = (0.3 * np.sin(2 * np.pi * 220.0 * np.arange(int(DUR_S * SR)) / SR)).astype(np.float32)
    flat_track = features.extract(sustained, SR, analysis)
    flat_cfg = MappingConfig(mode="beat")
    if mappers.detect_beat_grid(flat_track, flat_cfg) is not None:
        failures.append("持续音被误判出节拍网格")
    flat_raw = mappers.map_track(flat_track, flat_cfg)
    if float(np.max(np.abs(flat_raw))) > 0.15:
        failures.append(f"无节拍素材摆幅达到 {np.max(np.abs(flat_raw)):.4f}，应接近静止")
    else:
        print(f"无节拍素材 → 未检出网格，最大摆幅 {np.max(np.abs(flat_raw)):.4f}（接近静止）")

    if failures:
        print("\n验证失败:")
        for item in failures:
            print("  -", item)
        return 1

    print("\n验证通过：节拍周期、极值落点、左右对称、行程利用率均达标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
