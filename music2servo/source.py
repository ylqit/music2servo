"""输入获取层：本地文件或远程 URL -> 单声道 float32 PCM。

URL 一律由本层自己下载再解码，不走浏览器的直连，
这样可以绕开 CORS / 防盗链 / Referer 校验造成的不稳定。
"""

from __future__ import annotations

import io
import urllib.request
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .config import SourceConfig


class AudioLoadError(RuntimeError):
    """输入无法读取或解码时抛出，调用方据此给出可读的报错。"""


def load_audio(source: str, cfg: SourceConfig | None = None) -> tuple[np.ndarray, int]:
    """读取音频，返回 (pcm, sr)。

    pcm 为单声道 float32，值域约 [-1, 1]，采样率等于 cfg.sr。
    """
    cfg = cfg or SourceConfig()
    data, src_sr = _read(source, cfg)
    pcm = _to_mono(data)
    if src_sr != cfg.sr:
        pcm = _resample(pcm, src_sr, cfg.sr)
    if cfg.max_duration_s is not None:
        limit = int(round(cfg.max_duration_s * cfg.sr))
        pcm = pcm[:limit]
    return pcm, cfg.sr


def _is_url(source: str) -> bool:
    lowered = source.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _read(source: str, cfg: SourceConfig) -> tuple[np.ndarray, int]:
    """返回 (原声道数据 shape=(n, ch), 原始采样率)。"""
    try:
        if _is_url(source):
            with urllib.request.urlopen(source, timeout=cfg.url_timeout_s) as resp:
                raw = resp.read()
            return sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)

        path = Path(source).expanduser()
        if not path.is_file():
            raise AudioLoadError(f"文件不存在: {path}")
        return sf.read(str(path), dtype="float32", always_2d=True)
    except AudioLoadError:
        raise
    except Exception as exc:  # 解码器抛出的异常类型很杂，统一收敛
        raise AudioLoadError(f"无法解码输入 {source}: {exc}") from exc


def _to_mono(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        mono = data
    elif data.shape[1] == 1:
        mono = data[:, 0]
    else:
        mono = data.mean(axis=1)
    return np.ascontiguousarray(mono, dtype=np.float32)


def _resample(pcm: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """有理数倍重采样，避免简单抽取造成混叠。"""
    divisor = gcd(int(sr_in), int(sr_out))
    up, down = int(sr_out) // divisor, int(sr_in) // divisor
    return resample_poly(pcm, up, down).astype(np.float32)