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
        duration: Optional[float] = None,
        cut_times: Optional[List[float]] = None,
    ) -> List[SfxCue]:
        """根据节拍规划音效。

        Args:
            beats: Beat 列表（需含 time / strength / beat_type 属性）；
                时间应已按成片 0s 对齐（pipeline 会裁掉 BGM 前奏）。
            style: 风格（intense/dynamic/calm），决定音效密度
            max_cues: 音效数量上限
            duration: 成片实际时长（秒）。给定时只在该区间内选点，
                避免音效落到成片之外被 amix 静默截掉。
            cut_times: 真实剪辑点时间列表；whoosh 优先落在这些点上。

        Returns:
            按时间排序的 SfxCue 列表
        """
        if not beats:
            return []

        # 只保留成片时长内的节拍。原实现使用整首 BGM 的拍点：
        # 视频只有十几秒、BGM 有两分钟时，大部分 cue 会落在成片之外，
        # 混音时被静默截掉，日志却仍按 "已叠加 N 个" 计数。
        if duration is not None and duration > 0:
            beats = [b for b in beats if 0.0 <= float(b.time) < float(duration)]
        if not beats:
            return []

        density = STYLE_DENSITY.get(style, 0.5)
        strong = [b for b in beats if getattr(b, "beat_type", "normal") == "strong"]
        other = [b for b in beats if getattr(b, "beat_type", "normal") != "strong"]

        # 预留名额给真实切点 whoosh 和 riser
        budget = max(2, max_cues - 4)
        target = max(2, min(int(len(beats) * density), budget))

        # 均匀覆盖整段视频，而不是只取最前面的 target 个强拍
        selected: List = []
        if strong:
            if len(strong) > target:
                step = max(1, len(strong) // target)
                selected = list(strong[::step][:target])
            else:
                selected = list(strong)
        if len(selected) < target and other:
            need = target - len(selected)
            step = max(1, len(other) // max(1, need))
            selected += list(other[::step][:need])
        selected.sort(key=lambda b: float(b.time))

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

        # 真实剪辑点上的 whoosh：原来 "whoosh 落在剪辑点" 只是文档说法，
        # 实际是按强拍下标轮换。这里显式接收切点，隔一个切点放一次。
        if cut_times:
            used = [c.time for c in cues]
            added = 0
            for j, raw_t in enumerate(cut_times):
                if len(cues) >= max_cues or added >= 3:
                    break
                t = float(raw_t)
                if t < 0.15:
                    continue
                if duration is not None and t > float(duration) - 0.15:
                    continue
                if j % 2:
                    continue
                if any(abs(t - u) < 0.15 for u in used):
                    continue
                cues.append(SfxCue(
                    time=t, kind="whoosh",
                    gain_db=DEFAULT_GAIN_DB["whoosh"],
                ))
                used.append(t)
                added += 1

        # 在高潮（强度最大）的拍点前加一条 riser；必须落在成片内
        if len(cues) < max_cues:
            peak = max(beats, key=lambda b: getattr(b, "strength", 0.0))
            riser_time = float(peak.time) - RISER_LEAD
            if riser_time > 0.3:
                if duration is None or riser_time < float(duration) - 0.05:
                    cues.append(SfxCue(
                        time=riser_time,
                        kind="riser",
                        gain_db=DEFAULT_GAIN_DB["riser"],
                    ))

        # 最终再按成片时长过滤一次，并去掉几乎同时的重复 cue
        if duration is not None and duration > 0:
            cues = [c for c in cues if 0.0 <= c.time < float(duration) - 0.02]
        cues.sort(key=lambda c: c.time)
        deduped: List[SfxCue] = []
        for cue in cues:
            if deduped and abs(cue.time - deduped[-1].time) < 0.02:
                continue
            deduped.append(cue)
        cues = deduped[:max_cues]

        # 解析素材路径
        for cue in cues:
            cue.asset_path = self.library.get(cue.kind)
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
