"""
FFmpeg Executor
FFmpeg执行器 - 负责视频处理、合成、导出
"""

import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class VideoConfig:
    """视频配置"""
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

class FFmpegExecutor:
    """
    FFmpeg执行器
    功能:
    - trim: 裁剪视频片段
    - concat: 拼接视频
    - scale: 缩放视频
    - crop: 裁切视频
    - transition: 应用转场
    - 导出: final.mp4
    """

    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg_path = ffmpeg_path
        self._verify_ffmpeg()

    def _verify_ffmpeg(self):
        """验证FFmpeg是否安装"""
        try:
            subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True,
                check=True
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"FFmpeg not found at {self.ffmpeg_path}. "
                "Please install FFmpeg first."
            )

    def trim(
        self,
        input_path: str,
        output_path: str,
        start_time: float,
        end_time: float,
        codec_copy: bool = True
    ) -> str:
        """
        裁剪视频片段

        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            start_time: 开始时间(秒)
            end_time: 结束时间(秒)
            codec_copy: 是否直接复制编码(更快)

        Returns:
            输出文件路径
        """
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-ss", str(start_time),
            "-to", str(end_time)
        ]

        if codec_copy:
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23"
            ])

        cmd.append(output_path)

        subprocess.run(cmd, check=True, capture_output=True)

        return output_path

    def concat(
        self,
        input_paths: List[str],
        output_path: str,
        config: Optional[VideoConfig] = None
    ) -> str:
        """
        拼接多个视频

        Args:
            input_paths: 输入视频路径列表
            output_path: 输出视频路径
            config: 视频配置

        Returns:
            输出文件路径
        """
        if not input_paths:
            raise ValueError("No input videos provided")

        config = config or VideoConfig()

        # 创建concat文件列表
        concat_file = Path(output_path).parent / "concat_list.txt"
        with open(concat_file, 'w') as f:
            for path in input_paths:
                f.write(f"file '{path}'\n")

        cmd = [
            self.ffmpeg_path, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c:v", config.codec,
            "-preset", config.preset,
            "-crf", str(config.crf),
            "-r", str(config.fps),
            "-c:a", config.audio_codec,
            "-b:a", config.audio_bitrate,
            output_path
        ]

        subprocess.run(cmd, check=True, capture_output=True)

        # 清理临时文件
        concat_file.unlink(missing_ok=True)

        return output_path

    def scale(
        self,
        input_path: str,
        output_path: str,
        width: int,
        height: int,
        keep_aspect_ratio: bool = True
    ) -> str:
        """
        缩放视频

        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            width: 目标宽度
            height: 目标高度
            keep_aspect_ratio: 是否保持宽高比

        Returns:
            输出文件路径
        """
        if keep_aspect_ratio:
            scale_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease"
            pad_filter = f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            vf = f"{scale_filter},{pad_filter}"
        else:
            vf = f"scale={width}:{height}"

        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", vf,
            "-c:a", "copy",
            output_path
        ]

        subprocess.run(cmd, check=True, capture_output=True)

        return output_path

    def crop(
        self,
        input_path: str,
        output_path: str,
        x: int,
        y: int,
        width: int,
        height: int
    ) -> str:
        """
        裁切视频

        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            x: 裁切起始X坐标
            y: 裁切起始Y坐标
            width: 裁切宽度
            height: 裁切高度

        Returns:
            输出文件路径
        """
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", f"crop={width}:{height}:{x}:{y}",
            "-c:a", "copy",
            output_path
        ]

        subprocess.run(cmd, check=True, capture_output=True)

        return output_path

    def adjust_speed(
        self,
        input_path: str,
        output_path: str,
        speed_factor: float
    ) -> str:
        """
        调整视频速度（整段等比）

        修复点：原实现直接写 `-af atempo=speed`，
          * speed 超出 [0.5, 2.0] 时老版 ffmpeg 会报错；
          * 源无音轨时 `-af` 会直接失败。
        现改为多级 atempo 串联，并先探测是否存在音轨。

        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            speed_factor: 速度因子 (0.5 = 半速, 2.0 = 双速)

        Returns:
            输出文件路径
        """
        from packages.montage_engine.src.speed_curve import atempo_chain, has_audio_stream

        video_speed = 1.0 / speed_factor
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", f"setpts={video_speed}*PTS",
        ]
        if has_audio_stream(input_path):
            cmd += ["-af", ",".join(f"atempo={f:.6f}" for f in atempo_chain(speed_factor))]
        cmd.append(output_path)

        subprocess.run(cmd, check=True, capture_output=True)
        return output_path

    def adjust_speed_curve(
        self,
        input_path: str,
        output_path: str,
        curve,
    ) -> str:
        """按速度曲线变速（分段等速，逐段对齐保证音画同步）。

        Args:
            curve: ``montage_engine.SpeedCurve``
        """
        from packages.montage_engine.src.speed_curve import apply_speed_curve

        return apply_speed_curve(input_path, output_path, curve)

    def add_audio(
        self,
        video_path: str,
        audio_path: str,
        output_path: str,
        replace: bool = True
    ) -> str:
        """
        添加音频到视频

        Args:
            video_path: 输入视频路径
            audio_path: 音频文件路径
            output_path: 输出视频路径
            replace: 是否替换原有音频

        Returns:
            输出文件路径
        """
        if replace:
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                output_path
            ]
        else:
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", video_path,
                "-i", audio_path,
                "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first",
                "-c:v", "copy",
                output_path
            ]

        subprocess.run(cmd, check=True, capture_output=True)

        return output_path

    def get_video_info(self, video_path: str) -> Dict[str, Any]:
        """获取视频信息"""

        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,duration,codec_name",
            "-show_entries", "format=duration,size",
            "-of", "json",
            video_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        info = json.loads(result.stdout)

        return info

    def export_final(
        self,
        video_path: str,
        output_path: str,
        bgm_path: Optional[str] = None,
        config: Optional[VideoConfig] = None
    ) -> str:
        """
        导出最终视频

        Args:
            video_path: 输入视频路径
            output_path: 输出视频路径
            bgm_path: BGM音频路径(可选)
            config: 视频配置

        Returns:
            输出文件路径
        """
        config = config or VideoConfig()

        cmd = [
            self.ffmpeg_path, "-y",
            "-i", video_path
        ]

        # 添加BGM
        if bgm_path:
            cmd.extend(["-i", bgm_path])

        # 视频编码设置
        cmd.extend([
            "-c:v", config.codec,
            "-preset", config.preset,
            "-crf", str(config.crf),
            "-r", str(config.fps),
            "-s", f"{config.width}x{config.height}"
        ])

        # 音频编码设置
        if bgm_path:
            cmd.extend([
                "-c:a", config.audio_codec,
                "-b:a", config.audio_bitrate,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest"
            ])
        else:
            cmd.extend([
                "-c:a", config.audio_codec,
                "-b:a", config.audio_bitrate
            ])

        cmd.append(output_path)

        subprocess.run(cmd, check=True, capture_output=True)

        return output_path
