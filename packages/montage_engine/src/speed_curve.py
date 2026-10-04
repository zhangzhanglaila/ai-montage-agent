"""变速曲线（速度斜坡）

从「整段等比变速」升级为**带形状的速度曲线**：卡点冲刺、平滑慢动作、hero 慢-快-慢。

为什么用「分段等速」而不是一条连续速度函数
------------------------------------------
视频可以轻松用 `setpts` 表达连续变速，但 ffmpeg 的 `atempo` **不支持随时间变化**。
若只让视频平滑、音频近似，两者时长会累积漂移 → 音画不同步。因此这里把速度曲线
**离散成 N 段等速**，每段：

    视频:  trim[start,end] + setpts=(PTS-STARTPTS)/s_i
    音频:  atrim[start,end] + asetpts=PTS-STARTPTS + atempo(s_i)

两段都被压成同一个输出时长 ``(end-start)/s_i``，逐段对齐 ⇒ 全程严格同步。
段数够多（默认 8~12）时，观感上就是平滑斜坡。

`atempo` 单元因子一律拆到 [0.5, 2.0] 区间（老版本 ffmpeg 也只支持这个范围），
超出就串联多级，保证兼容。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple


# --------------------------------------------------------------- 数据结构
@dataclass
class SpeedSegment:
    """一段等速区间（start/end 为**输入**时间）"""
    start: float
    end: float
    speed: float          # 0.5=半速慢放，2.0=2 倍速

    @property
    def in_duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def out_duration(self) -> float:
        return self.in_duration / self.speed if self.speed > 0 else self.in_duration


class SpeedCurve:
    """速度曲线 = 一串等速段"""

    def __init__(self, segments: Sequence[SpeedSegment]):
        self.segments: List[SpeedSegment] = [
            SpeedSegment(float(s.start), float(s.end), max(0.05, float(s.speed)))
            for s in segments if s.end > s.start
        ]

    # ------------------------------------------------------------ 统计
    def __len__(self) -> int:
        return len(self.segments)

    def input_duration(self) -> float:
        return sum(s.in_duration for s in self.segments)

    def output_duration(self) -> float:
        return sum(s.out_duration for s in self.segments)

    def min_speed(self) -> float:
        return min((s.speed for s in self.segments), default=1.0)

    def max_speed(self) -> float:
        return max((s.speed for s in self.segments), default=1.0)

    def describe(self) -> str:
        speeds = [f"{s.speed:.2f}" for s in self.segments]
        return (f"{len(self.segments)} 段，速度 "
                f"{self.min_speed():.2f}~{self.max_speed():.2f}，"
                f"输入 {self.input_duration():.2f}s -> 输出 {self.output_duration():.2f}s"
                f"\n    段速: {' '.join(speeds)}")

    def to_dict(self) -> dict:
        return {
            "segments": [{"start": s.start, "end": s.end, "speed": s.speed}
                         for s in self.segments],
            "input_duration": round(self.input_duration(), 3),
            "output_duration": round(self.output_duration(), 3),
        }

    # ------------------------------------------------------------ 构造
    @classmethod
    def constant(cls, duration: float, speed: float) -> "SpeedCurve":
        return cls([SpeedSegment(0.0, duration, speed)])

    @classmethod
    def sampled(
        cls, duration: float, speed_fn: Callable[[float], float], steps: int = 10,
    ) -> "SpeedCurve":
        """按归一化时间采样速度函数（``speed_fn(u)``，u 在 [0,1]）。"""
        steps = max(1, int(steps))
        segs: List[SpeedSegment] = []
        for i in range(steps):
            u0, u1 = i / steps, (i + 1) / steps
            mid = (u0 + u1) / 2.0
            segs.append(SpeedSegment(u0 * duration, u1 * duration,
                                     max(0.05, float(speed_fn(mid)))))
        return cls(segs)

    @classmethod
    def ramp(
        cls, duration: float, start_speed: float, end_speed: float, steps: int = 10,
    ) -> "SpeedCurve":
        """线性速度斜坡：start_speed → end_speed。"""
        return cls.sampled(
            duration, lambda u: start_speed + (end_speed - start_speed) * u, steps)

    @classmethod
    def from_preset(cls, name: str, duration: float, steps: int = 10) -> "SpeedCurve":
        """内置预设曲线。

        - ``rush``    匀速起步 → 越来越快（卡点冲刺收尾）
        - ``slowmo``  快速冲入 → 平滑减速到慢动作
        - ``hero``    慢 → 快 → 慢（高光镜头感）
        - ``punch``   常态里插两个快切脉冲
        """
        name = (name or "").lower()
        if name == "rush":
            return cls.sampled(duration, lambda u: 1.0 + 2.2 * (u ** 1.6), steps)
        if name == "slowmo":
            return cls.sampled(duration, lambda u: 2.4 - 2.0 * min(1.0, u * 1.15), steps)
        if name == "hero":
            def f(u: float) -> float:
                # 两端快(1.8)、中段慢(0.4)
                return 0.4 + 1.4 * abs(2 * u - 1) ** 1.3
            return cls.sampled(duration, f, steps)
        if name == "punch":
            def g(u: float) -> float:
                return 2.6 if (0.18 < u < 0.30 or 0.55 < u < 0.67) else 1.0
            return cls.sampled(duration, g, max(steps, 20))
        raise ValueError(f"未知速度曲线预设: {name}")


PRESETS = ("rush", "slowmo", "hero", "punch")


# ----------------------------------------------------------------- 滤镜
def atempo_chain(speed: float, lo: float = 0.5, hi: float = 2.0) -> List[float]:
    """把速度拆成若干落在 [lo, hi] 的 atempo 因子（兼容老版 ffmpeg）。"""
    speed = max(0.05, float(speed))
    factors: List[float] = []
    s = speed
    while s > hi:
        factors.append(hi)
        s /= hi
    while s < lo:
        factors.append(lo)
        s /= lo
    factors.append(round(s, 6))
    return factors


def _atempo_str(speed: float) -> str:
    return ",".join(f"atempo={f:.6f}" for f in atempo_chain(speed))


def build_filter_complex(
    curve: SpeedCurve, has_audio: bool = True, fps: Optional[float] = None,
) -> Tuple[str, str]:
    """生成 filter_complex 与输出标签列表。

    Returns:
        (filter_complex 字符串, ["[outv]", "[outa]"] 或 ["[outv]"])
    """
    parts: List[str] = []
    for i, seg in enumerate(curve.segments):
        trim = f"trim=start={seg.start:.4f}:end={seg.end:.4f}"
        v = f"[0:v]{trim},setpts=(PTS-STARTPTS)/{seg.speed:.6f}"
        if fps and fps > 0:
            v += f",fps={fps:g}"
        v += f"[v{i}]"
        parts.append(v)
        if has_audio:
            a = (f"[0:a]atrim=start={seg.start:.4f}:end={seg.end:.4f},"
                 f"asetpts=PTS-STARTPTS,{_atempo_str(seg.speed)}[a{i}]")
            parts.append(a)

    n = len(curve.segments)
    if has_audio:
        label = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{label}concat=n={n}:v=1:a=1[outv][outa]")
        return ";".join(parts), ["[outv]", "[outa]"]
    label = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{label}concat=n={n}:v=1[outv]")
    return ";".join(parts), ["[outv]"]


def has_audio_stream(video_path: str) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def probe_duration(video_path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def apply_speed_curve(
    input_path: str,
    output_path: str,
    curve: SpeedCurve,
    crf: int = 20,
    preset: str = "medium",
    keep_audio: bool = True,
) -> str:
    """把速度曲线应用到视频（视频+音频分别处理，逐段对齐保证同步）。"""
    if not curve.segments:
        raise ValueError("速度曲线没有有效段")
    src_audio = keep_audio and has_audio_stream(input_path)
    fc, maps = build_filter_complex(curve, has_audio=src_audio)

    cmd = ["ffmpeg", "-y", "-i", input_path, "-filter_complex", fc]
    for m in maps:
        cmd += ["-map", m]
    cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]
    if src_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd.append(output_path)

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or "")[-600:]
        raise RuntimeError(f"变速渲染失败:\n{tail}")
    return output_path


def speed_curve_for_video(
    video_path: str, preset: str = "rush", steps: int = 10,
    speed: Optional[float] = None,
) -> SpeedCurve:
    """按视频实际时长构造曲线（speed 给定时退化为整段等比变速）。"""
    duration = probe_duration(video_path)
    if duration <= 0:
        raise RuntimeError(f"无法读取视频时长: {video_path}")
    if speed is not None:
        return SpeedCurve.constant(duration, speed)
    return SpeedCurve.from_preset(preset, duration, steps=steps)
