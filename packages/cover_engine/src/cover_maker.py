"""封面 / 缩略图生成引擎

两大能力：
1. pick_best_frame —— 从成片中自动挑一帧最适合做封面的画面
   （清晰度 + 亮度双重打分，无需 opencv，用 ffmpeg 抽帧 + numpy 打分）
2. make_cover —— 把该帧裁成目标比例，叠加标题 / 副标题，导出一张封面图

设计要点：
- 抽帧走 ffmpeg（项目已依赖），避免引入 cv2
- 中文标题用 subtitle_engine 的 find_font 复用系统中文字体
- 长标题自动按像素宽度折行；找不到字体时降级为内置默认字体
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from packages.subtitle_engine import find_font, estimate_text_width


# 目标比例 -> (宽, 高) 像素
ASPECT_SIZES = {
    "16:9": (1280, 720),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1280, 960),
    "3:4": (960, 1280),
}

# 封面文字版式
COVER_STYLES = ("bold", "minimal", "cinematic")


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _probe_duration(video_path: str) -> float:
    """返回视频时长（秒），失败返回 0。"""
    r = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ])
    try:
        return float(r.stdout.decode().strip())
    except (ValueError, AttributeError):
        return 0.0


def _sharpness(gray: "np.ndarray") -> float:
    """用拉普拉斯算子方差衡量清晰度（越大越锐）。"""
    g = gray.astype(np.float32)
    lap = (
        4.0 * g[1:-1, 1:-1]
        - g[:-2, 1:-1] - g[2:, 1:-1]
        - g[1:-1, :-2] - g[1:-1, 2:]
    )
    return float(lap.var())


def _brightness_score(gray: "np.ndarray") -> float:
    """亮度打分：均值落在 0.35~0.65 得满分，过暗/过曝扣分。"""
    mean = float(gray.mean()) / 255.0
    if 0.35 <= mean <= 0.65:
        return 1.0
    # 距离理想区间越远，分越低
    dist = 0.35 - mean if mean < 0.35 else mean - 0.65
    return max(0.0, 1.0 - dist / 0.35)


def pick_best_frame(
    video_path: str,
    top_n: int = 10,
    tmp_dir: Optional[str] = None,
    sample_fps: float = 1.0,
) -> Optional[str]:
    """从视频里挑出最适合当封面的一帧。

    Args:
        video_path: 输入视频
        top_n: 抽帧数量（等间隔采样，越多越准但越慢）
        tmp_dir: 抽帧临时目录，默认用系统临时目录
        sample_fps: 采样帧率上限，避免长视频抽太多帧

    Returns:
        最佳帧的图片路径；失败返回 None
    """
    if np is None:
        print("  [封面] 未安装 numpy，无法打分挑帧")
        return None

    src = Path(video_path)
    if not src.exists():
        print(f"  [封面] 视频不存在: {video_path}")
        return None

    tmp_dir = tmp_dir or tempfile.mkdtemp(prefix="cover_frames_")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)

    duration = _probe_duration(video_path)
    if duration <= 0:
        print("  [封面] 无法读取视频时长")
        return None

    # 等间隔选取 top_n 个时间点，避开片头片尾（各留 5%）
    n = max(1, top_n)
    lo, hi = duration * 0.05, duration * 0.95
    span = max(hi - lo, 0.001)
    times = [lo + span * (i / max(1, n - 1)) for i in range(n)] if n > 1 else [duration / 2]

    frames: List[Tuple[str, float]] = []
    for idx, t in enumerate(times):
        out = os.path.join(tmp_dir, f"frame_{idx:03d}.jpg")
        r = _run([
            "ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", video_path,
            "-frames:v", "1", "-q:v", "2", out,
        ])
        if r.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 0:
            frames.append((out, t))

    if not frames:
        print("  [封面] 抽帧失败")
        return None

    # 打分：清晰度为主(0.7)，亮度为辅(0.3)
    scored: List[Tuple[float, str]] = []
    for path, _t in frames:
        try:
            with Image.open(path) as im:
                gray = np.asarray(im.convert("L"))
        except Exception:
            continue
        sharp = _sharpness(gray)
        bright = _brightness_score(gray)
        # 清晰度做对数压缩，避免个别超锐帧压倒一切
        sharp_norm = float(np.log1p(sharp))
        score = 0.7 * sharp_norm + 0.3 * bright * sharp_norm
        scored.append((score, path))

    if not scored:
        return frames[0][0]

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    print(f"  [封面] 从 {len(frames)} 帧中选出最佳帧 (score={scored[0][0]:.2f})")
    return best


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """加载中文字体；找不到时退回内置默认字体。"""
    path = find_font("cjk")
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default()
    except Exception:  # pragma: no cover
        return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> List[str]:
    """按像素宽度折行；中文无空格，逐字累加测宽。"""
    lines: List[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for ch in para:
            trial = cur + ch
            if estimate_text_width(trial, font.size) > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        if cur:
            lines.append(cur)
    return lines


def _cover_crop(im: Image.Image, target: Tuple[int, int]) -> Image.Image:
    """等比缩放 + 居中裁剪到目标尺寸。"""
    tw, th = target
    w, h = im.size
    scale = max(tw / w, th / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return im.crop((left, top, left + tw, top + th))


def make_cover(
    frame_path: str,
    title: str,
    subtitle: str = "",
    size: str = "16:9",
    out_path: Optional[str] = None,
    style: str = "bold",
) -> Optional[str]:
    """把一帧图片做成带标题的封面。

    Args:
        frame_path: 底图（pick_best_frame 的结果）
        title: 主标题
        subtitle: 副标题（可选）
        size: 目标比例，见 ASPECT_SIZES
        out_path: 输出路径，默认在底图同目录生成 cover_<size>.png
        style: bold / minimal / cinematic

    Returns:
        生成的封面图片路径；失败返回 None
    """
    src = Path(frame_path)
    if not src.exists():
        print(f"  [封面] 底图不存在: {frame_path}")
        return None

    if size not in ASPECT_SIZES:
        print(f"  [封面] 未知比例 {size}，回退 16:9")
        size = "16:9"
    style = style if style in COVER_STYLES else "bold"
    tw, th = ASPECT_SIZES[size]

    if out_path is None:
        out_path = str(src.with_name(f"cover_{size.replace(':', 'x')}.png"))

    try:
        with Image.open(frame_path) as raw:
            base = _cover_crop(raw.convert("RGB"), (tw, th))
    except Exception as e:
        print(f"  [封面] 读取底图失败: {e}")
        return None

    # 文字区渐变蒙版：bold/cinematic 用下方压暗，minimal 用整幅轻压暗
    overlay = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    if style == "minimal":
        od.rectangle([0, 0, tw, th], fill=(0, 0, 0, 90))
    else:
        band = int(th * (0.55 if style == "cinematic" else 0.5))
        for i in range(band):
            alpha = int(200 * (i / band) ** 1.4)
            y = th - band + i
            od.line([(0, y), (tw, y)], fill=(0, 0, 0, alpha))
    base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(base)

    # 字号随分辨率缩放
    base_unit = th / 720.0
    title_size = int(64 * base_unit) if style != "minimal" else int(52 * base_unit)
    sub_size = int(32 * base_unit)
    title_font = _load_font(title_size, bold=True)
    sub_font = _load_font(sub_size, bold=False)

    margin = int(56 * base_unit)
    max_w = tw - margin * 2

    title_lines = _wrap_text(title, title_font, max_w)[:3]
    sub_lines = _wrap_text(subtitle, sub_font, max_w)[:2] if subtitle else []

    # 自底向上排版
    line_gap = int(14 * base_unit)
    title_lh = title_size + line_gap
    sub_lh = sub_size + int(8 * base_unit)
    block_h = len(title_lines) * title_lh + (len(sub_lines) * sub_lh + int(16 * base_unit) if sub_lines else 0)
    y = th - margin - block_h

    # 强调色竖条（bold 风格）
    accent = (255, 210, 60)
    if style == "bold" and title_lines:
        bar_x = margin - int(20 * base_unit)
        draw.rectangle(
            [bar_x, y + int(6 * base_unit), bar_x + int(8 * base_unit), y + len(title_lines) * title_lh],
            fill=accent,
        )

    for ln in title_lines:
        draw.text((margin, y), ln, font=title_font, fill=(255, 255, 255),
                  stroke_width=max(1, int(2 * base_unit)), stroke_fill=(0, 0, 0))
        y += title_lh

    if sub_lines:
        y += int(16 * base_unit)
        for ln in sub_lines:
            draw.text((margin, y), ln, font=sub_font, fill=(225, 225, 225),
                      stroke_width=max(1, int(1 * base_unit)), stroke_fill=(0, 0, 0))
            y += sub_lh

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    base.save(out_path, quality=95)
    print(f"  [封面] 已生成: {out_path} ({tw}x{th})")
    return out_path


def generate_cover(
    video_path: str,
    title: str,
    subtitle: str = "",
    size: str = "16:9",
    style: str = "bold",
    out_path: Optional[str] = None,
    top_n: int = 10,
    frame_path: Optional[str] = None,
) -> Optional[str]:
    """一步到位：挑帧 + 生成封面。"""
    fp = frame_path or pick_best_frame(video_path, top_n=top_n)
    if not fp:
        return None
    return make_cover(fp, title, subtitle, size=size, out_path=out_path, style=style)
