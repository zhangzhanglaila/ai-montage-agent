"""
Video Composer Module
视频合成器 - 将镜头、转场、音频合成为最终视频
"""

import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from .transition_engine import TransitionEngine, Transition, TransitionType

@dataclass
class CompositionConfig:
    """合成配置"""
    width: int = 1920
    height: int = 1080
    fps: int = 30
    codec: str = "libx264"
    preset: str = "medium"
    crf: int = 23
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "codec": self.codec,
            "preset": self.preset,
            "crf": self.crf,
            "audio_codec": self.audio_codec,
            "audio_bitrate": self.audio_bitrate
        }

class VideoComposer:
    """
    视频合成器
    将镜头、转场、音频合成为最终视频
    """

    def __init__(
        self,
        transition_engine: Optional[TransitionEngine] = None,
        ffmpeg_path: str = "ffmpeg",
        output_dir: str = "var/cache/temp"
    ):
        self.transition_engine = transition_engine or TransitionEngine(ffmpeg_path)
        self.ffmpeg_path = ffmpeg_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def compose(
        self,
        timeline: List[Dict[str, Any]],
        bgm_path: Optional[str] = None,
        output_path: str = "output.mp4",
        config: Optional[CompositionConfig] = None
    ) -> str:
        """
        根据时间线合成视频

        Args:
            timeline: 时间线条目列表
            bgm_path: BGM音频路径(可选)
            output_path: 输出视频路径
            config: 合成配置

        Returns:
            输出文件路径
        """
        config = config or CompositionConfig()

        # 1. 准备镜头片段
        prepared_clips = self._prepare_clips(timeline, config)

        # 2. 应用转场
        clips_with_transitions = self._apply_transitions(prepared_clips, timeline)

        # 3. 合成最终视频
        final_video = self._concatenate_clips(clips_with_transitions, config)

        # 4. 添加BGM
        if bgm_path:
            final_video = self._add_bgm(final_video, bgm_path, output_path, config)
        else:
            # 复制到输出路径
            Path(final_video).rename(output_path)
            final_video = output_path

        return final_video

    def _prepare_clips(
        self,
        timeline: List[Dict[str, Any]],
        config: CompositionConfig
    ) -> List[str]:
        """准备镜头片段"""

        prepared = []

        for i, entry in enumerate(timeline):
            shot_path = entry.get("file_path")
            if not shot_path:
                continue

            # 裁剪到计划的时长
            planned_duration = entry.get("planned_duration", entry.get("duration", 1.0))
            start_time = entry.get("start_time", 0)

            # 创建临时文件
            temp_path = str(self.output_dir / f"clip_{i:04d}.mp4")

            # 使用FFmpeg裁剪
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", shot_path,
                "-ss", str(start_time),
                "-t", str(planned_duration),
                "-vf", f"scale={config.width}:{config.height}:force_original_aspect_ratio=decrease,"
                       f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2",
                "-r", str(config.fps),
                "-c:v", config.codec,
                "-preset", "fast",
                "-an",  # 暂时移除音频
                temp_path
            ]

            subprocess.run(cmd, check=True, capture_output=True)
            prepared.append(temp_path)

        return prepared

    def _apply_transitions(
        self,
        clips: List[str],
        timeline: List[Dict[str, Any]]
    ) -> List[str]:
        """应用转场效果"""

        if len(clips) < 2:
            return clips

        result = [clips[0]]

        for i in range(1, len(clips)):
            # 获取转场配置
            transition_config = timeline[i].get("transition", {})

            # 创建转场对象
            transition = Transition(
                type=TransitionType(transition_config.get("type", "fade")),
                duration=transition_config.get("duration", 0.5),
                direction=transition_config.get("direction", "right"),
                intensity=transition_config.get("intensity", 1.0)
            )

            # 应用转场
            output_path = str(self.output_dir / f"transition_{i:04d}.mp4")

            self.transition_engine.apply_transition(
                result[-1],
                clips[i],
                output_path,
                transition
            )

            result.append(output_path)

        return result

    def _concatenate_clips(
        self,
        clips: List[str],
        config: CompositionConfig
    ) -> str:
        """拼接所有片段"""

        if not clips:
            raise ValueError("No clips to concatenate")

        output_path = str(self.output_dir / "concatenated.mp4")

        # 创建concat文件列表
        concat_file = str(self.output_dir / "concat_list.txt")
        with open(concat_file, 'w') as f:
            for clip in clips:
                f.write(f"file '{clip}'\n")

        cmd = [
            self.ffmpeg_path, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_file,
            "-c:v", config.codec,
            "-preset", config.preset,
            "-crf", str(config.crf),
            output_path
        ]

        subprocess.run(cmd, check=True, capture_output=True)

        # 清理临时文件
        Path(concat_file).unlink(missing_ok=True)

        return output_path

    def _add_bgm(
        self,
        video_path: str,
        bgm_path: str,
        output_path: str,
        config: CompositionConfig
    ) -> str:
        """添加BGM"""

        # 获取视频时长
        duration = self._get_duration(video_path)

        cmd = [
            self.ffmpeg_path, "-y",
            "-i", video_path,
            "-i", bgm_path,
            "-c:v", "copy",
            "-c:a", config.audio_codec,
            "-b:a", config.audio_bitrate,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(duration),
            output_path
        ]

        subprocess.run(cmd, check=True, capture_output=True)

        return output_path

    def _get_duration(self, video_path: str) -> float:
        """获取视频时长"""

        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())

    def compose_with_effects(
        self,
        timeline: List[Dict[str, Any]],
        effects: List[Dict[str, Any]],
        bgm_path: Optional[str] = None,
        output_path: str = "output.mp4",
        config: Optional[CompositionConfig] = None
    ) -> str:
        """
        带特效合成视频

        Args:
            timeline: 时间线条目列表
            effects: 特效列表
            bgm_path: BGM音频路径(可选)
            output_path: 输出视频路径
            config: 合成配置

        Returns:
            输出文件路径
        """
        config = config or CompositionConfig()

        # 1. 准备镜头片段（带特效）
        prepared_clips = self._prepare_clips_with_effects(
            timeline, effects, config
        )

        # 2. 应用转场
        clips_with_transitions = self._apply_transitions(prepared_clips, timeline)

        # 3. 合成最终视频
        final_video = self._concatenate_clips(clips_with_transitions, config)

        # 4. 添加BGM
        if bgm_path:
            final_video = self._add_bgm(final_video, bgm_path, output_path, config)
        else:
            Path(final_video).rename(output_path)
            final_video = output_path

        return final_video

    def _prepare_clips_with_effects(
        self,
        timeline: List[Dict[str, Any]],
        effects: List[Dict[str, Any]],
        config: CompositionConfig
    ) -> List[str]:
        """准备带特效的镜头片段"""

        prepared = []

        for i, entry in enumerate(timeline):
            shot_path = entry.get("file_path")
            if not shot_path:
                continue

            # 裁剪到计划的时长
            planned_duration = entry.get("planned_duration", entry.get("duration", 1.0))
            start_time = entry.get("start_time", 0)

            # 获取该片段的特效
            effect = effects[i] if i < len(effects) else {}

            # 创建临时文件
            temp_path = str(self.output_dir / f"clip_effect_{i:04d}.mp4")

            # 构建滤镜链
            vf = self._build_effect_filter(effect, config)

            # 使用FFmpeg裁剪并应用特效
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", shot_path,
                "-ss", str(start_time),
                "-t", str(planned_duration),
                "-vf", vf,
                "-r", str(config.fps),
                "-c:v", config.codec,
                "-preset", "fast",
                "-an",
                temp_path
            ]

            subprocess.run(cmd, check=True, capture_output=True)
            prepared.append(temp_path)

        return prepared

    def _build_effect_filter(
        self,
        effect: Dict[str, Any],
        config: CompositionConfig
    ) -> str:
        """构建特效滤镜链"""

        filters = []

        # 缩放和填充
        filters.append(
            f"scale={config.width}:{config.height}:force_original_aspect_ratio=decrease"
        )
        filters.append(
            f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2"
        )

        # 应用特效
        if effect.get("speed_ramp"):
            speed = effect["speed_ramp"]
            filters.append(f"setpts={1/speed}*PTS")

        if effect.get("blur"):
            blur_amount = effect["blur"]
            filters.append(f"boxblur={blur_amount}:{blur_amount}")

        if effect.get("brightness"):
            brightness = effect["brightness"]
            filters.append(f"eq=brightness={brightness}")

        if effect.get("contrast"):
            contrast = effect["contrast"]
            filters.append(f"eq=contrast={contrast}")

        if effect.get("saturation"):
            saturation = effect["saturation"]
            filters.append(f"eq=saturation={saturation}")

        if effect.get("vignette"):
            filters.append("vignette=PI/4")

        return ",".join(filters)
