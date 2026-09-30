"""本地 Web 界面：标准库 http.server 实现，零新增依赖。

为什么不用 FastAPI / Flask：这是本机单用户的调参工具，一共四个接口，
不需要路由框架、鉴权、模板引擎。而且本项目刻意保持轻依赖
（连 librosa 都不装），再加上当前 venv 是 `--system-site-packages`
继承 anaconda 的，能装什么不由我们决定。用标准库最稳。

音频仍然全部由后端读取（`source.load_audio`），前端只负责画和放：
这样既绕开浏览器 CORS / 防盗链，也保证页面看到的就是 CLI 算出来的东西。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import shutil
import tempfile
import threading
import webbrowser
from dataclasses import fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import soundfile as sf

from music2servo.config import (
    AnalysisConfig,
    MappingConfig,
    SegmentConfig,
    ServoConfig,
    ShapingConfig,
    SourceConfig,
)
from music2servo.mappers import ORDER
from music2servo.pipeline import PipelineConfig, build_mapping, run
from music2servo.pwm import write_frames_csv
from music2servo.source import AudioLoadError

from .report import build_payload, defaults_payload

STATIC_DIR = Path(__file__).resolve().parent / "static"

MAX_BODY_BYTES = 64 * 1024 * 1024
"""上传音频走 base64，比原始字节大 1/3，给足余量。"""

KEEP_JOBS = 5
"""同时保留的 job 数（含磁盘产物）。超出就淘汰最旧的。"""

_UNSUPPORTED_SUFFIX = {".m4a", ".m4b", ".aac", ".mp4"}
"""soundfile 解不了的容器，提前拦掉并给出可读提示，别让用户看到解码器原始报错。"""

_GROUPS = {
    "source": SourceConfig,
    "analysis": AnalysisConfig,
    "segment": SegmentConfig,
    "mapping": MappingConfig,
    "shaping": ShapingConfig,
    "servo": ServoConfig,
}

_TUPLE_FIELDS = {"band_low_hz", "band_high_hz", "beat_bpm_range"}


class JobStore:
    """按 job 保存分析结果与磁盘产物（片段音频、PWM CSV）。"""

    def __init__(self, keep: int = KEEP_JOBS):
        self.root = Path(tempfile.mkdtemp(prefix="music2servo-web-"))
        self.keep = keep
        self._lock = threading.Lock()
        self._counter = 0
        self._order: list[str] = []
        self._entries: dict[str, dict] = {}

    def create(self) -> tuple[str, Path]:
        with self._lock:
            job_id = f"j{self._counter}"
            self._counter += 1
            directory = self.root / job_id
            directory.mkdir(parents=True, exist_ok=True)
            self._entries[job_id] = {"dir": directory, "result": None}
            self._order.append(job_id)
            while len(self._order) > self.keep:
                self._evict(self._order.pop(0))
            return job_id, directory

    def directory(self, job_id: str) -> Path | None:
        with self._lock:
            entry = self._entries.get(job_id)
            return entry["dir"] if entry else None

    def get_result(self, job_id: str | None):
        if not job_id:
            return None
        with self._lock:
            entry = self._entries.get(job_id)
            return entry["result"] if entry else None

    def set_result(self, job_id: str, result) -> None:
        with self._lock:
            if job_id in self._entries:
                self._entries[job_id]["result"] = result

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _evict(self, job_id: str) -> None:
        entry = self._entries.pop(job_id, None)
        if entry:
            shutil.rmtree(entry["dir"], ignore_errors=True)


def _instantiate(cls, data):
    """按 dataclass 字段名过滤后实例化，忽略前端多传的键。"""
    if not isinstance(data, dict):
        return cls()
    allowed = {field.name for field in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key not in allowed:
            continue
        if key in _TUPLE_FIELDS and isinstance(value, list):
            value = tuple(float(item) for item in value)
        kwargs[key] = value
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{cls.__name__} 参数不合法：{exc}") from exc


def _build_config(payload: dict) -> PipelineConfig:
    raw = payload.get("config") or {}
    return PipelineConfig(**{name: _instantiate(cls, raw.get(name)) for name, cls in _GROUPS.items()})


def _reusable(old: PipelineConfig, new: PipelineConfig) -> bool:
    """只有 source / analysis / segment 都不变时，片段特征才能复用。

    改 mapping / shaping / servo 不影响特征提取与截取，
    可以直接跳到后段（映射 → 成形 → PWM），省掉两遍特征提取（含两遍 YIN）。

    注意：这里只比配置，不比音频本身，所以 base_job 相当于调用方的断言 ——
    换了音频之后必须不再传旧 job，否则会拿着上一首歌的片段特征与 PCM 继续算
    （现象：界面显示新文件名，尾巴与音频却还是上一首）。
    """
    return (old.source, old.analysis, old.segment) == (new.source, new.analysis, new.segment)


def _guard_suffix(source_spec: str) -> None:
    suffix = Path(urlparse(source_spec).path).suffix.lower()
    if suffix in _UNSUPPORTED_SUFFIX:
        supported = "MP3 / FLAC / WAV / OGG"
        raise ValueError(f"不支持 {suffix} 格式（soundfile 限制），请改用 {supported}")


def _analyze(store: JobStore, payload: dict) -> dict:
    source_spec = (payload.get("source") or "").strip()
    job_id, directory = store.create()

    audio_b64 = payload.get("audio_b64")
    if audio_b64:
        name = Path(payload.get("audio_name") or "input.wav").name
        target = directory / f"input{Path(name).suffix.lower() or '.wav'}"
        target.write_bytes(base64.b64decode(audio_b64))
        source_spec = str(target)

    if not source_spec:
        raise ValueError("请填写音频的本地路径或 URL，或直接选择文件上传")
    _guard_suffix(source_spec)

    cfg = _build_config(payload)
    modes = [str(mode) for mode in (payload.get("modes") or list(ORDER))]
    if not modes:
        modes = list(ORDER)

    base = store.get_result(payload.get("base_job"))
    if base is not None and _reusable(base.cfg, cfg):
        mappings = [build_mapping(mode, base.clip_track, cfg) for mode in modes]
        result = replace(base, source=source_spec, cfg=cfg, mappings=mappings)
    else:
        result = run(source_spec, cfg, modes)

    sf.write(directory / "clip.wav", result.clip.pcm, result.sr)
    write_frames_csv(
        directory / "frames.csv", {item.mode: item.pwm for item in result.mappings}
    )
    store.set_result(job_id, result)
    return build_payload(result, job_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "music2servo"
    store: JobStore

    def log_message(self, fmt, *args):  # noqa: A002 - 覆盖父类签名
        """默认会把每个静态资源的请求都打到 stderr，把 API 日志淹掉。"""

    def do_GET(self) -> None:  # noqa: N802 - 父类约定
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send_file(STATIC_DIR / "index.html")
        if path == "/api/config":
            return self._send_json(defaults_payload())
        if path.startswith("/api/clip.wav/"):
            return self._send_job_file(path, "clip.wav", "audio/wav")
        if path.startswith("/api/frames.csv/"):
            return self._send_job_file(path, "frames.csv", "text/csv")
        if path.startswith("/static/"):
            return self._send_static(path)
        return self._error(404, "未找到该路径")

    def do_POST(self) -> None:  # noqa: N802 - 父类约定
        if urlparse(self.path).path != "/api/analyze":
            return self._error(404, "未找到该路径")
        try:
            payload = self._read_json()
        except ValueError as exc:
            return self._error(400, str(exc))
        try:
            report = _analyze(self.store, payload)
        except AudioLoadError as exc:
            return self._error(422, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # 兜底：把异常回给页面，而不是让请求干挂
            return self._error(500, f"分析失败：{exc}")
        print(
            f"[analyze] {report['source']} · {len(report['modes'])} 模式 · job {report['job_id']}",
            flush=True,
        )
        return self._send_json(report)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length <= 0:
            raise ValueError("请求体为空")
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大，请换更短的音频或改用本地路径")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc

    def _send_job_file(self, path: str, filename: str, content_type: str) -> None:
        job_id = path.rstrip("/").rsplit("/", 1)[-1]
        directory = self.store.directory(job_id)
        if directory is None:
            return self._error(404, "该 job 已过期，请重新分析")
        target = directory / filename
        if not target.is_file():
            return self._error(404, f"{filename} 尚未生成")
        return self._send_bytes(target.read_bytes(), content_type)

    def _send_static(self, path: str) -> None:
        target = (STATIC_DIR / path[len("/static/") :]).resolve()
        if not target.is_relative_to(STATIC_DIR) or not target.is_file():
            return self._error(404, "静态资源不存在")
        return self._send_file(target)

    def _send_file(self, target: Path) -> None:
        if not target.is_file():
            return self._error(404, "文件不存在")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return self._send_bytes(target.read_bytes(), ctype)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type = f"{content_type}; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m web.server", description="music2servo 本地可视化界面"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    store = JobStore()
    Handler.store = store
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"music2servo 界面：{url}", flush=True)
    print("按 Ctrl+C 退出", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
