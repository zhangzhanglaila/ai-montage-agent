"""镜头检索器：在 ShotIndex 之上提供更好用的检索 / 导出 / 展示接口。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .index_store import ShotIndex, ShotRecord


@dataclass
class ShotHit:
    """一条检索命中"""
    shot_id: int
    source_video: str
    start: float
    end: float
    duration: float
    frame_path: str
    score: float

    @classmethod
    def from_record(cls, rec: ShotRecord, score: float) -> "ShotHit":
        return cls(rec.shot_id, rec.source_video, rec.start, rec.end,
                   rec.duration, rec.frame_path, score)

    def label(self) -> str:
        return f"#{self.shot_id:<3d} {self.start:6.2f}-{self.end:6.2f}s  score={self.score:.3f}  {Path(self.source_video).name}"


class ShotRetriever:
    """语义镜头检索器"""

    def __init__(self, index: ShotIndex):
        self.index = index

    # ------------------------------------------------------------------ 检索
    def search(
        self,
        query: Optional[str] = None,
        image: Optional[str] = None,
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> List[ShotHit]:
        if query and image:
            raise ValueError("query 与 image 只能二选一")
        if query:
            pairs = self.index.search_text(query, top_k=top_k, min_score=min_score)
        elif image:
            pairs = self.index.search_image(image, top_k=top_k, min_score=min_score)
        else:
            raise ValueError("必须提供 query 或 image")
        return [ShotHit.from_record(rec, s) for rec, s in pairs]

    # ------------------------------------------------------------------ 导出
    def export_clips(
        self, hits: Sequence[ShotHit], out_dir: str, prefix: str = "hit",
    ) -> List[str]:
        """把命中的镜头切片导出为独立 mp4（无损 copy 尽量，失败则重编码）。"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        files: List[str] = []
        for i, h in enumerate(hits):
            if not h.source_video or not Path(h.source_video).exists():
                continue
            dst = out / f"{prefix}_{i:02d}_shot{h.shot_id}.mp4"
            cmd = [
                "ffmpeg", "-y", "-ss", f"{h.start:.3f}", "-i", h.source_video,
                "-t", f"{max(0.05, h.duration):.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", str(dst),
            ]
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode == 0 and dst.exists():
                files.append(str(dst))
        print(f"  [检索] 已导出 {len(files)} 个镜头切片 -> {out}")
        return files


def format_hits(hits: Sequence[ShotHit], show_frame: bool = False) -> str:
    """把命中列表格式化成便于终端阅读的文本。"""
    if not hits:
        return "  （无命中）"
    lines = [f"  命中 {len(hits)} 个镜头："]
    for i, h in enumerate(hits, 1):
        lines.append(f"    {i:>2d}. {h.label()}")
        if show_frame and h.frame_path:
            lines.append(f"        代表帧: {h.frame_path}")
    return "\n".join(lines)
