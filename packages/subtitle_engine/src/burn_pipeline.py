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
from .caption_burner import burn_captions, load_captions, build_bilingual_ass, burn_ass
from .translator import translate_segments


# 需要词级时间戳的字幕风格（与 CaptionBurner._build_karaoke_filters 对应）
WORD_LEVEL_STYLES = {"karaoke"}

# 双语字幕风格
BILINGUAL_STYLE = "bilingual"


def transcribe_and_burn(
    video_path: str,
    style: str = "youtube",
    output_path: Optional[str] = None,
    model_size: str = "base",
    backend: str = "whisper",
    language: Optional[str] = None,
    translate: Optional[str] = None,
    source_lang: str = "auto",
    translate_backend: str = "auto",
) -> str:
    """对视频进行语音转录并压制字幕（可选双语翻译）。

    Args:
        video_path: 输入视频路径
        style: 字幕风格（tiktok/youtube/minimal/karaoke/bold/cinematic/bilingual）
        output_path: 输出视频路径；默认在原文件旁生成 ``<stem>_subtitled.mp4``
        model_size: Whisper 模型规格（tiny/base/small/medium/large）
        backend: whisper 后端（whisper/faster_whisper）
        language: 转录语言代码（zh/en/ja...），None 为自动检测
        translate: 目标语言代码（如 "en"）；给定时生成双语字幕
        source_lang: 源语言代码（auto 为自动）
        translate_backend: 翻译后端（auto/llm/deep）

    Returns:
        带字幕的视频路径

    Raises:
        FileNotFoundError: 输入视频不存在
        ImportError: 未安装 whisper 依赖
    """
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    if output_path is None:
        output_path = str(video.with_name(f"{video.stem}_subtitled.mp4"))

    # 双语：转录（含词级时间戳的 JSON，便于对齐）→ 翻译 → 写 ASS 压制
    if translate:
        json_path = video.with_suffix(".json")
        transcriber = Transcriber(backend=backend, model_size=model_size)
        transcriber.transcribe(
            str(video),
            output_path=str(json_path),
            output_format="json",
            language=language,
        )
        segments = load_captions(str(json_path))
        print(f"  翻译字幕 -> {translate}（后端: {translate_backend}）...")
        bilingual = translate_segments(
            segments, target=translate, source=source_lang,
            backend=translate_backend,
        )
        w, h = 1280, 720
        try:
            from .caption_burner import CaptionBurner
            w, h = CaptionBurner._probe_size(str(video))
        except Exception:
            pass
        ass_path = build_bilingual_ass(bilingual, video_size=(w, h), target_lang=translate)
        try:
            burn_ass(str(video), ass_path, output_path)
        finally:
            Path(ass_path).unlink(missing_ok=True)
        return output_path

    # 单语：原有流程
    needs_words = style.lower() in WORD_LEVEL_STYLES
    fmt = "json" if needs_words else "srt"
    ext = ".json" if needs_words else ".srt"

    subtitle_path = video.with_suffix(ext)
    transcriber = Transcriber(backend=backend, model_size=model_size)
    transcriber.transcribe(
        str(video),
        output_path=str(subtitle_path),
        output_format=fmt,
        language=language,
    )

    burn_captions(str(video), str(subtitle_path), style=style, output_path=output_path)
    return output_path
