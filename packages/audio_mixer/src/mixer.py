"""多轨音频混音器

人声 / BGM / 音效（任意轨数）独立增益 + 时间偏移 + 循环 + 自动闪避。

相比 F1.2 的 `mix_narration`（只处理「成片音轨 + 旁白」两轨、且把整段旁白当
一个 duck 窗口），这里做成通用轨道模型：

* 每轨可设 ``gain_db``（独立音量）、``start``（延迟入场）、``loop``（BGM 循环铺满）；
* ``role == "voice"`` 的轨可**自动导出说话时段**（ffmpeg ``silencedetect`` 取反），
  用来对 ``role == "bgm"`` 的轨做区间闪避；
* 混音用 ``amix=inputs=N:normalize=0`` —— ``normalize=0`` 很关键，否则 amix 会把
  总电平除以轨数，我们设的增益就白设了；
* 闪避仍走 ``volume`` 的 ``enable='between(t,a,b)+...'`` 时间窗（不用
  ``sidechaincompress``，后者在被 ``apad`` 补长时输出时长不确定，易挂起）。

所有输入统一重采样为 ``sample_rate`` / 立体声 / fltp，避免 amix 因格式不一致失败。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_SR = 44100
ROLES = ("voice", "bgm", "sfx", "other")


# ------------------------------------------------------------------ 基础
def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe_duration(path: str) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", path])
    try:
        return float(r.stdout.decode().strip())
    except (ValueError, AttributeError):
        return 0.0


def has_audio(path: str) -> bool:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a:0",
              "-show_entries", "stream=codec_type", "-of", "csv=p=0", path])
    return bool(r.stdout.decode().strip())


# --------------------------------------------------------------- 轨道模型
@dataclass
class AudioTrack:
    """一条音频轨"""
    path: str
    role: str = "other"           # voice / bgm / sfx / other
    gain_db: float = 0.0
    start: float = 0.0            # 延迟入场（秒）
    loop: bool = False            # 循环铺满目标时长（BGM 常用）
    duck_db: Optional[float] = None   # 被闪避时的额外衰减（None 用全局默认）
    duck_windows: List[Tuple[float, float]] = field(default_factory=list)

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"未知轨道角色: {self.role}（可选 {ROLES}）")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "role": self.role, "gain_db": self.gain_db,
            "start": self.start, "loop": self.loop, "duck_db": self.duck_db,
            "duck_windows": [[round(a, 3), round(b, 3)] for a, b in self.duck_windows],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AudioTrack":
        return cls(
            path=d["path"], role=d.get("role", "other"),
            gain_db=float(d.get("gain_db", 0.0)), start=float(d.get("start", 0.0)),
            loop=bool(d.get("loop", False)), duck_db=d.get("duck_db"),
            duck_windows=[tuple(w) for w in d.get("duck_windows", [])],
        )


# ------------------------------------------------------------ 说话时段检测
def detect_active_windows(
    path: str,
    duration: float = 0.0,
    noise_db: float = -35.0,
    min_silence: float = 0.28,
    min_active: float = 0.10,
    merge_gap: float = 0.18,
) -> List[Tuple[float, float]]:
    """检测一段音频里「有声音」的时间窗（用于闪避）。

    做法：用 ``silencedetect`` 找静音段，取其补集；再做「合并近邻 / 丢弃过短」。
    """
    duration = duration or probe_duration(path)
    r = _run(["ffmpeg", "-hide_banner", "-i", path,
              "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
              "-f", "null", "-"])
    text = r.stderr.decode(errors="ignore")

    silences: List[Tuple[float, float]] = []
    cur_start: Optional[float] = None
    for m in re.finditer(r"silence_(start|end):\s*([0-9.]+)", text):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            cur_start = val
        elif cur_start is not None:
            silences.append((cur_start, val))
            cur_start = None
    if cur_start is not None:
        silences.append((cur_start, duration or cur_start))

    # 补集 = 有声时段
    active: List[Tuple[float, float]] = []
    cursor = 0.0
    for a, b in silences:
        if a - cursor > 0:
            active.append((cursor, a))
        cursor = max(cursor, b)
    if duration and cursor < duration:
        active.append((cursor, duration))
    if not silences:
        active = [(0.0, duration)] if duration > 0 else []

    # 合并近邻
    merged: List[List[float]] = []
    for a, b in active:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in merged if b - a >= min_active]


def merge_windows(windows: Sequence[Tuple[float, float]],
                  gap: float = 0.12) -> List[Tuple[float, float]]:
    """合并/排序时间窗。"""
    ws = sorted((float(a), float(b)) for a, b in windows if b > a)
    out: List[List[float]] = []
    for a, b in ws:
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


# ------------------------------------------------------------------ 混音器
class AudioMixer:
    """多轨音频混音器"""

    def __init__(self, sample_rate: int = DEFAULT_SR, channels: int = 2,
                 duck_db: float = -12.0):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.duck_db = float(duck_db)

    # --------------------------------------------------------- 滤镜构建
    def _input_args(self, track: AudioTrack) -> List[str]:
        args: List[str] = []
        if track.loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", track.path]
        return args

    def _fmt(self) -> str:
        layout = "stereo" if self.channels == 2 else "mono"
        return (f"aformat=sample_fmts=fltp:sample_rates={self.sample_rate}"
                f":channel_layouts={layout}")

    def build_filter_complex(
        self, tracks: Sequence[AudioTrack], duration: float,
    ) -> Tuple[str, str]:
        """生成 filter_complex 与输入参数。

        Returns:
            (filter_complex, 输出标签 "[aout]")
        """
        parts: List[str] = []
        labels: List[str] = []

        for i, t in enumerate(tracks):
            chain: List[str] = []
            if t.loop and duration > 0:
                chain.append(f"atrim=0:{duration:.4f}")
                chain.append("asetpts=PTS-STARTPTS")
            if t.start > 0:
                ms = int(round(t.start * 1000))
                chain.append(f"adelay={ms}|{ms}")
            if abs(t.gain_db) > 1e-9:
                chain.append(f"volume={t.gain_db}dB")

            # 闪避：被闪避轨 + 有闪避窗口
            duck_db = t.duck_db if t.duck_db is not None else self.duck_db
            windows = merge_windows(t.duck_windows)
            if windows and duck_db < 0:
                enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in windows)
                chain.append(f"volume=enable='{enable}':volume={duck_db}dB")

            chain.append(self._fmt())
            labels.append(f"[t{i}]")
            parts.append(f"[{i}:a]{','.join(chain)}[t{i}]")

        if len(labels) == 1:
            out = labels[0]
            parts.append(f"{out}anull[aout]")
        else:
            parts.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:"
                f"duration=longest[aout]")
        return ";".join(parts), "[aout]"

    # --------------------------------------------------------- 混音主流程
    def mix(
        self,
        tracks: Sequence[AudioTrack],
        output_path: str,
        duration: Optional[float] = None,
        auto_duck: bool = True,
        video_path: Optional[str] = None,
        crf: int = 20,
    ) -> str:
        """把多条音轨混成一个音频（或替换视频音轨）。

        Args:
            tracks: 音轨列表
            output_path: 输出文件（给了 video_path 则输出 mp4）
            duration: 目标时长；None 时取各轨最大值（loop 轨需显式给）
            auto_duck: 是否用 voice 轨自动探测说话时段来闪避 bgm 轨
            video_path: 若给出，则把混音结果作为该视频的音轨输出 mp4
            crf: 视频重编码质量（video_path 非空时生效）
        """
        tracks = list(tracks)
        if not tracks:
            raise ValueError("没有音轨")
        for t in tracks:
            if not Path(t.path).exists():
                raise FileNotFoundError(f"音轨不存在: {t.path}")

        # 自动闪避：voice 轨探测说话时段 -> 写进 bgm 轨
        if auto_duck:
            voice_windows: List[Tuple[float, float]] = []
            for t in tracks:
                if t.role == "voice":
                    voice_windows += detect_active_windows(t.path, duration or 0.0)
            voice_windows = merge_windows(voice_windows)
            if voice_windows:
                for t in tracks:
                    if t.role == "bgm" and not t.duck_windows:
                        t.duck_windows = list(voice_windows)
                print(f"  [混音] 检测到 {len(voice_windows)} 段人声，BGM 将在这些区间闪避")

        if duration is None or duration <= 0:
            duration = max((probe_duration(t.path) + t.start for t in tracks), default=0.0)

        fc, out_label = self.build_filter_complex(tracks, duration)

        cmd: List[str] = ["ffmpeg", "-y"]
        for t in tracks:
            cmd += self._input_args(t)
        if video_path:
            cmd += ["-i", video_path]

        cmd += ["-filter_complex", fc]
        if video_path:
            cmd += ["-map", f"{len(tracks)}:v", "-map", out_label]
            cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium"]
        else:
            cmd += ["-map", out_label]
        cmd += ["-c:a", "aac", "-b:a", "192k"]
        if duration > 0:
            cmd += ["-t", f"{duration:.3f}"]
        cmd.append(output_path)

        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not Path(output_path).exists():
            raise RuntimeError("多轨混音失败:\n"
                               + r.stderr.decode("utf-8", errors="ignore")[-900:])
        return output_path

    # --------------------------------------------------------- 计划描述
    def describe(self, tracks: Sequence[AudioTrack]) -> str:
        lines = [f"  音轨数 {len(tracks)}（采样率 {self.sample_rate}, {self.channels}ch）:"]
        for i, t in enumerate(tracks):
            bits = [f"{t.role}", f"gain={t.gain_db:+.1f}dB"]
            if t.start:
                bits.append(f"start={t.start:.2f}s")
            if t.loop:
                bits.append("loop")
            if t.duck_windows:
                dd = t.duck_db if t.duck_db is not None else self.duck_db
                bits.append(f"duck={dd:.1f}dB×{len(t.duck_windows)}段")
            lines.append(f"    [{i}] {' '.join(bits)}  {Path(t.path).name}")
        return "\n".join(lines)


# --------------------------------------------------------------- 便捷入口
def mix_tracks_from_spec(spec: Dict[str, Any], output_path: str) -> str:
    """按 JSON 计划混音。

    spec 形如::

        {
          "duration": 12.0,
          "sample_rate": 44100,
          "duck_db": -12.0,
          "auto_duck": true,
          "video": "final.mp4",              # 可选：替换该视频音轨
          "tracks": [
            {"path": "bgm.mp3", "role": "bgm", "gain_db": -8, "loop": true},
            {"path": "voice.mp3", "role": "voice", "gain_db": 2}
          ]
        }
    """
    tracks = [AudioTrack.from_dict(d) for d in spec.get("tracks", [])]
    mixer = AudioMixer(
        sample_rate=int(spec.get("sample_rate", DEFAULT_SR)),
        channels=int(spec.get("channels", 2)),
        duck_db=float(spec.get("duck_db", -12.0)),
    )
    return mixer.mix(
        tracks, output_path,
        duration=spec.get("duration"),
        auto_duck=bool(spec.get("auto_duck", True)),
        video_path=spec.get("video"),
    )


def analyze_audio(path: str) -> Dict[str, Any]:
    """探测一条音轨的基本信息（时长/峰值/平均电平），便于设置增益。"""
    r = _run(["ffmpeg", "-hide_banner", "-i", path,
              "-af", "volumedetect", "-f", "null", "-"])
    text = r.stderr.decode(errors="ignore")
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", text)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", text)
    return {
        "path": path,
        "duration": round(probe_duration(path), 3),
        "mean_db": float(mean.group(1)) if mean else None,
        "peak_db": float(peak.group(1)) if peak else None,
    }
