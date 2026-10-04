"""字幕一站式处理：转录音频 + 压制字幕。

karaoke 等风格需要**词级时间戳**，而 SRT 格式不承载词级信息，
因此这里根据风格自动选择中间格式：
    - 普通风格 -> SRT
    - 词级风格（karaoke） -> Whisper JSON（含 words 字段）

CLI（pipeline.py）与 WebUI（packages/webui）共用本函数，避免逻辑重复。
"""

from pathlib import Path
from typing import Optional

from .transcriber import Transcriber
from .caption_burner import burn_captions


# 需要词级时间戳的字幕风格（与 CaptionBurner._build_karaoke_filters 对应）
WORD_LEVEL_STYLES = {"karaoke"}


def transcribe_and_burn(
    video_path: str,
    style: str = "youtube",
    output_path: Optional[str] = None,
    model_size: str = "base",
    backend: str = "whisper",
    language: Optional[str] = None,
) -> str:
    """对视频进行语音转录并压制字幕。

    Args:
        video_path: 输入视频路径
        style: 字幕风格（tiktok/youtube/minimal/karaoke/bold/cinematic）
        output_path: 输出视频路径；默认在原文件旁生成 ``<stem>_subtitled.mp4``
        model_size: Whisper 模型规格（tiny/base/small/medium/large）
        backend: whisper 后端（whisper/faster_whisper）
        language: 语言代码（zh/en/ja...），None 为自动检测

    Returns:
        带字幕的视频路径

    Raises:
        FileNotFoundError: 输入视频不存在
        ImportError: 未安装 whisper 依赖
    """
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    needs_words = style.lower() in WORD_LEVEL_STYLES
    fmt = "json" if needs_words else "srt"
    ext = ".json" if needs_words else ".srt"

    # 1) 转录（karaoke 保留词级时间戳）
    subtitle_path = video.with_suffix(ext)
    transcriber = Transcriber(backend=backend, model_size=model_size)
    transcriber.transcribe(
        str(video),
        output_path=str(subtitle_path),
        output_format=fmt,
        language=language,
    )

    # 2) 压制
    if output_path is None:
        output_path = str(video.with_name(f"{video.stem}_subtitled.mp4"))
    burn_captions(str(video), str(subtitle_path), style=style, output_path=output_path)

    return output_path
