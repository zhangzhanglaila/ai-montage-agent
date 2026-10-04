"""低码率预览 / 代理生成

两类预览：

1. **代理（proxy）**：把成片整体压成小尺寸低码率版本，WebUI 里秒开播放，
   不必等几十 MB 的原片缓冲。按「源文件路径 + mtime + 尺寸 + 参数」缓存。
2. **参数预览（param preview）**：只渲染一小段（默认前 6 秒）、小尺寸、低码率，
   带上当前调色等参数，让用户拖完参数几秒内就能看到效果，不用整片重渲。

缓存目录 ``cache/preview/``，键为内容指纹 —— 源文件改了或参数变了会自动重建。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_WIDTH = 480
DEFAULT_CRF = 32
DEFAULT_PRESET = "veryfast"
CACHE_DIR = Path("cache/preview")


# ------------------------------------------------------------------ 工具
def _run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe_duration(path: str) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", path])
    try:
        return float(r.stdout.decode().strip())
    except (ValueError, AttributeError):
        return 0.0


def probe_size(path: str) -> Tuple[int, int]:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height", "-of", "csv=p=0", path])
    try:
        w, h = r.stdout.decode().strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


def file_bitrate_kbps(path: str) -> float:
    """近似码率（文件大小 / 时长）。"""
    dur = probe_duration(path)
    size = Path(path).stat().st_size if Path(path).exists() else 0
    return (size * 8 / 1000.0 / dur) if dur > 0 else 0.0


def _cache_key(src: str, params: Dict[str, Any]) -> str:
    p = Path(src)
    try:
        st = p.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "0:0"
    payload = json.dumps(
        {"src": str(p.resolve()), "stamp": stamp, "params": params},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 代理
def build_proxy(
    src: str,
    dst: str,
    width: int = DEFAULT_WIDTH,
    crf: int = DEFAULT_CRF,
    preset: str = DEFAULT_PRESET,
    max_duration: Optional[float] = None,
    start: float = 0.0,
    mute: bool = False,
) -> str:
    """把视频压成低码率代理（等比缩到 width，不放大）。"""
    if not Path(src).exists():
        raise FileNotFoundError(f"视频不存在: {src}")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    vf = f"scale='min({int(width)},iw)':-2"
    cmd = ["ffmpeg", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src, "-vf", vf, "-c:v", "libx264",
            "-crf", str(int(crf)), "-preset", preset, "-pix_fmt", "yuv420p"]
    if mute:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    if max_duration and max_duration > 0:
        cmd += ["-t", f"{max_duration:.3f}"]
    cmd.append(dst)

    r = _run(cmd)
    if r.returncode != 0 or not Path(dst).exists():
        raise RuntimeError("代理生成失败:\n" + r.stderr.decode(errors="ignore")[-700:])
    return dst


def get_proxy(
    src: str,
    width: int = DEFAULT_WIDTH,
    crf: int = DEFAULT_CRF,
    max_duration: Optional[float] = None,
    cache_dir: Path = CACHE_DIR,
    force: bool = False,
) -> Dict[str, Any]:
    """获取（必要时生成）代理，命中缓存则直接返回。

    Returns:
        {"path", "cached", "build_ms", "width", "height", "kbps"}
    """
    params = {"kind": "proxy", "width": int(width), "crf": int(crf),
              "max_duration": max_duration}
    key = _cache_key(src, params)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{Path(src).stem}_{key}.mp4"

    cached = dst.exists() and not force
    t0 = time.time()
    if not cached:
        build_proxy(src, str(dst), width=width, crf=crf, max_duration=max_duration)
    build_ms = int((time.time() - t0) * 1000)

    t1 = time.time()
    w, h = probe_size(str(dst))
    return {
        "path": str(dst),
        "cached": cached,
        "build_ms": build_ms,
        "probe_ms": int((time.time() - t1) * 1000),
        "width": w,
        "height": h,
        "kbps": round(file_bitrate_kbps(str(dst)), 1),
        "key": key,
    }


# ------------------------------------------------------------ 参数预览
def _shift_subtitles(src_sub: str, out_sub: str, offset: float, duration: float) -> Optional[str]:
    """把字幕时间轴整体左移 offset，并裁到 [0, duration]，输出 ASS。

    这样对片段做预览时，字幕仍然对得上。
    """
    try:
        from packages.subtitle_engine import load_captions, build_bilingual_ass, CaptionSegment

        segs = load_captions(src_sub)
        shifted = []
        for s in segs:
            a, b = s.start - offset, s.end - offset
            if b <= 0 or a >= duration:
                continue
            shifted.append(CaptionSegment(max(0.0, a), min(duration, b), s.text, s.words, s.text2))
        if not shifted:
            return None
        return build_bilingual_ass(shifted, out_path=out_sub)
    except Exception as e:
        print(f"  [预览] 字幕平移失败（跳过字幕）: {e}")
        return None


def build_param_preview(
    src: str,
    dst: str,
    start: float = 0.0,
    duration: float = 6.0,
    width: int = DEFAULT_WIDTH,
    crf: int = DEFAULT_CRF,
    color_preset: Optional[str] = None,
    subtitle_path: Optional[str] = None,
    with_audio: bool = True,
) -> str:
    """渲染一小段带参数的预览（调色 + 可选字幕）。

    分两/三小步做：片段小，所以每步都很快；换来的是能直接复用项目里
    已验证的调色 / 字幕逻辑，避免在预览里另写一套滤镜。
    """
    if not Path(src).exists():
        raise FileNotFoundError(f"视频不存在: {src}")
    dst = str(dst)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(dst).parent / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    seg = str(tmp_dir / f"seg_{Path(dst).stem}.mp4")

    # 1) 截取 + 缩小 + 低码率
    build_proxy(src, seg, width=width, crf=crf, max_duration=duration, start=start,
                mute=not with_audio)

    cur = seg

    # 2) 调色（复用 video_enhancement，作用于小片段很快）
    if color_preset and color_preset not in ("none", "neutral"):
        try:
            from packages.video_enhancement import apply_color_grade
            graded = str(tmp_dir / f"graded_{Path(dst).stem}.mp4")
            apply_color_grade(cur, graded, preset=color_preset)
            if Path(graded).exists():
                cur = graded
        except Exception as e:
            print(f"  [预览] 调色失败（跳过）: {e}")

    # 3) 可选字幕
    if subtitle_path and Path(subtitle_path).exists():
        ass = _shift_subtitles(
            subtitle_path, str(tmp_dir / f"sub_{Path(dst).stem}.ass"), start, duration)
        if ass:
            from packages.subtitle_engine import burn_ass
            subbed = str(tmp_dir / f"subbed_{Path(dst).stem}.mp4")
            try:
                burn_ass(cur, ass, subbed)
                if Path(subbed).exists():
                    cur = subbed
            except Exception as e:
                print(f"  [预览] 字幕烧录失败（跳过）: {e}")

    # 4) 收口：统一到目标路径
    if cur != dst:
        r = _run(["ffmpeg", "-y", "-i", cur, "-c", "copy", dst])
        if r.returncode != 0 or not Path(dst).exists():
            r = _run(["ffmpeg", "-y", "-i", cur, "-c:v", "libx264", "-crf", str(crf),
                      "-preset", DEFAULT_PRESET, "-c:a", "aac", "-b:a", "64k", dst])
            if r.returncode != 0:
                raise RuntimeError("预览生成失败:\n"
                                   + r.stderr.decode(errors="ignore")[-700:])
    return dst


def get_param_preview(
    src: str,
    start: float = 0.0,
    duration: float = 6.0,
    width: int = DEFAULT_WIDTH,
    crf: int = DEFAULT_CRF,
    color_preset: Optional[str] = None,
    subtitle_path: Optional[str] = None,
    cache_dir: Path = CACHE_DIR,
    force: bool = False,
) -> Dict[str, Any]:
    """获取（必要时生成）参数预览，同样按内容指纹缓存。"""
    params = {"kind": "param", "start": round(float(start), 2),
              "duration": round(float(duration), 2), "width": int(width),
              "crf": int(crf), "color": color_preset or "none",
              "sub": str(subtitle_path or "")}
    key = _cache_key(src, params)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"prev_{Path(src).stem}_{key}.mp4"

    cached = dst.exists() and not force
    t0 = time.time()
    if not cached:
        build_param_preview(src, str(dst), start=start, duration=duration,
                            width=width, crf=crf, color_preset=color_preset,
                            subtitle_path=subtitle_path)
    build_ms = int((time.time() - t0) * 1000)
    w, h = probe_size(str(dst))
    return {
        "path": str(dst),
        "cached": cached,
        "build_ms": build_ms,
        "width": w,
        "height": h,
        "duration": round(probe_duration(str(dst)), 2),
        "kbps": round(file_bitrate_kbps(str(dst)), 1),
        "key": key,
    }


def clear_cache(cache_dir: Path = CACHE_DIR) -> int:
    """清空预览缓存，返回删除文件数。"""
    n = 0
    if cache_dir.exists():
        for p in cache_dir.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
                n += 1
        for p in sorted(cache_dir.rglob("*"), reverse=True):
            if p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
    return n
