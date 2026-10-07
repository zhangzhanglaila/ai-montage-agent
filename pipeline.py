"""
真正的 Montage Pipeline
输入：视频文件 + BGM
输出：混剪视频
"""

import subprocess
import json
import math
import os
import random
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import numpy as np

# 添加项目路径
import sys
sys.path.insert(0, str(Path(__file__).parent))

from packages.core_types.models import (
    Shot, Beat, TimelineEntry, HighlightScore,
    BeatAnalysis, MotionData, MusicSegment
)


def _safe_float(value, default: float) -> float:
    """把可能为 -inf / inf / nan 的数值安全转换为有限 float。

    loudnorm 在遇到静音或极低电平输入时，会把 input_i 等字段输出为 "-inf"，
    直接带入第二遍滤镜会导致 ffmpeg 报错，因此这里统一兜底。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _parse_ratio(value, default: float = 9 / 16) -> float:
    """解析比例字符串（"9:16" / "0.5625"）为宽高比 float。"""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else default
    s = str(value or "").strip()
    if not s:
        return default
    try:
        if ":" in s:
            a, b = s.split(":", 1)
            fa, fb = float(a), float(b)
            return fa / fb if fb else default
        v = float(s)
        return v if v > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


# 常见目标比例 -> 文件名后缀（直接用浮点数拼名可读性差，这里给可读标签）
_ASPECT_SUFFIX = {
    16 / 9: "16x9",
    9 / 16: "9x16",
    1.0: "1x1",
    4 / 3: "4x3",
    3 / 4: "3x4",
    4 / 5: "4x5",
    21 / 9: "21x9",
    2.35: "235x100",
}


def _ratio_suffix(aspect: float, default_aspect: float = 9 / 16) -> str:
    """把宽高比 float 转成文件名后缀，如 9:16 -> "_9x16"。"""
    try:
        aspect = float(aspect)
    except (TypeError, ValueError):
        aspect = default_aspect
    if not math.isfinite(aspect) or aspect <= 0:
        aspect = default_aspect
    for ratio, label in _ASPECT_SUFFIX.items():
        if abs(aspect - ratio) < 1e-3:
            return f"_{label}"
    # 非常见比例：退化为可读的十进制（小数点转 p，避免文件名带点）
    txt = f"{aspect:.4f}".rstrip("0").rstrip(".").replace(".", "p")
    return f"_{txt}"


def _fmt_ts(ts) -> str:
    """时间戳 -> 本地时间字符串。"""
    if not ts:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


def _print_task_table(rows) -> None:
    """打印任务列表。"""
    if not rows:
        print("  （无任务）")
        return
    print(f"  {'ID':14s} {'KIND':8s} {'STATUS':9s} {'TRY':5s} {'PROG':6s} "
          f"{'UPDATED':15s} MESSAGE")
    for t in rows:
        tries = f"{t.get('attempts', 0)}/{t.get('max_attempts', 0)}"
        print(f"  {t['id'][:14]:14s} {str(t.get('kind', ''))[:8]:8s} "
              f"{t['status']:9s} {tries:5s} {t.get('progress', 0):>4d}%  "
              f"{_fmt_ts(t.get('updated_at')):15s} "
              f"{str(t.get('message') or '')[:38]}")


def _print_task_detail(task) -> None:
    """打印单个任务详情。"""
    for key in ("id", "kind", "status", "progress", "attempts", "max_attempts",
                "priority", "output_path", "error", "message",
                "created_at", "updated_at", "started_at", "finished_at"):
        value = task.get(key)
        if key.endswith("_at"):
            value = _fmt_ts(value)
        print(f"  {key:13s}: {value}")
    payload = task.get("payload") or {}
    brief = json.dumps(payload, ensure_ascii=False)
    print(f"  {'payload':13s}: {brief[:400]}")


def _probe_media_duration(path: str) -> float:
    """获取媒体时长（秒），失败返回 0。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


class ShotDetector:
    """镜头检测 - 使用 FFmpeg scene detect"""

    def __init__(self, cache_dir: str = "cache/shots"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def detect(self, video_path: str, threshold: float = 0.3, video_index: int = 0) -> List[Shot]:
        """检测镜头并切割"""
        print(f"  检测镜头: {video_path}")

        if not Path(video_path).exists():
            print(f"  文件不存在: {video_path}")
            return []

        # 获取视频时长
        try:
            duration = self._get_duration(video_path)
        except Exception as e:
            print(f"  获取时长失败: {e}")
            return []

        if duration <= 0:
            print(f"  视频时长为0: {video_path}")
            return []

        # 使用 FFmpeg 检测场景变化
        scenes = self._detect_scenes(video_path, threshold)

        # 如果没检测到场景变化，整个视频作为一个镜头
        if not scenes:
            scenes = [(0.0, duration)]

        # 切割视频
        shots = []
        for i, (start, end) in enumerate(scenes):
            if end - start < 0.1:
                continue

            shot_id = video_index * 10000 + i
            shot_path = self.cache_dir / f"shot_{shot_id:05d}.mp4"

            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", video_path,
                "-to", str(end - start),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(shot_path)
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode != 0 or not shot_path.exists():
                    print(f"  镜头切割失败: shot_{i:04d}")
                    continue
            except subprocess.TimeoutExpired:
                print(f"  镜头切割超时: shot_{i:04d}")
                continue

            shots.append(Shot(
                shot_id=shot_id,
                start_time=start,
                end_time=end,
                duration=end - start,
                file_path=str(shot_path),
                source_video=video_path,
            ))

        # 如果切割全部失败，直接复制原视频作为单镜头
        if not shots and duration > 0:
            print(f"  切割失败，使用原视频作为单镜头")
            shots.append(Shot(
                shot_id=video_index * 10000,
                start_time=0.0,
                end_time=duration,
                duration=duration,
                file_path=video_path,
                source_video=video_path,
            ))

        print(f"  检测到 {len(shots)} 个镜头")
        return shots

    def _detect_scenes(self, video_path: str, threshold: float) -> List[tuple]:
        """使用 FFmpeg 检测场景变化"""
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-"
        ]

        result = subprocess.run(cmd, capture_output=True)

        # 解析时间点（用 bytes 处理避免编码问题）
        import re
        stderr_text = result.stderr.decode('utf-8', errors='ignore') if result.stderr else ""
        times = []
        for line in stderr_text.split('\n'):
            if 'pts_time' in line:
                match = re.search(r'pts_time:(\d+\.?\d*)', line)
                if match:
                    times.append(float(match.group(1)))

        # 生成场景列表
        scenes = []
        for i in range(len(times)):
            start = times[i]
            end = times[i + 1] if i + 1 < len(times) else self._get_duration(video_path)
            scenes.append((start, end))

        # 添加第一个场景
        if scenes and scenes[0][0] > 0:
            scenes.insert(0, (0.0, scenes[0][0]))

        return scenes

    def _get_duration(self, video_path: str) -> float:
        """获取视频时长"""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True)
        return float(result.stdout.decode('utf-8', errors='ignore').strip())


class MotionAnalyzer:
    """运动分析 - 使用 OpenCV 光流（采样帧优化）"""

    def analyze(self, shot_path: str, max_frames: int = 30) -> MotionData:
        """分析镜头运动（只分析前 max_frames 帧）"""
        try:
            import cv2

            cap = cv2.VideoCapture(shot_path)
            if not cap.isOpened():
                return MotionData()

            # 读取第一帧
            ret, prev_frame = cap.read()
            if not ret:
                return MotionData()

            prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)

            magnitudes = []
            frame_count = 0
            while frame_count < max_frames:
                ret, frame = cap.read()
                if not ret:
                    break

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                # 计算光流
                flow = cv2.calcOpticalFlowFarneback(
                    prev_gray, gray,
                    None, 0.5, 3, 15, 3, 5, 1.2, 0
                )

                # 计算运动幅度
                magnitude = np.sqrt(flow[..., 0]**2 + flow[..., 1]**2)
                magnitudes.append(np.mean(magnitude))

                prev_gray = gray
                frame_count += 1

            cap.release()

            if not magnitudes:
                return MotionData()

            avg_magnitude = np.mean(magnitudes)
            shake = np.std(magnitudes)

            return MotionData(
                magnitude=float(avg_magnitude),
                shake=float(shake)
            )

        except ImportError:
            # 如果没有 OpenCV，返回默认值
            return MotionData(magnitude=0.5, shake=0.1)


class BeatAnalyzer:
    """节拍分析 - 使用 librosa"""

    def analyze(self, audio_path: str) -> BeatAnalysis:
        """分析 BGM"""
        print(f"  分析 BGM: {audio_path}")

        try:
            import librosa

            # 加载音频
            y, sr = librosa.load(audio_path, sr=22050)
            duration = len(y) / sr

            # 检测节拍 + onset 强度
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
            beat_times = librosa.frames_to_time(beat_frames, sr=sr)

            # 如果没有检测到节拍，生成默认节拍
            if len(beat_times) == 0:
                print("  警告: 未检测到节拍，使用默认节拍")
                beat_interval = 0.5  # 120 BPM
                beat_times = np.arange(0, duration, beat_interval)

            # 计算 onset 强度（用于区分强拍/弱拍）
            onset_env = librosa.onset.onset_strength(y=y, sr=sr)
            onset_times = librosa.frames_to_time(np.arange(len(onset_env)), sr=sr)

            # 计算能量
            rms = librosa.feature.rms(y=y)[0]
            rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr)
            rms_normalized = (rms - rms.min()) / (rms.max() - rms.min() + 1e-8)

            # 为每个节拍分类强/弱拍
            # 强拍：onset 强度高于中位数 + 能量高于中位数
            onset_at_beats = []
            for bt in beat_times:
                idx = np.argmin(np.abs(onset_times - bt))
                onset_at_beats.append(onset_env[idx])

            onset_median = np.median(onset_at_beats) if onset_at_beats else 0
            energy_median = np.median(rms_normalized)

            # 找高潮段
            threshold = np.percentile(rms_normalized, 80)
            drops = []
            in_drop = False
            drop_start = 0

            for i, (t, e) in enumerate(zip(rms_times, rms_normalized)):
                if e > threshold and not in_drop:
                    in_drop = True
                    drop_start = t
                elif e <= threshold and in_drop:
                    in_drop = False
                    if t - drop_start > 1.0:
                        drops.append({"start": drop_start, "end": t})

            # 生成能量曲线
            energy_curve = [
                {"time": float(t), "energy": float(e)}
                for t, e in zip(rms_times[::100], rms_normalized[::100])
            ]

            # 创建节拍对象（区分强拍/弱拍）
            beats = []
            for i, t in enumerate(beat_times):
                onset_val = onset_at_beats[i] if i < len(onset_at_beats) else 0

                # 获取该时刻的能量
                e_idx = np.argmin(np.abs(rms_times - t))
                energy_val = rms_normalized[e_idx]

                # 分类：onset 强度高 + 能量高 = 强拍
                is_strong = (onset_val > onset_median * 1.2) or (energy_val > energy_median * 1.3)

                beat_type = "strong" if is_strong else "normal"
                strength = min(float(onset_val / (onset_median + 1e-8)), 2.0) / 2.0

                beats.append(Beat(
                    time=float(t),
                    strength=strength,
                    beat_type=beat_type,
                ))

            return BeatAnalysis(
                beats=beats,
                tempo=float(tempo),
                energy_curve=energy_curve,
                drops=drops,
                segments=[],
                duration=duration
            )

        except ImportError:
            # 如果没有 librosa，返回模拟数据
            print("  警告: librosa 未安装，使用模拟节拍")
            return self._mock_analysis()

    def _mock_analysis(self) -> BeatAnalysis:
        """模拟分析结果"""
        beats = [
            Beat(time=i * 0.5, strength=0.8, beat_type="normal")
            for i in range(20)
        ]
        return BeatAnalysis(
            beats=beats,
            tempo=120.0,
            energy_curve=[],
            drops=[],
            segments=[],
            duration=10.0
        )


class HighlightScorer:
    """高光评分 - 5 维评分系统

    维度：运动幅度(0.25)、镜头多样性(0.20)、人脸情感(0.25)、镜头运动(0.15)、音频(0.15)
    """

    def __init__(self):
        self.weights = {
            "motion": 0.25,
            "shot_diversity": 0.20,
            "face_emotion": 0.25,
            "camera_movement": 0.15,
            "audio": 0.15,
        }

    def score(self, shot: Shot, motion: MotionData) -> float:
        """计算高光分数（5 维）"""
        w = self.weights

        # 1. 运动幅度（sigmoid 归一化）
        motion_score = self._sigmoid(motion.magnitude, center=3.0, scale=1.0)

        # 2. 镜头时长多样性（1-3 秒最佳）
        duration_score = self._duration_score(shot.duration)

        # 3. 人脸情感（如果有 action 信息）
        face_score = self._face_emotion_score(shot)

        # 4. 镜头运动（shake + zoom）
        camera_score = self._camera_movement_score(motion)

        # 5. 音频能量（如果可用）
        audio_score = self._audio_score(shot)

        total = (
            w["motion"] * motion_score +
            w["shot_diversity"] * duration_score +
            w["face_emotion"] * face_score +
            w["camera_movement"] * camera_score +
            w["audio"] * audio_score
        )

        return min(max(total, 0.0), 1.0)

    def _sigmoid(self, x: float, center: float = 3.0, scale: float = 1.0) -> float:
        """Sigmoid 归一化"""
        import math
        return 1.0 / (1.0 + math.exp(-scale * (x - center)))

    def _duration_score(self, duration: float) -> float:
        """时长分数（1-3 秒最佳）"""
        if 1.0 <= duration <= 3.0:
            return 1.0
        elif duration < 1.0:
            return duration
        else:
            return max(0.3, 1.0 - (duration - 3.0) * 0.1)

    def _face_emotion_score(self, shot: Shot) -> float:
        """人脸情感分数（基于 action 信息）"""
        if not shot.actions:
            return 0.5  # 默认中性

        # 情感权重映射
        emotion_weights = {
            "angry": 0.9, "fear": 0.8, "surprise": 0.7,
            "sad": 0.6, "happy": 0.5, "disgust": 0.4, "neutral": 0.1,
        }

        # 从 actions 中提取情感
        for action in shot.actions:
            action_lower = action.lower() if isinstance(action, str) else ""
            for emotion, weight in emotion_weights.items():
                if emotion in action_lower:
                    return weight

        return 0.3  # 无情感信息

    def _camera_movement_score(self, motion: MotionData) -> float:
        """镜头运动分数（shake + zoom 复合）"""
        shake_score = min(motion.shake / 3.0, 1.0) * 0.4
        zoom_score = min(abs(motion.zoom - 1.0) / 0.5, 1.0) * 0.3
        direction_score = min(
            (abs(motion.direction_x) + abs(motion.direction_y)) / 5.0, 1.0
        ) * 0.3
        return shake_score + zoom_score + direction_score

    def _audio_score(self, shot: Shot) -> float:
        """音频能量分数"""
        # 如果 shot 有额外的音频信息，使用它
        # 否则基于时长估算
        if shot.duration < 0.5:
            return 0.2
        elif shot.duration > 10:
            return 0.4
        return 0.6


class BeatSyncEngine:
    """卡点同步引擎 - 镜头时长严格量化到整拍，结束点对齐节拍边界"""

    # 转场类型映射
    TRANSITION_TYPES = ["cut", "fade", "dissolve", "wipe", "flash", "zoom", "blur"]

    def sync(
        self,
        shots: List[Shot],
        beats: List[Beat],
        style: str = "dynamic",
        transition_pattern: dict = None,
    ) -> List[TimelineEntry]:
        """
        将镜头与节拍同步
        核心逻辑：
        - 每个镜头时长 = N 拍（N 由高光分数和拍类型决定）
        - 结束点严格对齐节拍边界
        - 速度因子范围 0.8~1.2（不破坏节奏感）
        """
        if not shots or not beats:
            return []

        # 计算节拍间隔（BPM 的倒数）
        if len(beats) >= 2:
            # 用中位数间隔，避免异常值
            intervals = [beats[i+1].time - beats[i].time for i in range(len(beats)-1)]
            beat_interval = sorted(intervals)[len(intervals) // 2]
        else:
            beat_interval = 0.5

        # 风格参数：拍数范围 + 速度范围
        style_cfg = self._get_style_config(style)

        timeline = []
        current_beat_idx = 0

        for shot in shots:
            if current_beat_idx >= len(beats):
                break

            # 当前节拍时间点
            beat_time = beats[current_beat_idx].time
            beat = beats[current_beat_idx]

            # 根据高光分数 + 拍类型决定拍数（N 拍）
            beat_count = self._calc_beat_count(
                shot.highlight_score, beat.beat_type, style_cfg
            )

            # 时长 = N 拍 * 拍间隔（严格量化）
            duration = beat_count * beat_interval

            # 速度因子：基于高光分数微调（范围 0.8~1.2）
            speed_factor = self._calc_speed(
                shot.highlight_score, beat.beat_type, style_cfg
            )

            # 转场：支持模板系统
            transition_type = self._pick_transition(beat, shot, style, transition_pattern)
            if transition_pattern and transition_type != "cut":
                # 用模板的时长比例
                dur_ratio = transition_pattern.get(
                    "strong_duration_ratio" if beat.beat_type == "strong" else "weak_duration_ratio",
                    0.5
                )
                transition_duration = beat_interval * dur_ratio
            else:
                transition_duration = beat_interval * 0.5 if transition_type != "cut" else 0.0

            # 创建时间线条目
            entry = TimelineEntry(
                shot_id=shot.shot_id,
                shot_path=shot.file_path or "",
                start_time=beat_time,
                end_time=beat_time + duration,
                duration=duration,
                beat_time=beat_time,
                speed_factor=speed_factor,
                transition_type=transition_type,
                transition_duration=transition_duration
            )

            timeline.append(entry)

            # 移动到下一个节拍位置（跳过已用的拍数）
            current_beat_idx += beat_count

        # 按时间排序
        timeline.sort(key=lambda x: x.start_time)

        return timeline

    def _get_style_config(self, style: str) -> Dict[str, Any]:
        """获取风格配置"""
        configs = {
            "dynamic": {
                "strong_beats": (1, 2),      # 强拍镜头：1-2 拍
                "normal_beats": (2, 3),       # 普通镜头：2-3 拍
                "weak_beats": (3, 4),         # 弱拍镜头：3-4 拍
                "speed_range": (0.85, 1.15),  # 速度范围
                "strong_speed": 0.9,          # 强拍速度（略慢，强调）
                "weak_speed": 1.1,            # 弱拍速度（略快，紧凑）
            },
            "calm": {
                "strong_beats": (2, 3),
                "normal_beats": (3, 4),
                "weak_beats": (4, 6),
                "speed_range": (0.9, 1.1),
                "strong_speed": 0.95,
                "weak_speed": 1.05,
            },
            "intense": {
                "strong_beats": (1, 1),
                "normal_beats": (1, 2),
                "weak_beats": (2, 3),
                "speed_range": (0.8, 1.2),
                "strong_speed": 0.85,
                "weak_speed": 1.15,
            },
        }
        return configs.get(style, configs["dynamic"])

    def _calc_beat_count(
        self, highlight_score: float, beat_type: str, cfg: Dict
    ) -> int:
        """
        根据高光分数和拍类型决定镜头占几拍
        高光分数越高 + 拍越强 -> 拍数越少（镜头越短，节奏越快）
        """
        if beat_type == "strong":
            # 强拍：高分镜头用最少拍数
            if highlight_score > 0.7:
                beat_range = cfg["strong_beats"]
            else:
                beat_range = (cfg["strong_beats"][0] + 1, cfg["strong_beats"][1] + 1)
        elif beat_type == "weak":
            # 弱拍：用较多拍数
            beat_range = cfg["weak_beats"]
        else:
            # 普通拍
            beat_range = cfg["normal_beats"]

        # 在范围内随机选（增加变化感）
        min_b, max_b = beat_range
        if min_b == max_b:
            return min_b
        # 高分偏短，低分偏长
        if highlight_score > 0.6:
            return min_b
        elif highlight_score > 0.3:
            return (min_b + max_b) // 2
        else:
            return max_b

    def _calc_speed(
        self, highlight_score: float, beat_type: str, cfg: Dict
    ) -> float:
        """
        计算速度因子（范围 0.8~1.2）
        强拍略慢（强调），弱拍略快（紧凑）
        """
        base_speed = cfg["strong_speed"] if beat_type == "strong" else cfg["weak_speed"]

        # 根据高光分数微调
        if highlight_score > 0.7:
            # 高光镜头：强拍更慢，弱拍更快
            adjustment = 0.05 if beat_type == "strong" else -0.05
        else:
            adjustment = 0.0

        speed = base_speed + adjustment

        # 限制范围
        min_speed, max_speed = cfg["speed_range"]
        return max(min_speed, min(speed, max_speed))

    def _pick_transition(self, beat: Beat, shot: Shot, style: str,
                         transition_pattern: dict = None) -> str:
        """根据拍类型、风格和转场模板选择转场效果"""
        if transition_pattern:
            from packages.montage_engine.src.transition_patterns import pick_transition
            motion = getattr(shot, 'motion_score', 0.0) or 0.0
            trans_type, _ = pick_transition(transition_pattern, beat.beat_type, motion)
            return trans_type

        # 旧逻辑：根据风格随机选
        if beat.beat_type == "strong":
            if style == "intense":
                return random.choice(["flash", "zoom", "cut"])
            elif style == "calm":
                return random.choice(["fade", "dissolve"])
            else:
                return random.choice(["cut", "fade", "flash"])

        if style == "intense":
            return "cut"
        elif style == "calm":
            return random.choice(["dissolve", "fade"])
        else:
            return random.choice(["cut", "dissolve"])


class VideoRenderer:
    """视频渲染器"""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(
        self,
        timeline: List[TimelineEntry],
        bgm_path: str,
        output_path: str
    ) -> str:
        """渲染最终视频（支持转场效果）"""
        print(f"  渲染视频...")

        # 收集有效的镜头（按时间排序）
        valid_entries = [e for e in timeline if e.shot_path and Path(e.shot_path).exists()]
        if not valid_entries:
            raise ValueError("没有有效的镜头文件")

        temp_files = []

        # Step 1: 截取 + 调整速度（合并为一次 FFmpeg 调用）
        import shutil
        prepared = []
        for idx, entry in enumerate(valid_entries):
            clip_path = self.output_dir / f"clip_{idx}.mp4"
            self._prepare_clip(entry.shot_path, str(clip_path), entry.duration, entry.speed_factor)
            temp_files.append(clip_path)
            prepared.append(str(clip_path))

        # Step 2: 检查是否有非 cut 转场
        has_transitions = any(
            getattr(e, "transition_type", "cut") != "cut" and getattr(e, "transition_duration", 0) > 0
            for e in valid_entries
        )

        if has_transitions and len(prepared) > 1:
            # 使用 xfade 滤镜应用转场
            concat_video = self._render_with_transitions(prepared, valid_entries)
        else:
            # 简单拼接（无转场或全部是 cut）
            concat_video = self._render_concat(prepared)

        temp_files.append(concat_video)

        # Step 3: BGM 混音（BGM 为主 + 原始音频低音量叠加）
        # 先归一化 BGM 音量（-14 LUFS 标准响度）
        normalized_bgm = self.output_dir / "bgm_normalized.wav"
        self._normalize_bgm(bgm_path, str(normalized_bgm))
        temp_files.append(normalized_bgm)

        # 混音：BGM 100% + 原始音频 20%
        cmd = [
            "ffmpeg", "-y",
            "-i", str(concat_video),
            "-i", str(normalized_bgm),
            "-filter_complex",
            "[0:a]volume=0.2[orig];[1:a]volume=1.0[bgm];[orig][bgm]amix=inputs=2:duration=shortest:dropout_transition=2[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # 如果原始视频没有音频轨道，回退到只用 BGM
            print(f"  混音失败（可能无原音轨），使用纯 BGM")
            cmd = [
                "ffmpeg", "-y",
                "-i", str(concat_video),
                "-i", str(normalized_bgm),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                output_path
            ]
            subprocess.run(cmd, capture_output=True, check=True)

        # 清理临时文件
        for f in temp_files:
            Path(f).unlink(missing_ok=True)

        print(f"  输出: {output_path}")
        return output_path

    def _render_concat(self, video_paths: List[str]) -> str:
        """简单拼接（无转场）"""
        concat_file = self.output_dir / "concat.txt"
        with open(concat_file, 'w') as f:
            for path in video_paths:
                f.write(f"file '{Path(path).resolve().as_posix()}'\n")

        concat_video = self.output_dir / "concat.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(concat_video)
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        concat_file.unlink(missing_ok=True)
        return str(concat_video)

    def _render_with_transitions(self, video_paths: List[str], entries: List[TimelineEntry]) -> str:
        """渲染视频：cut 用 concat，非 cut 连续段用 xfade"""
        if len(video_paths) < 2:
            return self._render_concat(video_paths)

        durations = [self._get_duration(p) for p in video_paths]
        W, H = 1280, 720

        # 统一分辨率和帧率（**保留音轨**：转场段的 acrossfade 需要它）
        scaled_files = []
        for i, path in enumerate(video_paths):
            scaled = self.output_dir / f"scaled_{i}.mp4"
            cmd = ["ffmpeg", "-y", "-i", path]
            if self._has_audio(path):
                maps = ["-map", "0:v:0", "-map", "0:a:0"]
            else:
                # 源无音轨时补静音轨，保证各段流结构一致（concat -c copy 才不会错位）
                cmd.extend(["-f", "lavfi", "-i",
                            "anullsrc=channel_layout=stereo:sample_rate=44100"])
                maps = ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            cmd.extend([
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30",
                *maps,
                "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                str(scaled)
            ])
            subprocess.run(cmd, capture_output=True, check=True)
            scaled_files.append(str(scaled))

        # 按转场类型分段：连续非 cut 的 clips 用 xfade，cut 处断开
        segments = []
        current_seg = [0]
        for i in range(1, len(video_paths)):
            entry = entries[i] if i < len(entries) else entries[-1]
            trans_type = getattr(entry, "transition_type", "cut")
            trans_dur = getattr(entry, "transition_duration", 0.5)
            if trans_type != "cut" and trans_dur > 0:
                current_seg.append(i)
            else:
                segments.append(current_seg)
                current_seg = [i]
        segments.append(current_seg)

        # 渲染每个 segment
        segment_videos = []
        xfade_temps = []
        for seg in segments:
            if len(seg) == 1:
                segment_videos.append(scaled_files[seg[0]])
            else:
                sv = self._xfade_segment(scaled_files, seg, durations, entries)
                segment_videos.append(sv)
                xfade_temps.append(sv)

        # concat 所有 segments
        if len(segment_videos) == 1:
            result_path = segment_videos[0]
        else:
            result_path = self._render_concat(segment_videos)

        # 清理临时文件（注意：单段 xfade 时 result_path 本身就是临时文件，不能删）
        keep = str(result_path)
        for f in scaled_files + xfade_temps:
            if str(f) != keep:
                Path(f).unlink(missing_ok=True)

        return result_path

    def _xfade_segment(self, scaled_files: List[str], indices: List[int], durations: List[float], entries: List[TimelineEntry]) -> str:
        """对一组连续非-cut clips 应用 xfade 链（**视频 xfade + 音频 acrossfade 同步缩短**）"""
        seg_dur = [durations[i] for i in indices]
        filter_parts = []
        accum_dur = seg_dur[0]      # 视频流累计时长（xfade 语义：输出 = offset + 后段时长）
        accum_audio = seg_dur[0]    # 音频流累计时长（acrossfade 语义：输出 = 前段 + 后段 - d）
        current_label = "[0:v]"
        current_audio = "[0:a]"

        xfade_map = {
            "fade": "fade", "dissolve": "dissolve",
            "wipe": "wipeleft", "flash": "fadeblack",
            "zoom": "circlecrop", "blur": "fadeblack",
            "motion_blur": "fadeblack",
            "wipeleft": "wipeleft", "wiperight": "wiperight",
            "wipeup": "wipeup", "wipedown": "wipedown",
            "slideleft": "slideleft", "slideright": "slideright",
            "slideup": "slideup", "slidedown": "slidedown",
            "smoothleft": "smoothleft", "smoothright": "smoothright",
            "smoothup": "smoothup", "smoothdown": "smoothdown",
            "circleopen": "circleopen", "circleclose": "circleclose",
            "circlecrop": "circlecrop", "rectcrop": "rectcrop",
            "diagtl": "diagtl", "diagtr": "diagtr",
            "diagbl": "diagbl", "diagbr": "diagbr",
            "vertopen": "vertopen", "vertclose": "vertclose",
            "horzopen": "horzopen", "horzclose": "horzclose",
            "radial": "radial", "pixelize": "pixelize",
            "distance": "distance", "fadeblack": "fadeblack",
            "fadewhite": "fadewhite",
            "hlslice": "hlslice", "hrslice": "hrslice",
            "vuslice": "vuslice", "vdslice": "vdslice",
            "shake": "fadeblack",
        }

        for j in range(1, len(indices)):
            i = indices[j]
            entry = entries[i] if i < len(entries) else entries[-1]
            trans_dur = getattr(entry, "transition_duration", 0.5)
            trans_type = getattr(entry, "transition_type", "fade")
            xfade_type = xfade_map.get(trans_type, "fade")

            # 转场起点 = 上一段末尾 - 转场时长（xfade 语义：转场结束时正好落在接缝上）
            offset = accum_dur - trans_dur
            if offset < 0:
                offset = max(0.1, accum_dur * 0.5)

            # 音频交叉淡化时长不得 ≥ 任一被叠合的片段长度（acrossfade 会报错）
            fade_d = min(trans_dur, 0.9 * min(seg_dur[j], accum_audio))
            fade_d = max(0.05, fade_d)

            accum_dur = offset + seg_dur[j]
            accum_audio = accum_audio + seg_dur[j] - fade_d

            filter_parts.append(
                f"{current_label}[{j}:v]xfade=transition={xfade_type}:"
                f"duration={trans_dur}:offset={offset:.3f}[out{j}]"
            )
            filter_parts.append(
                f"{current_audio}[{j}:a]acrossfade=d={fade_d:.3f}:c1=tri:c2=tri[a{j}]"
            )
            current_label = f"[out{j}]"
            current_audio = f"[a{j}]"

        # 音频可能因转场误差比画面略长/略短：apad 补尾 + 输出 -t 精确对齐，避免
        # concat demuxer 按流各自拼接时音画逐段漂移
        filter_parts.append(f"{current_audio}apad[aout]")

        seg_video = self.output_dir / f"xfade_seg_{indices[0]}.mp4"
        cmd = ["ffmpeg", "-y"]
        for i in indices:
            cmd.extend(["-i", scaled_files[i]])
        cmd.extend([
            "-filter_complex", ";".join(filter_parts),
            "-map", current_label,
            "-map", "[aout]",
            "-t", f"{accum_dur:.3f}",
            "-c:v", "libx264", "-crf", "23", "-preset", "medium",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            str(seg_video)
        ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"  xfade segment 失败，回退 concat")
            return self._render_concat([scaled_files[i] for i in indices])
        return str(seg_video)

    def _get_duration(self, video_path: str) -> float:
        """获取视频时长"""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            return float(result.stdout.strip())
        except ValueError:
            print(f"  [警告] 无法获取时长: {video_path}，使用默认值 2.0s")
            return 2.0

    @staticmethod
    def _has_audio(path: str) -> bool:
        """探测输入是否含音轨。

        用途：决定要不要补一条静音轨。**所有 clip 的流结构必须一致**，
        否则 concat demuxer 的 `-c copy` 会因为某段缺音轨而错位/失败。
        """
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return bool((result.stdout or "").strip())

    @staticmethod
    def _atempo_chain(speed: float) -> str:
        """把任意倍速拆成多级 atempo（单级只支持 [0.5, 2.0]，超出必须串联）"""
        speed = max(0.1, float(speed))
        parts: List[str] = []
        s = speed
        while s > 2.0:
            parts.append("atempo=2.0")
            s /= 2.0
        while s < 0.5:
            parts.append("atempo=0.5")
            s /= 0.5
        parts.append(f"atempo={s:.6f}")
        return ",".join(parts)

    def _prepare_clip(self, input_path: str, output_path: str, duration: float, speed: float = 1.0, width: int = 1280, height: int = 720):
        """截取 + 调整速度 + 统一分辨率（合并为一次 FFmpeg 调用，**保留原音轨**）

        原实现两个分支都带 `-an`，把原音轨丢在第一步 → 后续 concat 出的视频没有音轨
        → Step3 混音的 `[0:a]` 不存在、静默回退成「纯 BGM」，原片人声/现场声永远进不了成片。
        现改为：
        - 源有声 → map 原音轨；变速时用 `atempo`（多级串联）同步变速，`apad` 补齐到画面长度；
        - 源无声 → 用 `anullsrc` 补一条静音轨，保证所有 clip 流结构一致。
        """
        has_audio = self._has_audio(input_path)
        cmd = ["ffmpeg", "-y", "-i", input_path]
        if not has_audio:
            cmd.extend(["-f", "lavfi", "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=44100"])

        # 统一分辨率 + 可选变速
        scale_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        if speed != 1.0:
            pts = 1.0 / speed
            cmd.extend(["-vf", f"setpts={pts}*PTS,{scale_filter}"])
        else:
            cmd.extend(["-vf", scale_filter])

        cmd.extend(["-map", "0:v:0"])
        if has_audio:
            cmd.extend(["-map", "0:a:0"])
            af = f"{self._atempo_chain(speed)},apad" if speed != 1.0 else "apad"
            cmd.extend(["-af", af])
        else:
            cmd.extend(["-map", "1:a:0"])

        cmd.extend([
            "-t", str(duration),
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            output_path,
        ])
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=60)
        except subprocess.TimeoutExpired:
            print(f"  [警告] clip 准备超时: {input_path}")

    def _trim_video(self, input_path: str, output_path: str, duration: float):
        """截取视频到指定时长"""
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)

    def _normalize_bgm(self, input_path: str, output_path: str, target_lufs: float = -14.0):
        """
        BGM 音量归一化（两遍 loudnorm）
        目标：-14 LUFS（流媒体标准响度）
        """
        # 第一遍：分析
        cmd_analyze = [
            "ffmpeg", "-y", "-i", input_path,
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"
        ]
        result = subprocess.run(cmd_analyze, capture_output=True, text=True)

        # 解析分析结果
        import json as _json
        stderr = result.stderr or ""
        measured_i = -14.0
        measured_tp = -1.5
        measured_lra = 11.0
        measured_thresh = -24.0
        offset = 0.0

        try:
            # 从 stderr 中提取 JSON 块
            json_start = stderr.rfind('{')
            json_end = stderr.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                stats = _json.loads(stderr[json_start:json_end])
                measured_i = _safe_float(stats.get("input_i"), -14.0)
                measured_tp = _safe_float(stats.get("input_tp"), -1.5)
                measured_lra = _safe_float(stats.get("input_lra"), 11.0)
                measured_thresh = _safe_float(stats.get("input_thresh"), -24.0)
                offset = _safe_float(stats.get("target_offset"), 0.0)
        except (ValueError, KeyError, _json.JSONDecodeError):
            pass

        # 静音/极低电平输入兜底：double-pass 需要有限且有效的测量值，
        # 若判定为静音（≤ -70 LUFS）则降级为单遍 loudnorm。
        if measured_i <= -70.0:
            af = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
        else:
            af = (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
                f":measured_I={measured_i}"
                f":measured_TP={measured_tp}"
                f":measured_LRA={measured_lra}"
                f":measured_thresh={measured_thresh}"
                f":offset={offset}"
                f":linear=true"
            )

        # 第二遍：应用归一化
        cmd_normalize = [
            "ffmpeg", "-y", "-i", input_path,
            "-af", af,
            "-ar", "44100",
            output_path
        ]
        proc = subprocess.run(cmd_normalize, capture_output=True)
        if proc.returncode != 0:
            # 最终兜底：仅重采样到 44.1kHz，不做响度归一化，保证流程不中断
            subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, "-ar", "44100",
                 "-af", "volume=1.0", output_path],
                capture_output=True, check=True,
            )

    def _adjust_speed(self, input_path: str, output_path: str, speed: float):
        """调整视频速度"""
        pts = 1.0 / speed
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", f"setpts={pts}*PTS",
            "-an",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)


class MontagePipeline:
    """混剪 Pipeline - 真正的端到端流程"""

    def __init__(self, cache_dir: str = "cache", output_dir: str = "output"):
        self.cache_dir = cache_dir
        self.output_dir = output_dir

        self.shot_detector = ShotDetector(f"{cache_dir}/shots")
        self.motion_analyzer = MotionAnalyzer()
        self.beat_analyzer = BeatAnalyzer()
        self.scorer = HighlightScorer()
        self.sync_engine = BeatSyncEngine()
        self.renderer = VideoRenderer(output_dir)

        # 时间线数据（供导出使用）
        self._last_timeline = []
        self._last_bgm_path = ""
        self._last_total_duration = 0.0

    @staticmethod
    def _interval_gap(a: Shot, b: Shot) -> float:
        """两个镜头在源时间轴上相隔多少秒（源区间重叠时为负数）"""
        return max(a.start_time, b.start_time) - min(a.end_time, b.end_time)

    def select_highlight_shots(
        self,
        all_shots: List[Shot],
        max_per_video: int = 15,
        min_shot_gap: float = 1.0,
        max_total: int = 50,
        verbose: bool = True,
    ) -> List[Shot]:
        """按源分组挑高光镜头，并保证同一源内入选镜头的**源区间互不相接**。

        原实现（已修）是 `min_gap = 20; if abs(shot.shot_id - last_id) >= min_gap`，
        有三个毛病，导致成片里出现"重复镜头"：
          1) 只跟「上一个入选者」比，不跟**所有已选镜头**比。而循环是按分数降序走的，
             于是 shot2[1.00-2.00] 与 shot4[2.13-3.00] 这种源上紧挨着的镜头，只要各自
             离"上一个"够远就都能入选 —— 实测 15 个镜头里 9 对源起点差 <2s，
             id71/75/77 三个还全挤在 32.87~36.37 这 3.5s 窗口里。
          2) 用「起点差」也不够：id61[26.40-28.43] 与 id62[28.43-29.63] 起点虽差 2.03s，
             但两段源区间**严丝合缝相接**，成片里就是同一段连续画面出现两次。
             所以这里比较的是**区间空隙** `max(起点)-min(终点)`，而不是起点差。
          3) shot_id 差值本身不可靠：shot_id = video_index*10000 + i，而 ShotDetector.detect()
             会 continue 掉 <0.1s 的短场景，id 并不连续，跨源比较更是毫无意义。

        Args:
            all_shots: 所有检测到的镜头
            max_per_video: 每个源最多取多少个
            min_shot_gap: 同一源内任两个入选镜头源区间之间要空出的秒数；源很长时会按
                源跨度自适应放大（`span / (max_per_video * 2.5)`）。设为 0 关闭该约束。
            max_total: 交替排列后最终截取的镜头总数上限
            verbose: 是否打印每个源的选取情况
        """
        video_shots: Dict[str, List[Shot]] = {}
        for shot in all_shots:
            key = shot.source_video or "unknown"
            video_shots.setdefault(key, []).append(shot)

        # 每个源：按分数降序贪心，要求与**所有**已选镜头都拉开 min_sep
        per_video_lists = []
        for key, shots in video_shots.items():
            span = max(s.end_time for s in shots) - min(s.start_time for s in shots)
            if min_shot_gap and float(min_shot_gap) > 0:
                # 2.5 是留给贪心的松弛量，保证一般还能选到接近 max_per_video 个
                min_sep = max(float(min_shot_gap), span / (max_per_video * 2.5))
            else:
                min_sep = 0.0  # 显式关闭该约束（回到"尽量多选"，可能出现重复镜头）
            sorted_group = sorted(shots, key=lambda s: (-s.highlight_score, s.start_time))
            selected: List[Shot] = []
            for shot in sorted_group:
                if len(selected) >= max_per_video:
                    break
                if all(self._interval_gap(shot, s) >= min_sep for s in selected):
                    selected.append(shot)
            if verbose:
                print(f"  {Path(key).name}: 取 {len(selected)} 个镜头"
                      f"（源跨度 {span:.1f}s，最小源区间间隔 {min_sep:.2f}s）")
            per_video_lists.append(selected)

        # 交替排列不同源的镜头（避免连续同源）
        balanced_shots: List[Shot] = []
        max_len = max((len(lst) for lst in per_video_lists), default=0)
        for i in range(max_len):
            for lst in per_video_lists:
                if i < len(lst):
                    balanced_shots.append(lst[i])

        # 截取上限（保持交替排列，不按分数重新排序）
        max_shots = min(max_total, len(balanced_shots))
        if verbose:
            print(f"  选择 Top {max_shots} 高光镜头（共 {len(all_shots)} 个，"
                  f"来自 {len(video_shots)} 个视频）")
        return balanced_shots[:max_shots]

    def run(
        self,
        video_paths: List[str],
        bgm_path: str,
        style: str = "dynamic",
        output_name: str = "final.mp4",
        threshold: float = 0.2,
        color_preset: str = None,
        enable_reframe: bool = False,
        reframe_aspect: float = 9 / 16,
        reframe_method: str = "subject",
        enable_ducking: bool = False,
        duck_level_db: float = -12.0,
        enable_harmonize: bool = False,
        harmonize_strength: float = 0.5,
        transition_pattern: dict = None,
        enable_sfx: bool = False,
        narration_audio: str = None,
        narration_duck: bool = True,
        min_shot_gap: float = 1.0,
    ) -> str:
        """
        运行完整混剪流程

        Args:
            video_paths: 视频文件路径列表
            bgm_path: BGM 文件路径
            style: 风格 (dynamic, calm, intense)
            output_name: 输出文件名
            threshold: 镜头检测灵敏度 (0.01~1.0，越小切得越细)
            color_preset: 颜色分级预设 (如 cinematic, vibrant, vintage 等)
            enable_reframe: 是否启用自动竖屏裁切
            reframe_aspect: 竖屏裁切目标宽高比 (默认 9:16)
            enable_ducking: 是否启用对话闪避
            duck_level_db: 闪避降低分贝数 (默认 -12dB)
            enable_harmonize: 是否启用色彩协调
            harmonize_strength: 色彩协调强度 (0.0~1.0)
            transition_pattern: 转场模板
            enable_sfx: 是否启用音效卡点（按强拍/剪辑点叠加音效）
            narration_audio: 旁白音频路径（有则叠加到成片并自动闪避 BGM）
            narration_duck: 旁白时段是否自动压低 BGM
            min_shot_gap: 同一源内任两个入选镜头的**源区间之间**要空出的秒数（默认 1.0），
                用于避免近邻镜头造成的「重复镜头」观感；源很长时会按源跨度自适应放大。
                调大可进一步减少重复（本片实测：1.0→13 镜头、2.5→12、3.0→10），
                设 0 可关闭该约束（回到「尽量多选」的旧行为，可能出现重复镜头）。

        Returns:
            输出文件路径
        """
        print("=" * 50)
        print("AI Montage Agent - 开始混剪")
        print("=" * 50)

        # Step 1: 检测镜头
        print(f"\n[1/6] 检测镜头 (阈值: {threshold})...")
        if not video_paths:
            raise ValueError("没有可用的视频文件，请检查搜索关键词或上传文件")

        all_shots = []
        for video_idx, video_path in enumerate(video_paths):
            print(f"  处理视频: {Path(video_path).name}")
            shots = self.shot_detector.detect(video_path, threshold=threshold, video_index=video_idx)
            all_shots.extend(shots)

        if not all_shots:
            raise ValueError("没有检测到任何镜头，视频文件可能损坏或格式不支持")

        # Step 2: 分析运动
        print("\n[2/6] 分析运动...")
        for shot in all_shots:
            motion = self.motion_analyzer.analyze(shot.file_path)
            shot.motion_score = motion.magnitude

        # Step 3: 评分高光
        print("\n[3/6] 评分高光...")
        for shot in all_shots:
            motion = MotionData(magnitude=shot.motion_score)
            shot.highlight_score = self.scorer.score(shot, motion)

        # Step 4: 分析 BGM
        print("\n[4/6] 分析 BGM...")
        beat_analysis = self.beat_analyzer.analyze(bgm_path)

        # Step 5: 卡点同步
        print("\n[5/6] 卡点同步...")
        # 按 source_video 分组，每视频最多取 top N 个镜头（带源区间间隔约束）
        max_per_video = 15
        top_shots = self.select_highlight_shots(
            all_shots, max_per_video=max_per_video, min_shot_gap=min_shot_gap)
        if not top_shots:
            raise ValueError("没有可用的高光镜头")
        timeline = self.sync_engine.sync(top_shots, beat_analysis.beats, style, transition_pattern)

        if not timeline:
            raise ValueError("时间线为空")

        # 存储时间线数据（供导出使用）
        self._last_timeline = timeline
        self._last_bgm_path = bgm_path
        self._last_total_duration = sum(e.duration for e in timeline)

        # 后处理：色彩协调（渲染前对镜头做，避免双重渲染）
        if enable_harmonize:
            print("\n  色彩协调...")
            try:
                from packages.video_enhancement.src.color_harmonizer import harmonize_clips
                shot_paths = [e.shot_path for e in timeline if e.shot_path and Path(e.shot_path).exists()]
                if shot_paths:
                    harmonized = [p.replace(".mp4", "_harmonized.mp4") for p in shot_paths]
                    harmonize_clips(shot_paths, harmonized, strength=harmonize_strength)
                    for i, entry in enumerate(timeline):
                        if entry.shot_path and i < len(harmonized) and Path(harmonized[i]).exists():
                            entry.shot_path = harmonized[i]
            except Exception as e:
                print(f"  色彩协调失败（跳过）: {e}")

        # Step 6: 渲染
        print("\n[6/6] 渲染输出...")
        out_p = Path(output_name)
        if out_p.parent and str(out_p.parent) != ".":
            output_path = str(out_p)
        else:
            output_path = str(Path(self.output_dir) / output_name)
        result = self.renderer.render(timeline, bgm_path, output_path)

        # 后处理：对话闪避
        if enable_ducking:
            print("\n  对话闪避...")
            try:
                from packages.video_enhancement.src.dialogue_ducking import duck_audio
                ducked_path = output_path.replace(".mp4", "_ducked.mp4")
                duck_audio(result, bgm_path, ducked_path, duck_level_db=duck_level_db)
                if Path(ducked_path).exists():
                    import os
                    os.replace(ducked_path, result)
            except Exception as e:
                print(f"  对话闪避失败（跳过）: {e}")

        # 后处理：颜色分级
        if color_preset and color_preset != "none":
            print(f"\n  颜色分级: {color_preset}...")
            try:
                from packages.video_enhancement.src.color_grading import apply_color_grade
                graded_path = output_path.replace(".mp4", "_graded.mp4")
                apply_color_grade(result, graded_path, preset=color_preset)
                if Path(graded_path).exists():
                    import os
                    os.replace(graded_path, result)
            except Exception as e:
                print(f"  颜色分级失败（跳过）: {e}")

        # 后处理：自动裁切（默认竖屏，目标比例可配）
        if enable_reframe:
            _suffix = _ratio_suffix(reframe_aspect)
            print(f"\n  自动裁切 (目标比例 {_suffix.lstrip('_').replace('x', ':')})...")
            try:
                from packages.video_enhancement.src.auto_reframe import auto_reframe
                reframed_path = output_path.replace(".mp4", f"{_suffix}.mp4")
                auto_reframe(result, reframed_path, target_aspect=reframe_aspect,
                             method=reframe_method)
                if Path(reframed_path).exists():
                    result = reframed_path
            except Exception as e:
                print(f"  竖屏裁切失败（跳过）: {e}")

        # 后处理：音效卡点
        if enable_sfx:
            print("\n  音效卡点...")
            try:
                from packages.sound_engine import SfxEngine
                sfx_engine = SfxEngine()
                cues = sfx_engine.plan(beat_analysis.beats, style=style)
                if cues:
                    sfx_path = output_path.replace(".mp4", "_sfx.mp4")
                    sfx_engine.mix(result, cues, sfx_path)
                    if Path(sfx_path).exists():
                        result = sfx_path
                    print(f"  已叠加 {len(cues)} 个音效")
                else:
                    print("  未规划出音效点（跳过）")
            except Exception as e:
                print(f"  音效卡点失败（跳过）: {e}")

        # 后处理：配音旁白（叠加人声轨，旁白时段自动闪避 BGM）
        if narration_audio:
            print("\n  叠加配音旁白...")
            try:
                from packages.voice_engine import mix_narration
                narr_path = output_path.replace(".mp4", "_voiced.mp4")
                mix_narration(result, narration_audio, narr_path, duck=narration_duck)
                if Path(narr_path).exists():
                    result = narr_path
                print("  旁白叠加完成")
            except Exception as e:
                print(f"  配音叠加失败（跳过）: {e}")

        print("\n" + "=" * 50)
        print("混剪完成!")
        print(f"输出文件: {result}")
        print(f"镜头数量: {len(all_shots)}")
        print(f"节拍数量: {len(beat_analysis.beats)}")
        print(f"BPM: {beat_analysis.tempo:.1f}")
        print("=" * 50)

        return result


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="AI Montage Agent")
    parser.add_argument("--movies", nargs="+", help="本地视频文件路径")
    parser.add_argument("--query", type=str, help="搜索关键词，自动下载素材（与 --movies 二选一）")
    parser.add_argument("--source", type=str, default="playphrase",
                        choices=["playphrase", "quodb", "bilibili",
                                 "youtube", "dailymotion", "douyin", "ixigua", "acfun", "vimeo",
                                 "yarn", "zhaotaici"],
                        help="素材来源（默认 playphrase）")
    parser.add_argument("--clip-limit", type=int, default=20, help="最大下载片段数，默认 20（仅 --query 模式）")
    parser.add_argument("--bgm", help="BGM 文件路径（可选，不传则自动从B站搜索下载）")
    parser.add_argument("--bgm-query", help="BGM 搜索关键词（可选，不传则按 --style 风格自动搜索）")
    parser.add_argument("--style", default="dynamic", choices=["dynamic", "calm", "intense"],
                        help="基础风格: dynamic(动感) / calm(舒缓) / intense(高燃)")
    parser.add_argument("--style-preset", type=str, default=None,
                        help="风格预设模板(如 action/wedding/cinematic/gaming 等)，会覆盖 --style 和 --enhance")
    parser.add_argument("--output", default="final.mp4", help="输出文件名")
    parser.add_argument("--threshold", type=float, default=0.2,
                        help="镜头检测灵敏度 0.01~1.0，越小切得越细（默认 0.2，混剪推荐 0.1~0.2）")
    parser.add_argument("--min-shot-gap", type=float, default=1.0,
                        help="同一源内任两个入选镜头的源区间之间要空出的秒数（默认 1.0）；"
                             "调大可减少重复镜头，0 表示关闭该约束")
    # 新增功能参数
    parser.add_argument("--prompt", type=str, help="自然语言描述，如 '做一个30秒的漫威高燃混剪'")
    parser.add_argument("--subtitles", type=str, default="none",
                        choices=["none", "tiktok", "youtube", "minimal", "karaoke",
                                 "bold", "cinematic", "bilingual"],
                        help="字幕风格（默认 none；karaoke 为逐字高亮，需 whisper）")
    parser.add_argument("--subtitle-model", type=str, default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="字幕 Whisper 模型规格（默认 base，越大越准越慢）")
    parser.add_argument("--translate", type=str, default=None,
                        help="字幕翻译目标语言（如 en/ja/ko）；给定时输出双语字幕")
    parser.add_argument("--translate-backend", type=str, default="auto",
                        choices=["auto", "llm", "deep"],
                        help="翻译后端: auto(LLM优先)/llm/deep(需 deep-translator)")
    parser.add_argument("--subtitle-lang", type=str, default=None,
                        help="字幕源语言（zh/en/ja...，默认自动检测）")
    parser.add_argument("--enhance", nargs="*", default=[],
                        help="视频增强选项: stabilize denoise color-grade auto-reframe")
    parser.add_argument("--reframe-aspect", type=str, default="16:9",
                        help="auto-reframe 目标比例，如 16:9 / 9:16 / 3:4 / 1:1（默认 16:9，即不默认出竖屏）")
    parser.add_argument("--reframe-method", type=str, default="subject",
                        choices=["subject", "center"],
                        help="auto-reframe 裁切方式：subject 主体跟随（默认）/ center 静态居中")
    parser.add_argument("--export-timeline", type=str,
                        choices=["edl", "csv", "json", "xml", "otio"],
                        help="导出时间轴格式")
    parser.add_argument("--sfx", action="store_true",
                        help="按节拍自动叠加音效（强拍 impact / 剪辑点 whoosh）")
    parser.add_argument("--narrate", type=str,
                        help="配音旁白主题（LLM 自动写解说词并合成语音）")
    parser.add_argument("--script", type=str,
                        help="自定义旁白脚本文件（每行一句，优先于 --narrate）")
    parser.add_argument("--narrate-sec", type=int, default=30,
                        help="旁白目标时长（秒，仅 --narrate 模式）")
    parser.add_argument("--voice", type=str, default=None,
                        help="TTS 音色（如 zh-CN-YunxiNeural，默认女声晓晓）")
    parser.add_argument("--no-duck", action="store_true",
                        help="旁白时段不自动压低 BGM")
    parser.add_argument("--cover", action="store_true",
                        help="自动生成封面图（挑最佳帧 + 叠加标题）")
    parser.add_argument("--cover-title", type=str, default=None,
                        help="封面主标题（默认取 --prompt 或输出文件名）")
    parser.add_argument("--cover-subtitle", type=str, default="",
                        help="封面副标题")
    parser.add_argument("--cover-size", type=str, default="16:9",
                        choices=["16:9", "9:16", "1:1", "4:3", "3:4"],
                        help="封面比例，默认 16:9")
    parser.add_argument("--cover-style", type=str, default="bold",
                        choices=["bold", "minimal", "cinematic"],
                        help="封面文字版式，默认 bold")
    parser.add_argument("--speed-curve", type=str, default=None,
                        choices=["rush", "slowmo", "hero", "punch"],
                        help="变速曲线预设: rush 渐快冲刺 / slowmo 冲入慢放 / hero 慢快慢 / punch 脉冲")
    parser.add_argument("--speed", type=float, default=None,
                        help="整段等比变速（如 2.0 双速、0.5 半速），优先于 --speed-curve")
    parser.add_argument("--speed-steps", type=int, default=10,
                        help="变速曲线分段数（越多越平滑，默认 10）")
    parser.add_argument("--audio-mix", type=str, default=None, metavar="SPEC.json",
                        help="多轨混音计划(JSON)：人声/BGM/音效独立增益+自动闪避；"
                             "spec 内不写 video 时自动以本次成片为底")
    parser.add_argument("--webui", action="store_true", help="启动 WebUI 界面")
    parser.add_argument("--index-shots", type=str, nargs="?", const="cache/index/shot_index.json",
                        default=None, metavar="PATH",
                        help="为 --movies 建立语义镜头索引并保存（默认 cache/index/shot_index.json）")
    parser.add_argument("--query-shots", type=str, default=None,
                        help="语义检索镜头，如 '明亮的画面'（需先建索引；配合 --index-shots 指定索引路径）")
    parser.add_argument("--index-top-k", type=int, default=10, help="检索返回条数（默认 10）")
    parser.add_argument("--index-export", type=str, default=None,
                        help="把检索命中的镜头切片导出到指定目录")
    parser.add_argument("--embedder", type=str, default="auto",
                        choices=["auto", "clip", "heuristic"],
                        help="镜头编码器：auto(优先CLIP)/clip/heuristic(离线)")

    # ---- 任务队列管理（持久化任务表，不跑混剪）----
    parser.add_argument("--task-db", type=str, default=None, metavar="PATH",
                        help="任务库路径（默认 cache/tasks.db 或环境变量 MONTAGE_TASK_DB）")
    parser.add_argument("--task-list", action="store_true", help="列出持久化任务")
    parser.add_argument("--task-status", type=str, default=None,
                        choices=["pending", "queued", "running", "done", "error", "canceled"],
                        help="配合 --task-list：只显示某状态的任务")
    parser.add_argument("--task-show", type=str, default=None, metavar="ID",
                        help="查看单个任务详情")
    parser.add_argument("--task-retry", type=str, default=None, metavar="ID",
                        help="手动重试任务（清零尝试次数并重新入队）")
    parser.add_argument("--task-cancel", type=str, default=None, metavar="ID",
                        help="取消任务")
    parser.add_argument("--task-resume", action="store_true",
                        help="把卡在 running 的超时任务重新入队（进程中断后断点续跑）")
    parser.add_argument("--task-stale", type=float, default=600.0,
                        help="--task-resume 的超时判定秒数（默认 600）")

    args = parser.parse_args()

    # 风格预设模板处理
    if args.style_preset:
        from packages.video_enhancement.src.style_templates import get_pipeline_params
        preset_params = get_pipeline_params(args.style_preset)
        args.style = preset_params.get("style", args.style)
        if preset_params.get("stabilize") and "stabilize" not in args.enhance:
            args.enhance.append("stabilize")
        if preset_params.get("color_preset"):
            args._color_preset = preset_params["color_preset"]
            if "color-grade" not in args.enhance:
                args.enhance.append("color-grade")
        if preset_params.get("enable_harmonize"):
            args._enable_harmonize = True
        if preset_params.get("enable_ducking"):
            args._enable_ducking = True
        if preset_params.get("enable_reframe"):
            args._enable_reframe = True
        print(f"  风格预设: {args.style_preset} -> style={args.style}, color={getattr(args, '_color_preset', 'default')}")

    # WebUI 模式
    if args.webui:
        from packages.webui import start_webui
        print("启动 WebUI: http://localhost:8000")
        start_webui()
        return

    # 任务队列管理模式（只读/操作持久化任务表，不跑混剪）
    if any([args.task_list, args.task_show, args.task_retry,
            args.task_cancel, args.task_resume]):
        from packages.task_queue import get_store
        store = get_store(args.task_db)
        print(f"任务库: {store.db_path}")

        if args.task_show:
            task = store.get(args.task_show)
            if task is None:
                print(f"任务不存在: {args.task_show}")
                return 1
            _print_task_detail(task)
            return 0

        if args.task_retry:
            if store.get(args.task_retry) is None:
                print(f"任务不存在: {args.task_retry}")
                return 1
            store.retry(args.task_retry)
            print(f"已重新入队: {args.task_retry}（WebUI 的 worker 启动后会自动执行）")
            return 0

        if args.task_cancel:
            if store.get(args.task_cancel) is None:
                print(f"任务不存在: {args.task_cancel}")
                return 1
            store.cancel(args.task_cancel)
            print(f"已取消: {args.task_cancel}")
            return 0

        if args.task_resume:
            info = store.resume_stale(stale_seconds=args.task_stale)
            print(f"续跑: 重新入队 {info['resumed']} 个，"
                  f"重试耗尽转失败 {info['exhausted']} 个")
            return 0

        rows = store.list(status=args.task_status, limit=50)
        _print_task_table(rows)
        stats = store.stats()
        print("  统计: " + "  ".join(f"{k}={v}" for k, v in stats.items()))
        return 0

    # 镜头索引构建模式
    if args.index_shots:
        if not args.movies:
            parser.error("--index-shots 需要配合 --movies 指定素材")
        from packages.shot_index import get_embedder, ShotIndex
        for movie in args.movies:
            if not Path(movie).exists():
                print(f"错误: 视频文件不存在: {movie}")
                return
        emb = get_embedder(prefer=args.embedder)
        print(f"镜头编码器: {emb.name} (dim={emb.dim})")
        index = ShotIndex(embedder=emb)
        for movie in args.movies:
            print(f"\n索引: {movie}")
            index.build(movie)
        index.save(args.index_shots)
        print(f"\n索引完成：{len(index)} 个镜头 -> {args.index_shots}")
        return

    # 镜头语义检索模式
    if args.query_shots:
        from packages.shot_index import get_embedder, ShotIndex, ShotRetriever, format_hits
        index_path = args.index_shots or "cache/index/shot_index.json"
        if not Path(index_path).exists():
            print(f"索引不存在: {index_path}\n请先用 --movies xxx --index-shots {index_path} 建索引")
            return
        emb = get_embedder(prefer=args.embedder)
        index = ShotIndex.load(index_path, embedder=emb)
        retriever = ShotRetriever(index)
        print(f"\n索引: {index_path}（{len(index)} 镜头，编码器 {index.name}）")
        print(f"查询: 「{args.query_shots}」")
        hits = retriever.search(query=args.query_shots, top_k=args.index_top_k)
        print(format_hits(hits, show_frame=True))
        if args.index_export and hits:
            retriever.export_clips(hits, args.index_export)
        return

    # 校验参数：--movies 和 --query 二选一
    if not args.movies and not args.query:
        parser.error("请指定 --movies（本地视频）或 --query（搜索素材）")
    if args.movies and args.query:
        parser.error("--movies 和 --query 不能同时使用")

    # 获取视频路径
    if args.query:
        video_paths = _crawl_videos(args.query, args.source, args.clip_limit, parser)
        if not video_paths:
            return
    else:
        video_paths = args.movies
        for movie in video_paths:
            if not Path(movie).exists():
                print(f"错误: 视频文件不存在: {movie}")
                return

    # BGM 处理：本地文件 → 指定搜索 → 按风格自动搜索 → 默认 BGM
    bgm_path = args.bgm
    if args.bgm_query:
        # 用户指定了搜索关键词
        from packages.video_crawler.src.bgm_crawler import BgmCrawler
        bgm_crawler = BgmCrawler()
        bgm_paths = bgm_crawler.search_and_download(args.bgm_query, max_clips=1)
        if bgm_paths:
            bgm_path = bgm_paths[0]
            print(f"  使用 BGM: {bgm_path}")
        else:
            print(f"  BGM 搜索未找到结果，尝试按风格搜索...")
            bgm_path = None
    elif bgm_path and not Path(bgm_path).exists():
        print(f"  BGM 文件不存在: {bgm_path}，尝试自动搜索...")
        bgm_path = None

    # 没有 BGM → 按风格自动从 B站搜索真实音乐
    if not bgm_path:
        from packages.video_crawler.src.bgm_crawler import BgmCrawler
        bgm_crawler = BgmCrawler()
        print(f"  自动搜索 {args.style} 风格 BGM...")
        bgm_paths = bgm_crawler.search_and_download_by_style(args.style, max_clips=1)
        if bgm_paths:
            bgm_path = bgm_paths[0]
            print(f"  使用 BGM: {bgm_path}")
        else:
            # 最终兜底：使用合成默认 BGM
            from packages.video_crawler.src.default_bgm import get_default_bgm_path
            bgm_path = get_default_bgm_path(style=args.style)
            print(f"  搜索失败，使用合成默认 BGM ({args.style}): {bgm_path}")

    # LLM 自然语言控制
    if args.prompt:
        from packages.ai_director import CreativeDirector
        director = CreativeDirector()
        instructions = director.interpret_prompt(args.prompt)
        if instructions:
            # 从 LLM 指令中提取参数
            style_map = {"intense": "intense", "calm": "calm", "dynamic": "dynamic"}
            llm_speed = instructions.get("pacing", {}).get("speed", "dynamic")
            args.style = style_map.get(llm_speed, args.style)
            print(f"  AI 导演建议风格: {args.style}")

            # 自动设置调色
            effects = instructions.get("effects", {})
            color_preset = effects.get("color_grading", "")
            if color_preset and color_preset != "neutral":
                if "color-grade" not in args.enhance:
                    args.enhance.append("color-grade")
                print(f"  AI 导演建议调色: {color_preset}")

            # 自动设置字幕
            if args.subtitles == "none" and instructions.get("subtitles"):
                args.subtitles = instructions["subtitles"]

            # 自动设置目标时长
            target_dur = instructions.get("constraints", {}).get("target_duration_sec")
            if target_dur:
                print(f"  AI 导演建议时长: {target_dur}s")

    # 配音旁白：生成脚本 → TTS 合成
    narration_audio = None
    if args.narrate or args.script:
        from packages.voice_engine import ScriptWriter, TtsEngine

        writer = ScriptWriter()
        if args.script:
            lines = writer.from_file(args.script)
        else:
            lines = writer.write(args.narrate, target_sec=args.narrate_sec)
        script_text = ScriptWriter.to_text(lines)
        print(f"\n  旁白脚本 {len(lines)} 句，预计 {ScriptWriter.total_duration(lines)}s")
        for ln in lines:
            print(f"    · {ln.text}")

        if TtsEngine.available():
            try:
                tts = TtsEngine(voice=args.voice)
                narration_path = "output/narration.mp3"
                tts.synthesize(script_text, narration_path)
                narration_audio = narration_path
                print(f"  旁白音频: {narration_path}（音色: {tts.voice}）")
            except Exception as e:
                print(f"  配音合成失败（跳过配音）: {e}")
        else:
            print("  未安装 edge-tts，跳过配音（pip install edge-tts）")

    # 运行 pipeline
    pipeline = MontagePipeline()
    color_preset = getattr(args, '_color_preset', None)
    enable_harmonize = getattr(args, '_enable_harmonize', False)
    enable_ducking = getattr(args, '_enable_ducking', False)
    enable_reframe = getattr(args, '_enable_reframe', False)
    reframe_aspect = _parse_ratio(getattr(args, "reframe_aspect", "16:9"), default=16 / 9)
    reframe_method = getattr(args, "reframe_method", "subject")

    result_path = pipeline.run(
        video_paths, bgm_path, args.style, args.output,
        threshold=args.threshold,
        color_preset=color_preset if color_preset and "color-grade" in args.enhance else None,
        enable_harmonize=enable_harmonize,
        enable_ducking=enable_ducking,
        enable_reframe=enable_reframe,
        reframe_aspect=reframe_aspect,
        reframe_method=reframe_method,
        enable_sfx=args.sfx,
        narration_audio=narration_audio,
        narration_duck=not args.no_duck,
        min_shot_gap=args.min_shot_gap,
    )

    # 后处理：视频增强（只处理 pipeline.run() 未处理的项目）
    post_enhance = [e for e in args.enhance if e != "color-grade" or not color_preset]
    if post_enhance:
        _apply_enhancement(result_path, post_enhance, color_preset,
                           reframe_aspect=reframe_aspect, reframe_method=reframe_method)

    # 后处理：字幕压制（--translate 时输出双语字幕）
    sub_style = args.subtitles
    if args.translate and sub_style == "none":
        sub_style = "bilingual"
    if sub_style != "none":
        _apply_subtitles(
            result_path, sub_style, args.subtitle_model,
            translate=args.translate,
            source_lang=args.subtitle_lang or "auto",
            translate_backend=args.translate_backend,
        )

    # 后处理：变速曲线
    if args.speed is not None or args.speed_curve:
        try:
            from packages.montage_engine import SpeedCurve, apply_speed_curve, probe_duration

            dur = probe_duration(result_path)
            if args.speed is not None:
                curve = SpeedCurve.constant(dur, args.speed)
                label = f"整段 {args.speed}x"
            else:
                curve = SpeedCurve.from_preset(args.speed_curve, dur, steps=args.speed_steps)
                label = f"曲线 {args.speed_curve}"
            print(f"\n  应用变速（{label}）...")
            print(f"  {curve.describe()}")
            sped_path = result_path.replace(".mp4", "_speed.mp4")
            apply_speed_curve(result_path, sped_path, curve)
            if Path(sped_path).exists():
                result_path = sped_path
        except Exception as e:
            print(f"  变速失败（跳过）: {e}")

    # 后处理：多轨混音（人声/BGM/音效）
    if args.audio_mix:
        try:
            from packages.audio_mixer import AudioMixer, AudioTrack, mix_tracks_from_spec

            spec_path = Path(args.audio_mix)
            if not spec_path.exists():
                print(f"  混音计划不存在: {args.audio_mix}")
            else:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                spec.setdefault("video", result_path)
                # 先把 spec 里没有的时长补成视频时长，避免 loop 轨无限长
                if not spec.get("duration"):
                    try:
                        spec["duration"] = _probe_media_duration(result_path)
                    except Exception:
                        pass
                mixer = AudioMixer(
                    sample_rate=int(spec.get("sample_rate", 44100)),
                    channels=int(spec.get("channels", 2)),
                    duck_db=float(spec.get("duck_db", -12.0)),
                )
                tracks = [AudioTrack.from_dict(d) for d in spec.get("tracks", [])]
                print(f"\n  多轨混音（{len(tracks)} 轨）...")
                print(mixer.describe(tracks))
                mixed_path = result_path.replace(".mp4", "_mixed.mp4")
                mix_tracks_from_spec({**spec, "video": result_path}, mixed_path)
                if Path(mixed_path).exists():
                    import os
                    os.replace(mixed_path, result_path)
                    print("  混音完成!")
        except Exception as e:
            print(f"  多轨混音失败（跳过）: {e}")

    # 后处理：封面生成
    if args.cover:
        from packages.cover_engine import generate_cover

        cover_title = args.cover_title or args.prompt or Path(args.output).stem
        cover_out = str(Path(args.output).with_suffix("")) + f"_cover.png"
        print("\n  生成封面...")
        generate_cover(
            result_path,
            title=cover_title,
            subtitle=args.cover_subtitle,
            size=args.cover_size,
            style=args.cover_style,
            out_path=cover_out,
        )

    # 导出时间轴
    if args.export_timeline:
        _export_timeline(pipeline, args.export_timeline)


def _apply_enhancement(video_path: str, enhance_options: list, color_preset: str = None,
                       reframe_aspect: float = 16 / 9, reframe_method: str = "subject"):
    """对输出视频应用增强"""
    from packages.video_enhancement import enhance_video, stabilize_video, apply_color_grade

    temp_path = video_path + ".enhanced.mp4"
    enhanced = False

    if "stabilize" in enhance_options:
        print("\n  应用防抖...")
        stabilize_video(video_path, temp_path)
        import shutil
        shutil.move(temp_path, video_path)
        enhanced = True

    if "denoise" in enhance_options:
        print("  应用降噪...")
        enhance_video(video_path, temp_path, denoise=True)
        import shutil
        shutil.move(temp_path, video_path)
        enhanced = True

    if "color-grade" in enhance_options:
        preset = color_preset or "cinematic"
        print(f"  应用调色: {preset}")
        apply_color_grade(video_path, temp_path, preset=preset)
        import shutil
        shutil.move(temp_path, video_path)
        enhanced = True

    if "auto-reframe" in enhance_options:
        _label = _ratio_suffix(reframe_aspect).lstrip("_").replace("x", ":")
        print(f"  应用自动裁切 (目标 {_label} / {reframe_method})...")
        from packages.video_enhancement import auto_reframe
        auto_reframe(video_path, temp_path,
                     target_aspect=reframe_aspect, method=reframe_method)
        if Path(temp_path).exists():
            import shutil
            shutil.move(temp_path, video_path)
            enhanced = True

    if enhanced:
        print("  增强完成!")


def _apply_subtitles(
    video_path: str, style: str, model_size: str = "base",
    translate: str = None, source_lang: str = "auto",
    translate_backend: str = "auto",
):
    """对输出视频应用字幕（转录 + 可选翻译 + 压制）

    karaoke 等词级风格由 transcribe_and_burn 自动改用 JSON 中间格式，
    以保留逐字时间戳；bilingual / --translate 走 ASS 双行排版。
    """
    try:
        from packages.subtitle_engine import transcribe_and_burn

        extra = f", 翻译->{translate}" if translate else ""
        print(f"\n  生成字幕 (风格: {style}, 模型: {model_size}{extra})...")

        out_path = video_path.replace(".mp4", "_subtitled.mp4")
        transcribe_and_burn(
            video_path, style=style, output_path=out_path, model_size=model_size,
            translate=translate, source_lang=source_lang,
            translate_backend=translate_backend,
        )

        # 替换原文件
        import shutil
        shutil.move(out_path, video_path)
        print(f"  字幕烧录完成!")
    except ImportError as e:
        print(f"  字幕功能需要安装 whisper: pip install openai-whisper")
        print(f"  错误: {e}")
    except Exception as e:
        print(f"  字幕处理失败: {e}")


def _export_timeline(pipeline, format: str):
    """导出时间轴"""
    # OTIO 使用专用导出器
    if format == "otio":
        from packages.timeline_export.src.otio_exporter import export_otio_from_pipeline
        output_path = f"{pipeline.output_dir}/timeline.otio"
        export_otio_from_pipeline(pipeline, output_path)
        return

    from packages.timeline_export import TimelineExporter, Timeline, Clip

    # 从 pipeline 中收集的时间线数据构建 Timeline 对象
    timeline_entries = getattr(pipeline, '_last_timeline', [])
    bgm_path = getattr(pipeline, '_last_bgm_path', '')
    total_duration = getattr(pipeline, '_last_total_duration', 0.0)

    if not timeline_entries:
        print(f"\n  导出时间轴: 无时间线数据（需要先运行 pipeline）")
        return

    # 转换为 Clip 对象
    clips = []
    for entry in timeline_entries:
        clips.append(Clip(
            source_path=getattr(entry, 'shot_path', ''),
            start_time=getattr(entry, 'start_time', 0.0),
            duration=getattr(entry, 'duration', 0.0),
            timeline_start=getattr(entry, 'start_time', 0.0),
        ))

    timeline = Timeline(
        clips=clips,
        audio_path=bgm_path,
        total_duration=total_duration,
    )

    exporter = TimelineExporter(output_dir=pipeline.output_dir)
    exported = exporter.export(timeline, formats=[format])

    for fmt, path in exported.items():
        print(f"  导出 {fmt.upper()}: {path}")


def _crawl_videos(keyword: str, source: str, clip_limit: int, parser) -> list:
    """根据来源爬取视频"""
    if source == "bilibili":
        from packages.video_crawler.src.bilibili_crawler import BilibiliCrawler
        crawler = BilibiliCrawler()
    elif source in ("youtube", "dailymotion", "douyin", "ixigua", "acfun", "vimeo"):
        from packages.video_crawler.src.ytdlp_crawler import create_crawler
        try:
            crawler = create_crawler(source)
        except ValueError as e:
            parser.error(str(e))
    elif source in ("yarn", "playphrase", "quodb", "zhaotaici"):
        from packages.video_crawler.src.quote_crawler import create_quote_crawler
        try:
            crawler = create_quote_crawler(source)
        except ValueError as e:
            parser.error(str(e))
    else:
        parser.error(f"不支持的素材来源: {source}")

    paths = crawler.search_and_download(keyword, max_clips=clip_limit)
    if not paths:
        print("错误: 未下载到任何视频")
    return paths


if __name__ == "__main__":
    main()
