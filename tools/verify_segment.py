"""合成一段结构明确的测试音频，并端到端验证高潮截取。

预期结构（sr=22050, 总长 80s）：
  0-25s  前奏/主歌：低能量持续音        -> 不应被选中
  25-55s 副歌：高能量 + 每 0.5s 强拍   -> 应被选中
  55-80s 尾奏：低能量持续音            -> 不应被选中
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 22050
INTRO_END = 25.0
CHORUS_END = 55.0
TOTAL = 80.0
WINDOW = 20.0


def make_song() -> np.ndarray:
    n = int(TOTAL * SR)
    t = np.arange(n) / SR
    x = np.zeros(n, dtype=np.float64)

    quiet = (t < INTRO_END) | (t >= CHORUS_END)
    loud = ~quiet

    # 低能量区：单音持续，振幅很小
    x[quiet] = 0.06 * np.sin(2 * np.pi * 220.0 * t[quiet])

    # 高能量区：谐波铺底
    for freq, amp in ((440.0, 0.30), (660.0, 0.22), (880.0, 0.16)):
        x[loud] += amp * np.sin(2 * np.pi * freq * t[loud])

    # 高能量区：每 0.5s 一个衰减低频强拍，制造谱通量峰值
    beat_interval = int(round(0.5 * SR))
    decay = np.exp(-np.arange(beat_interval) / (0.05 * SR))
    beat = 0.9 * decay * np.sin(2 * np.pi * 60.0 * np.arange(beat_interval) / SR)
    start = int(round(INTRO_END * SR))
    stop = int(round(CHORUS_END * SR))
    for offset in range(start, stop - beat_interval, beat_interval):
        x[offset : offset + beat_interval] += beat

    rng = np.random.default_rng(0)
    x[quiet] += 0.0005 * rng.standard_normal(quiet.sum())
    x[loud] += 0.005 * rng.standard_normal(loud.sum())

    return np.clip(x, -1.0, 1.0).astype(np.float32)


def main() -> int:
    song = make_song()
    fixture = Path("/tmp/m2s_fixture.wav")
    sf.write(fixture, song, SR)
    print(f"测试音频: {fixture}  ({TOTAL:.0f}s, 副歌 {INTRO_END:.0f}-{CHORUS_END:.0f}s)")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from music2servo.config import SegmentConfig
    from music2servo.pipeline import PipelineConfig, run
    from music2servo.pwm import frame_count
    from music2servo.viz import plot_tuning_report

    cfg = PipelineConfig(segment=SegmentConfig(window_s=WINDOW))
    modes = ["envelope", "brightness", "pitch"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run(str(fixture), cfg, modes)
        report = plot_tuning_report(result, "/tmp/m2s_report.png")

    glyph = [w for w in caught if "Glyph" in str(w.message) or "glyph" in str(w.message)]
    print(f"字体缺字警告: {len(glyph)} 条")
    for w in glyph[:5]:
        print("   ", w.message)

    clip = result.clip
    print(
        f"检测结果: {clip.start_s:.2f} → {clip.end_s:.2f}s "
        f"(时长 {clip.duration_s:.2f}s)  得分 {clip.score:.4f}"
    )
    print(f"报告图片: {report}  ({report.stat().st_size / 1024:.0f} KB)")

    failures: list[str] = []
    if not clip.auto:
        failures.append("应为自动检测模式")
    if abs(clip.duration_s - WINDOW) > 0.05:
        failures.append(f"片段时长应为 {WINDOW}s，实际 {clip.duration_s:.2f}s")
    # 高能区 [25, 55]，20s 窗口完全落在其中的起点范围是 [25, 35]
    if not (INTRO_END - 2.0 <= clip.start_s <= CHORUS_END - WINDOW + 2.0):
        failures.append(
            f"窗口起点 {clip.start_s:.2f}s 未落在预期区间 "
            f"[{INTRO_END - 2.0:.1f}, {CHORUS_END - WINDOW + 2.0:.1f}]"
        )

    # 判据：片段能量应接近真实高能区，而不是接近整首均值。
    # 用整首均值做基准是没有意义的——副歌只占 37.5%，会把基准拉高。
    def rms(x: np.ndarray) -> float:
        return float(np.sqrt(np.mean(np.square(x.astype(np.float64)))))

    clip_rms = rms(result.clip.pcm)
    loud_rms = rms(song[int(INTRO_END * SR) : int(CHORUS_END * SR)])
    full_rms = rms(result.pcm)
    print(
        f"片段 RMS {clip_rms:.4f} | 真实高能区 RMS {loud_rms:.4f} | 整首 RMS {full_rms:.4f}"
    )
    if clip_rms < 0.85 * loud_rms:
        failures.append(
            f"片段能量 {clip_rms:.4f} 明显低于真实高能区 {loud_rms:.4f}，疑似切到低能量段"
        )
    if report.stat().st_size < 10_000:
        failures.append("报告图片过小，可能未正常绘制")

    # ---- 映射 / 成形 / PWM 三层契约断言 ----
    shaping_cfg = cfg.shaping
    servo_cfg = cfg.servo
    step_limit = shaping_cfg.max_rate_per_s / shaping_cfg.control_hz
    print(f"\n限速单步上限 {step_limit:.4f}（{shaping_cfg.max_rate_per_s}/s ÷ {shaping_cfg.control_hz}Hz）")

    for item in result.mappings:
        raw = item.raw
        shaped = item.shaped.values
        pulse = item.pwm.pulse_us
        print(
            f"[{item.mode:<10}] 映射 [{raw.min():+.3f},{raw.max():+.3f}]"
            f" | 成形 [{shaped.min():+.3f},{shaped.max():+.3f}]"
            f" | 脉宽 {pulse.min():.0f}~{pulse.max():.0f}µs | PWM {item.pwm.n_frames} 帧"
        )

        if raw.size != result.clip_track.n_frames:
            failures.append(
                f"{item.mode}: 映射输出长度 {raw.size} 与片段帧数 "
                f"{result.clip_track.n_frames} 不一致（违反同一时间栅格契约）"
            )
        if raw.min() < -1.0 - 1e-9 or raw.max() > 1.0 + 1e-9:
            failures.append(
                f"{item.mode}: 映射输出越界 [{raw.min():.4f}, {raw.max():.4f}]，应为 [-1,1]"
            )
        if shaped.size == 0:
            failures.append(f"{item.mode}: 成形输出为空")
            continue
        if shaped.min() < -1.0 - 1e-9 or shaped.max() > 1.0 + 1e-9:
            failures.append(
                f"{item.mode}: 成形输出越界 [{shaped.min():.4f}, {shaped.max():.4f}]"
            )
        if abs(shaped[0]) > 1e-9:
            failures.append(f"{item.mode}: 起点未回中（{shaped[0]:.4f} ≠ 0）")
        if abs(shaped[-1]) > step_limit + 1e-9:
            failures.append(f"{item.mode}: 终点未回中（{shaped[-1]:.4f}）")

        max_step = float(np.max(np.abs(np.diff(shaped)))) if shaped.size > 1 else 0.0
        if max_step > step_limit + 1e-9:
            failures.append(
                f"{item.mode}: 限速失效，最大单步 {max_step:.4f} > 上限 {step_limit:.4f}"
            )

        if pulse.size == 0:
            failures.append(f"{item.mode}: PWM 帧序列为空")
        else:
            if pulse.min() < servo_cfg.pulse_min_us - 1e-6:
                failures.append(
                    f"{item.mode}: 脉宽低于下限 {pulse.min():.2f} < {servo_cfg.pulse_min_us}"
                )
            if pulse.max() > servo_cfg.pulse_max_us + 1e-6:
                failures.append(
                    f"{item.mode}: 脉宽高于上限 {pulse.max():.2f} > {servo_cfg.pulse_max_us}"
                )
            expected = frame_count(item.shaped.duration_s, servo_cfg.pwm_hz)
            if item.pwm.n_frames != expected:
                failures.append(
                    f"{item.mode}: PWM 帧数 {item.pwm.n_frames} ≠ 期望 {expected}"
                )

    if len(result.mappings) == 3:
        raws = [item.raw for item in result.mappings]
        if np.allclose(raws[0], raws[1]) and np.allclose(raws[1], raws[2]):
            failures.append("三种映射输出完全相同，mode 参数疑似未生效")

    if failures:
        print("\n验证失败:")
        for item in failures:
            print("  -", item)
        return 1

    print("\n验证通过：高潮截取落点正确，三种映射 / 成形 / PWM 契约成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())