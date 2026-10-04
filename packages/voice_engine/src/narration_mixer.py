"""旁白混音。

把旁白音轨叠加到成片上，并在旁白播放时段自动压低 BGM（ducking），
保证人声清晰。

实现说明：用 ``volume`` 滤镜的 ``enable='between(t,a,b)'`` 显式书写闪避
时间窗，而不是 ``sidechaincompress`` —— 后者在输入被 ``apad`` 补长时
输出时长不确定，容易导致 ffmpeg 挂起。
"""

import subprocess
from pathlib import Path


def probe_duration(path: str) -> float:
    """获取媒体时长（秒）"""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def _has_audio(path: str) -> bool:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=index", "-of", "csv=p=0", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return bool(r.stdout.strip())


def mix_narration(
    video_path: str,
    narration_audio: str,
    output_path: str,
    voice_gain_db: float = 0.0,
    bgm_gain_db: float = 0.0,
    duck: bool = True,
    duck_db: float = -9.0,
) -> str:
    """把旁白叠加到视频音轨，并在旁白时段压低 BGM。

    Args:
        video_path: 输入视频
        narration_audio: 旁白音频
        output_path: 输出视频
        voice_gain_db: 旁白增益（dB）
        bgm_gain_db: 原始音轨（BGM）整体增益（dB）
        duck: 是否在旁白时段自动压低 BGM
        duck_db: 旁白时段 BGM 的额外衰减（dB，负值）

    Returns:
        输出视频路径
    """
    if not Path(video_path).exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")
    if not Path(narration_audio).exists():
        raise FileNotFoundError(f"旁白音频不存在: {narration_audio}")

    narr_dur = probe_duration(narration_audio)

    # 视频无音轨：旁白直接作为音轨
    if not _has_audio(video_path):
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", narration_audio,
            "-map", "0:v", "-map", "1:a",
            "-af", f"volume={voice_gain_db}dB",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_path,
        ]
    else:
        vols = [f"volume={bgm_gain_db}dB"]
        if duck and narr_dur > 0:
            vols.append(
                f"volume=enable='between(t,0,{narr_dur:.3f})':volume={duck_db}dB"
            )
        filters = [f"[0:a]{','.join(vols)}[bgm]"]
        filters.append(f"[1:a]volume={voice_gain_db}dB[vo]")
        filters.append("[bgm][vo]amix=inputs=2:duration=first:normalize=0[aout]")
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", narration_audio,
            "-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]

    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not Path(output_path).exists():
        raise RuntimeError(
            "旁白混音失败:\n" + proc.stderr.decode("utf-8", errors="ignore")[-800:]
        )
    return output_path
