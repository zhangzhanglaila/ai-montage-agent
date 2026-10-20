"""镜头检测与运动分析。"""

import re
import subprocess
from pathlib import Path
from typing import List

import numpy as np

from packages.core_types.models import MotionData, Shot


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

            # ⚠️ 必须重编码，不能用 `-c copy`。
            # 原来写的是 `-ss <start> -i in -to <dur> -c copy`：copy 模式无法丢帧，
            # ffmpeg 只能从 `<= start` 的**关键帧**开始拷，于是镜头文件的**实际内容**
            # 比记录的 `start_time` 提前一整个 GOP —— 本片实测中位 -0.77s、最大 -4.3s
            # （源的关键帧间隔中位 1.2s、最大 2.0s）。后果是「标称上互相分离」的两个
            # 镜头，实际画面仍大面积重叠；源上同一时刻被最多 4~6 个镜头文件覆盖 →
            # 成片里看着就是"同一个片段用了 4 次"。
            # 重编码时 ffmpeg 会 seek 到关键帧后**解码并丢弃**到 start 之前的帧，切点精确。
            # 另外 `-to` 在"输入 -ss"语境下语义含糊，改用相对时长 `-t`。
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", video_path,
                "-t", str(end - start),
                "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                str(shot_path)
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=90)
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


