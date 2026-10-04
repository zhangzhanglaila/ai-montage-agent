"""音效卡点引擎。

根据 BGM 节拍自动规划并在成片上叠加音效，提升"专业感"：
    - 强拍   -> impact（低频撞击）
    - 剪辑点 -> whoosh（呼啸过渡）
    - 高潮前 -> riser（上升铺垫）
    - 弱拍   -> click（轻微点缀）

用法：
    engine = SfxEngine()
    cues = engine.plan(beats, style="dynamic")
    engine.mix("final.mp4", cues, "final_sfx.mp4")
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .sfx_library import SfxLibrary


# 各音效类型的默认增益（dB）
DEFAULT_GAIN_DB = {
    "impact": -5.0,
    "whoosh": -9.0,
    "riser": -11.0,
    "click": -14.0,
}

# 不同风格的音效密度（0~1，越大越密）
STYLE_DENSITY = {
    "intense": 0.75,
    "dynamic": 0.5,
    "calm": 0.25,
}

# riser 提前量（秒）：在强拍之前起音
RISER_LEAD = 0.9


@dataclass
class SfxCue:
    """单个音效安排"""
    time: float
    kind: str
    gain_db: float = -6.0
    asset_path: str = ""

    def to_dict(self) -> Dict:
        return {
            "time": round(self.time, 3),
            "kind": self.kind,
            "gain_db": self.gain_db,
            "asset_path": self.asset_path,
        }


class SfxEngine:
    """音效卡点引擎"""

    def __init__(self, library: Optional[SfxLibrary] = None):
        self.library = library or SfxLibrary()

    # ------------------------------------------------------------------ plan
    def plan(
        self,
        beats: List,
        style: str = "dynamic",
        max_cues: int = 16,
    ) -> List[SfxCue]:
        """根据节拍规划音效。

        Args:
            beats: Beat 列表（需含 time / strength / beat_type 属性）
            style: 风格（intense/dynamic/calm），决定音效密度
            max_cues: 音效数量上限

        Returns:
            按时间排序的 SfxCue 列表
        """
        if not beats:
            return []

        density = STYLE_DENSITY.get(style, 0.5)
        strong = [b for b in beats if getattr(b, "beat_type", "normal") == "strong"]
        other = [b for b in beats if getattr(b, "beat_type", "normal") != "strong"]

        # 目标数量：按总节拍数 × 密度，至少 2 个
        target = max(2, min(int(len(beats) * density), max_cues))

        # 优先取强拍，不足则从其余拍点均匀补齐
        selected = list(strong)[:target]
        if len(selected) < target and other:
            need = target - len(selected)
            step = max(1, len(other) // need)
            selected += other[::step][:need]
        selected.sort(key=lambda b: b.time)

        cues: List[SfxCue] = []
        for i, beat in enumerate(selected):
            bt = getattr(beat, "beat_type", "normal")
            if bt == "strong":
                # 强拍以 impact 为主，每第 3 个换成 whoosh 增加变化
                kind = "whoosh" if i % 3 == 2 else "impact"
            else:
                kind = "whoosh" if i % 2 == 1 else "click"
            cues.append(SfxCue(
                time=float(beat.time),
                kind=kind,
                gain_db=DEFAULT_GAIN_DB[kind],
            ))

        # 在高潮（能量/强度最大）的拍点前加一条 riser
        peak = max(beats, key=lambda b: getattr(b, "strength", 0.0))
        riser_time = float(peak.time) - RISER_LEAD
        if riser_time > 0.3:
            cues.append(SfxCue(
                time=riser_time,
                kind="riser",
                gain_db=DEFAULT_GAIN_DB["riser"],
            ))

        # 解析素材路径并排序
        for cue in cues:
            cue.asset_path = self.library.get(cue.kind)
        cues.sort(key=lambda c: c.time)
        return cues

    # ------------------------------------------------------------------- mix
    def mix(self, video_path: str, cues: List[SfxCue], output_path: str) -> str:
        """把音效叠加到视频音轨上。

        Args:
            video_path: 输入视频
            cues: 音效安排
            output_path: 输出视频

        Returns:
            输出视频路径
        """
        if not Path(video_path).exists():
            raise FileNotFoundError(f"视频不存在: {video_path}")
        if not cues:
            raise ValueError("没有音效安排，无需混音")

        inputs: List[str] = ["-i", video_path]
        for cue in cues:
            inputs += ["-i", cue.asset_path]

        has_audio = self._has_audio(video_path)
        dur = self._duration(video_path)
        parts: List[str] = []
        labels: List[str] = []
        idx = 1

        if has_audio:
            labels.append("[0:a]")
        else:
            # 视频无音轨：补一段静音作为混音基底
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo"]
            parts.append(f"[{idx}:a]atrim=0:{dur},asetpts=N/SR/TB[base]")
            labels.append("[base]")
            idx += 1

        for i, cue in enumerate(cues):
            ms = max(0, int(cue.time * 1000))
            parts.append(
                f"[{idx}:a]adelay={ms}:all=1,volume={cue.gain_db}dB[sfx{i}]"
            )
            labels.append(f"[sfx{i}]")
            idx += 1

        parts.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[aout]"
        )

        cmd = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", ";".join(parts),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not Path(output_path).exists():
            raise RuntimeError(
                "音效混音失败:\n"
                + proc.stderr.decode("utf-8", errors="ignore")[-800:]
            )
        return output_path

    # --------------------------------------------------------------- helpers
    def _has_audio(self, path: str) -> bool:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
               "-show_entries", "stream=index", "-of", "csv=p=0", path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return bool(r.stdout.strip())

    def _duration(self, path: str) -> float:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        try:
            return float(r.stdout.strip())
        except (TypeError, ValueError):
            return 0.0
