"""
字幕压制模块 - 将字幕烧录到视频中

支持多种字幕风格：tiktok/youtube/minimal/karaoke/bold/cinematic
支持 SRT/VTT/Whisper JSON 格式
"""

import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional


class CaptionStyle(str, Enum):
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    MINIMAL = "minimal"
    KARAOKE = "karaoke"
    BOLD = "bold"
    CINEMATIC = "cinematic"


@dataclass
class CaptionSegment:
    start: float
    end: float
    text: str
    words: Optional[List[Dict[str, Any]]] = None


@dataclass
class StyleConfig:
    fontsize: int = 48
    fontcolor: str = "white"
    fontfile: Optional[str] = None
    borderw: int = 2
    bordercolor: str = "black"
    shadowcolor: str = "black@0.5"
    shadowx: int = 2
    shadowy: int = 2
    box: bool = False
    boxcolor: str = "black@0.6"
    boxborderw: int = 10
    x_expr: str = "(w-text_w)/2"
    y_expr: str = "h-100"
    line_spacing: int = 10


STYLE_CONFIGS: Dict[CaptionStyle, StyleConfig] = {
    CaptionStyle.TIKTOK: StyleConfig(
        fontsize=64, fontcolor="white",
        borderw=4, bordercolor="black",
        shadowx=0, shadowy=0, box=False,
        x_expr="(w-text_w)/2",
        y_expr="(h-text_h)*0.75",
    ),
    CaptionStyle.YOUTUBE: StyleConfig(
        fontsize=42, fontcolor="white",
        borderw=2, bordercolor="black",
        shadowx=2, shadowy=2,
        box=True, boxcolor="black@0.7", boxborderw=8,
        x_expr="(w-text_w)/2",
        y_expr="h-80",
    ),
    CaptionStyle.MINIMAL: StyleConfig(
        fontsize=36, fontcolor="white",
        borderw=1, bordercolor="black@0.5",
        shadowx=1, shadowy=1, box=False,
        x_expr="(w-text_w)/2",
        y_expr="h-60",
    ),
    CaptionStyle.KARAOKE: StyleConfig(
        fontsize=56, fontcolor="yellow",
        borderw=3, bordercolor="black",
        shadowx=0, shadowy=0, box=False,
        x_expr="(w-text_w)/2",
        y_expr="(h-text_h)/2",
    ),
    CaptionStyle.BOLD: StyleConfig(
        fontsize=72, fontcolor="white",
        borderw=5, bordercolor="black",
        shadowx=3, shadowy=3, box=False,
        x_expr="(w-text_w)/2",
        y_expr="(h-text_h)*0.6",
    ),
    CaptionStyle.CINEMATIC: StyleConfig(
        fontsize=38, fontcolor="white@0.9",
        borderw=0, box=True,
        boxcolor="black@0.4", boxborderw=12,
        shadowx=0, shadowy=0,
        x_expr="(w-text_w)/2",
        y_expr="h*0.85",
    ),
}


# ---------------------------------------------------------------------------
# 字体解析
# ---------------------------------------------------------------------------
# 各平台常见中文字体候选（按优先级）。
# 不指定字体时 ffmpeg 的默认字体不含中文字形，中文会渲染成「豆腐块」，
# 因此这里自动挑一个可用的中文字体文件。
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",            # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",          # 黑体
    r"C:\Windows\Fonts\simsun.ttc",          # 宋体
    r"C:\Windows\Fonts\Deng.ttf",            # 等线
    "/System/Library/Fonts/PingFang.ttc",    # macOS 苹方
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

_FFMPEG_MONO_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

_font_cache: Dict[str, Optional[str]] = {}


def find_font(kind: str = "cjk") -> Optional[str]:
    """查找可用的字体文件。

    Args:
        kind: "cjk" 中文字体，"mono" 等宽字体

    Returns:
        字体文件路径；找不到返回 None（此时按 ffmpeg 默认字体处理）
    """
    if kind in _font_cache:
        return _font_cache[kind]

    env_key = "MONTAGE_FONT" if kind == "cjk" else "MONTAGE_FONT_MONO"
    candidates = []
    if os.environ.get(env_key):
        candidates.append(os.environ[env_key])
    candidates += _CJK_FONT_CANDIDATES if kind == "cjk" else _FFMPEG_MONO_FONT_CANDIDATES

    found = None
    for p in candidates:
        if p and Path(p).exists():
            found = p
            break
    if found is None and kind == "cjk":
        print("  [警告] 未找到中文字体，字幕中文可能显示为方块；"
              "可通过环境变量 MONTAGE_FONT 指定字体文件")
    _font_cache[kind] = found
    return found


def escape_font_path(path: str) -> str:
    """转义并引用 ffmpeg 滤镜中的字体路径。

    必须用单引号包裹，否则 ffmpeg 滤镜解析器会把盘符的 ``:`` 当作选项分隔符
    （Windows 下 ``fontfile=C:/...`` 会直接报 filter 解析失败）。
    """
    p = str(path).replace("\\", "/").replace(":", "\\:")
    return f"'{p}'"


def _is_wide_char(ch: str) -> bool:
    """是否为全角字符（CJK / 全角标点），用于估算文本宽度"""
    return ord(ch) > 0x2E80


def estimate_text_width(text: str, fontsize: int) -> float:
    """粗略估算文本像素宽度。

    drawtext 无法在构建滤镜时测量真实字宽，这里按经验值估算：
    CJK/全角字符 ≈ 1 em，其余（ASCII 字母、数字、半角空格）≈ 0.5 em。
    只要基础行与高亮词使用同一套估算，二者的相对位置就一致。
    """
    return sum(fontsize * (1.0 if _is_wide_char(c) else 0.5) for c in text)


def parse_srt(srt_path: Path) -> List[CaptionSegment]:
    segments = []
    content = srt_path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"(.*?)(?=\n\n|\n*$)",
        re.DOTALL,
    )
    for match in pattern.finditer(content):
        start = int(match.group(2)) * 3600 + int(match.group(3)) * 60 + int(match.group(4)) + int(match.group(5)) / 1000
        end = int(match.group(6)) * 3600 + int(match.group(7)) * 60 + int(match.group(8)) + int(match.group(9)) / 1000
        text = match.group(10).strip().replace("\n", " ")
        segments.append(CaptionSegment(start=start, end=end, text=text))
    return segments


def parse_vtt(vtt_path: Path) -> List[CaptionSegment]:
    segments = []
    content = vtt_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    i = 0
    while i < len(lines) and "-->" not in lines[i]:
        i += 1
    pattern = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
    while i < len(lines):
        match = pattern.match(lines[i].strip())
        if match:
            start = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 1000
            end = int(match.group(5)) * 3600 + int(match.group(6)) * 60 + int(match.group(7)) + int(match.group(8)) / 1000
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1
            segments.append(CaptionSegment(start=start, end=end, text=" ".join(text_lines)))
        i += 1
    return segments


def parse_whisper_json(json_path: Path) -> List[CaptionSegment]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    segments = []
    for seg in data.get("segments", []):
        segments.append(CaptionSegment(
            start=seg["start"], end=seg["end"],
            text=seg["text"].strip(),
            words=seg.get("words"),
        ))
    return segments


def load_captions(caption_path: str) -> List[CaptionSegment]:
    path = Path(caption_path)
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return parse_srt(path)
    elif suffix == ".vtt":
        return parse_vtt(path)
    elif suffix == ".json":
        return parse_whisper_json(path)
    else:
        raise ValueError(f"不支持的字幕格式: {suffix}")


def escape_ffmpeg_text(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    text = text.replace("%", "\\%")
    return text


class CaptionBurner:
    def __init__(self, style: CaptionStyle = CaptionStyle.YOUTUBE, custom_config: Optional[StyleConfig] = None):
        self.style = style
        # 复制一份，避免修改模块级 STYLE_CONFIGS 单例
        self.config = copy.deepcopy(custom_config or STYLE_CONFIGS[style])
        # 未指定字体时自动套用系统中文字体，否则中文会变成方块
        if not self.config.fontfile:
            self.config.fontfile = find_font("cjk")

    def _font_part(self, config: StyleConfig) -> List[str]:
        """drawtext 的 fontfile 参数（含路径转义）"""
        if not config.fontfile:
            return []
        return [f"fontfile={escape_font_path(config.fontfile)}"]

    def _build_drawtext_filter(self, segment: CaptionSegment, config: StyleConfig) -> str:
        text = escape_ffmpeg_text(segment.text)
        parts = [
            f"drawtext=text='{text}'",
            f"fontsize={config.fontsize}",
            f"fontcolor={config.fontcolor}",
            f"borderw={config.borderw}",
            f"bordercolor={config.bordercolor}",
            f"x={config.x_expr}",
            f"y={config.y_expr}",
            f"enable='between(t,{segment.start:.3f},{segment.end:.3f})'",
        ]
        parts.extend(self._font_part(config))
        if config.shadowx or config.shadowy:
            parts.append(f"shadowcolor={config.shadowcolor}")
            parts.append(f"shadowx={config.shadowx}")
            parts.append(f"shadowy={config.shadowy}")
        if config.box:
            parts.append("box=1")
            parts.append(f"boxcolor={config.boxcolor}")
            parts.append(f"boxborderw={config.boxborderw}")
        return ":".join(parts)

    def _build_karaoke_filters(self, segment: CaptionSegment, config: StyleConfig) -> List[str]:
        if not segment.words:
            return [self._build_drawtext_filter(segment, config)]

        words = [w for w in segment.words if str(w.get("word", "")).strip()]
        if not words:
            return [self._build_drawtext_filter(segment, config)]

        fs = config.fontsize

        # 逐词绘制：灰底词与黄色高亮词共用同一套坐标，彻底避免错位
        texts = [str(w.get("word", "")).strip() for w in words]
        widths = [estimate_text_width(t, fs) for t in texts]

        # 词间距：所有词都是单个全角字（中文逐字）时不插额外间距，
        # 否则中文会被撑开；拉丁词之间保留一个空格宽度。
        all_single_wide = all(len(t) == 1 and _is_wide_char(t) for t in texts)
        space_w = 0.0 if all_single_wide else fs * 0.35

        line_w = sum(widths) + space_w * max(0, len(words) - 1)
        center = f"(w-{line_w:.0f})/2"

        filters: List[str] = []
        cum = 0.0
        for i, word_data in enumerate(words):
            x = f"{center}+{cum:.0f}"
            common = [
                f"fontsize={fs}",
                f"borderw={config.borderw}",
                f"bordercolor={config.bordercolor}",
                f"x={x}",
                f"y={config.y_expr}",
            ]
            common.extend(self._font_part(config))

            # 基础词（整句时段内显示为灰色）
            base_parts = [
                f"drawtext=text='{escape_ffmpeg_text(texts[i])}'",
                "fontcolor='gray'",
                *common,
                f"enable='between(t,{segment.start:.3f},{segment.end:.3f})'",
            ]
            filters.append(":".join(base_parts))

            # 高亮词（该词自己的时间段内覆盖为高亮色）
            word_start = word_data.get("start", segment.start)
            word_end = word_data.get("end", segment.end)
            hi_parts = [
                f"drawtext=text='{escape_ffmpeg_text(texts[i])}'",
                f"fontcolor={config.fontcolor}",
                *common,
                f"enable='between(t,{word_start:.3f},{word_end:.3f})'",
            ]
            filters.append(":".join(hi_parts))

            cum += widths[i] + space_w
        return filters

    def _build_filter_chain(self, segments: List[CaptionSegment], config: Optional[StyleConfig] = None) -> str:
        config = config or self.config
        filters = []
        if self.style == CaptionStyle.KARAOKE:
            for seg in segments:
                filters.extend(self._build_karaoke_filters(seg, config))
        else:
            for seg in segments:
                filters.append(self._build_drawtext_filter(seg, config))
        return ",".join(filters)

    @staticmethod
    def _probe_width(video_path: str) -> int:
        """获取视频宽度（失败返回 0）"""
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", video_path],
            capture_output=True, text=True,
        )
        try:
            return int(r.stdout.strip().split(",")[0])
        except (ValueError, IndexError):
            return 0

    def _fit_config(self, segments: List[CaptionSegment], video_width: int) -> StyleConfig:
        """按视频宽度自动缩小字号，避免长字幕溢出画面。"""
        config = copy.deepcopy(self.config)
        if video_width <= 0:
            return config
        max_text = max((s.text for s in segments), key=len, default="")
        est = estimate_text_width(max_text, config.fontsize)
        limit = video_width * 0.9
        if est > limit and est > 0:
            scale = limit / est
            old = config.fontsize
            config.fontsize = max(16, int(old * scale))
            print(f"  字幕过长，字号 {old} -> {config.fontsize} 以避免溢出")
        return config

    def burn(
        self, video_path: str, caption_path: str,
        output_path: Optional[str] = None,
        codec: str = "libx264", crf: int = 23, preset: str = "medium",
    ) -> str:
        video = Path(video_path)
        if not video.exists():
            raise FileNotFoundError(f"视频不存在: {video_path}")
        if not Path(caption_path).exists():
            raise FileNotFoundError(f"字幕不存在: {caption_path}")
        if output_path is None:
            output_path = str(video.parent / f"{video.stem}_captioned.mp4")

        segments = load_captions(caption_path)
        if not segments:
            raise ValueError("没有字幕段")

        config = self._fit_config(segments, self._probe_width(video_path))
        filter_chain = self._build_filter_chain(segments, config)
        cmd = [
            "ffmpeg", "-y", "-i", str(video),
            "-vf", filter_chain,
            "-c:v", codec, "-crf", str(crf), "-preset", preset,
            "-c:a", "copy", output_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return output_path


def burn_captions(
    video_path: str, caption_path: str,
    style: str = "youtube", output_path: Optional[str] = None,
) -> str:
    try:
        caption_style = CaptionStyle(style.lower())
    except ValueError:
        caption_style = CaptionStyle.YOUTUBE
    burner = CaptionBurner(style=caption_style)
    return burner.burn(video_path, caption_path, output_path)
