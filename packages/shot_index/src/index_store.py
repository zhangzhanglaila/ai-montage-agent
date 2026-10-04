"""镜头索引：建索引、存取、检索。

索引结构：
    cache/index/shot_index.json   —— 元数据（镜头时间码、代表帧、分数、编码器信息）
    cache/index/shot_index.npy    —— embedding 矩阵（N x dim，L2 归一化）

embedding 单独存 .npy：CLIP 是 512 维，若塞进 JSON 会体积暴涨且难读。

代表帧来源：不做逐镜头切分（太慢），而是对视频做**一次**低帧率抽帧
（默认 2fps），再给每个镜头挑时间上最近的那一帧。一次解码搞定。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .embedder import BaseEmbedder, get_embedder

INDEX_VERSION = 1


# --------------------------------------------------------------- ffmpeg 工具
def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe_duration(video_path: str) -> float:
    r = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path,
    ])
    try:
        return float(r.stdout.decode().strip())
    except (ValueError, AttributeError):
        return 0.0


def detect_scene_cuts(video_path: str, threshold: float = 0.3) -> List[float]:
    """用 ffmpeg 的 scene 检测拿到切点时间（不依赖 scenedetect/opencv）。"""
    r = _run([
        "ffmpeg", "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ])
    text = r.stderr.decode(errors="ignore")
    times = [float(m.group(1)) for m in re.finditer(r"pts_time:(\d+\.?\d*)", text)]
    return sorted(set(times))


def detect_shots(video_path: str, threshold: float = 0.3,
                 min_len: float = 0.4) -> List[Tuple[float, float]]:
    """返回 [(start, end), ...]；无切点时整段作为一个镜头。"""
    duration = probe_duration(video_path)
    if duration <= 0:
        return []
    cuts = [t for t in detect_scene_cuts(video_path, threshold) if 0.0 < t < duration]
    bounds = [0.0] + cuts + [duration]
    shots = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a >= min_len:
            shots.append((round(a, 3), round(b, 3)))
    return shots


def extract_frames(
    video_path: str, out_dir: str, fps: float = 2.0,
    width: int = 320, limit: int = 0,
) -> List[Tuple[float, str]]:
    """低帧率抽帧。返回 [(time_sec, frame_path), ...]，按时间升序。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pattern = str(out / "f_%05d.jpg")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps={fps},scale={width}:-2",
        "-q:v", "3",
    ]
    if limit > 0:
        cmd += ["-frames:v", str(limit)]
    cmd.append(pattern)
    r = _run(cmd)
    if r.returncode != 0 and not list(out.glob("f_*.jpg")):
        print(f"  [索引] 抽帧失败: {r.stderr.decode(errors='ignore')[-300:]}")
        return []

    frames: List[Tuple[float, str]] = []
    for p in sorted(out.glob("f_*.jpg")):
        idx = int(p.stem.split("_")[1])
        t = (idx - 1) / fps
        frames.append((round(t, 3), str(p)))
    return frames


def _nearest(frames: Sequence[Tuple[float, str]], t: float) -> Tuple[float, str]:
    if not frames:
        return 0.0, ""
    return min(frames, key=lambda ft: abs(ft[0] - t))


# ------------------------------------------------------------------ 数据模型
@dataclass
class ShotRecord:
    """索引中的一条镜头记录"""
    shot_id: int
    source_video: str = ""
    start: float = 0.0
    end: float = 0.0
    duration: float = 0.0
    frame_path: str = ""
    highlight_score: float = 0.0
    motion_score: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None

    def to_dict(self, with_embedding: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        if not with_embedding:
            d.pop("embedding", None)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ShotRecord":
        return cls(
            shot_id=d.get("shot_id", 0),
            source_video=d.get("source_video", ""),
            start=d.get("start", 0.0),
            end=d.get("end", 0.0),
            duration=d.get("duration", 0.0),
            frame_path=d.get("frame_path", ""),
            highlight_score=d.get("highlight_score", 0.0),
            motion_score=d.get("motion_score", 0.0),
            meta=d.get("meta", {}) or {},
            embedding=d.get("embedding"),
        )


# ------------------------------------------------------------------ 索引本体
class ShotIndex:
    """镜头索引：建 / 存 / 取 / 检索"""

    def __init__(self, embedder: Optional[BaseEmbedder] = None,
                 records: Optional[List[ShotRecord]] = None):
        self.embedder = embedder or get_embedder(verbose=False)
        self.records: List[ShotRecord] = records or []
        self._matrix: Optional[np.ndarray] = None

    # ---------------------------------------------------------------- 基础
    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    @property
    def name(self) -> str:
        return getattr(self.embedder, "name", "unknown")

    @property
    def dim(self) -> int:
        return getattr(self.embedder, "dim", 0)

    def matrix(self) -> np.ndarray:
        """N x dim 的 embedding 矩阵（懒构建）"""
        if self._matrix is None:
            vecs = []
            for r in self.records:
                if r.embedding:
                    vecs.append(np.asarray(r.embedding, dtype=np.float32))
                else:
                    vecs.append(np.zeros(self.dim, dtype=np.float32))
            self._matrix = np.stack(vecs) if vecs else np.zeros((0, self.dim), dtype=np.float32)
        return self._matrix

    def add(self, record: ShotRecord) -> None:
        self.records.append(record)
        self._matrix = None

    # ---------------------------------------------------------------- 构建
    def build(
        self,
        video_path: str,
        shots: Optional[Sequence[Any]] = None,
        frames_dir: Optional[str] = None,
        scene_threshold: float = 0.3,
        fps: float = 2.0,
        max_shots: int = 0,
    ) -> "ShotIndex":
        """为一个视频建立索引。

        Args:
            video_path: 源视频
            shots: 已有的镜头列表（对象或 dict，需含 start/end）；None 则自动检测
            frames_dir: 抽帧目录，默认 ``cache/index/frames/<视频名>``
            scene_threshold: 场景切换阈值（越大切得越少）
            fps: 抽帧帧率（挑代表帧用）
            max_shots: 限制镜头数（0 为不限），便于快速试跑
        """
        video = Path(video_path)
        if not video.exists():
            raise FileNotFoundError(f"视频不存在: {video_path}")

        # 1) 镜头边界
        if shots is None:
            pairs = detect_shots(str(video), threshold=scene_threshold)
            print(f"  [索引] 检测到 {len(pairs)} 个镜头")
        else:
            pairs = []
            for s in shots:
                if isinstance(s, dict):
                    a, b = s.get("start", s.get("start_time")), s.get("end", s.get("end_time"))
                else:
                    a = getattr(s, "start_time", None)
                    b = getattr(s, "end_time", None)
                if a is not None and b is not None:
                    pairs.append((float(a), float(b)))
        if max_shots > 0:
            pairs = pairs[:max_shots]
        if not pairs:
            print("  [索引] 没有可用镜头")
            return self

        # 2) 抽帧（一次解码）
        frames_dir = frames_dir or str(Path("cache/index/frames") / video.stem)
        frames = extract_frames(str(video), frames_dir, fps=fps)
        print(f"  [索引] 抽到 {len(frames)} 帧用于挑代表帧")

        # 3) 记录 + 编码
        base = len(self.records)   # 支持多视频追加到同一索引
        frame_paths: List[str] = []
        for i, (a, b) in enumerate(pairs):
            mid = (a + b) / 2.0
            _t, fp = _nearest(frames, mid)
            rec = ShotRecord(
                shot_id=base + i, source_video=str(video), start=round(a, 3),
                end=round(b, 3), duration=round(b - a, 3), frame_path=fp,
            )
            self.records.append(rec)
            frame_paths.append(fp)

        vecs = self.embedder.encode_images(frame_paths)
        for rec, v in zip(self.records[base:], vecs):
            rec.embedding = [float(x) for x in v]
        self._matrix = None
        print(f"  [索引] 编码完成（{self.name}, {self.dim} 维, 累计 {len(self.records)} 镜头）")
        return self

    # ---------------------------------------------------------------- 检索
    def search_by_vector(
        self, vec: np.ndarray, top_k: int = 10, min_score: float = 0.0,
    ) -> List[Tuple[ShotRecord, float]]:
        if not self.records:
            return []
        q = np.asarray(vec, dtype=np.float32).reshape(-1)
        m = self.matrix()
        if m.shape[1] != q.shape[0]:
            raise ValueError(f"维度不匹配：索引 {m.shape[1]} vs 查询 {q.shape[0]}")
        scores = m @ q
        order = np.argsort(-scores)
        out = []
        for i in order[:max(1, top_k)]:
            s = float(scores[i])
            if s < min_score:
                break
            out.append((self.records[int(i)], s))
        return out

    def search_text(
        self, query: str, top_k: int = 10, min_score: float = 0.0,
    ) -> List[Tuple[ShotRecord, float]]:
        if not getattr(self.embedder, "supports_text", False):
            raise RuntimeError(f"编码器 {self.name} 不支持文本检索")
        vec = self.embedder.encode_texts([query])[0]
        return self.search_by_vector(vec, top_k=top_k, min_score=min_score)

    def search_image(
        self, image_path: str, top_k: int = 10, min_score: float = 0.0,
    ) -> List[Tuple[ShotRecord, float]]:
        vec = self.embedder.encode_images([image_path])[0]
        return self.search_by_vector(vec, top_k=top_k, min_score=min_score)

    # ---------------------------------------------------------------- 存取
    def save(self, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "embedder": self.name,
            "dim": self.dim,
            "supports_text": bool(getattr(self.embedder, "supports_text", False)),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(self.records),
            "shots": [r.to_dict(with_embedding=False) for r in self.records],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        np.save(str(p.with_suffix(".npy")), self.matrix())
        print(f"  [索引] 已保存: {p}（{len(self.records)} 镜头）")
        return str(p)

    @classmethod
    def load(cls, path: str, embedder: Optional[BaseEmbedder] = None) -> "ShotIndex":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"索引不存在: {path}")
        payload = json.loads(p.read_text(encoding="utf-8"))
        records = [ShotRecord.from_dict(d) for d in payload.get("shots", [])]

        npy = p.with_suffix(".npy")
        vecs: Optional[np.ndarray] = None
        if npy.exists():
            vecs = np.load(str(npy))
        for i, rec in enumerate(records):
            if vecs is not None and i < len(vecs):
                rec.embedding = [float(x) for x in vecs[i]]

        idx = cls(embedder=embedder or get_embedder(verbose=False), records=records)
        idx._matrix = vecs
        return idx

    @staticmethod
    def describe(path: str) -> Dict[str, Any]:
        """只读索引头信息（不加载 embedding）。"""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        payload.pop("shots", None)
        return payload
