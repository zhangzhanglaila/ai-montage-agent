"""
自动竖屏裁切 - 16:9 → 9:16（主体轨迹跟随）

与原实现的关键差异
------------------
原实现依赖 OpenCV（`cv2` 人脸 + 光流），环境没装就直接 ImportError；且其动态
裁切表达式写成 `if(between(t,a,b),x,x)` 再相加，**恒等于 x**，实际从未跟随主体。

现改为：
1. 主体定位走 `packages.video_understanding.src.subject_tracker`
   （纯 numpy 显著性 + 运动 + 滑动窗口，cv2 仅作可选增强）；
2. 裁切路径生成**分段线性插值表达式** `x(t)`，交给 ffmpeg 的 `crop` 时间表达式，
   一次编码完成平滑跟随（已验证 `crop=w:h:x='...':y='...'` 可用）；
3. 全程失败时回退静态居中裁切，保证不阻塞出片。

用法：
    from packages.video_enhancement.src.auto_reframe import auto_reframe
    auto_reframe("input.mp4", "output_9x16.mp4")            # 主体跟随
    auto_reframe("in.mp4", "out.mp4", method="center")      # 静态居中
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class CropRegion:
    """裁切区域"""
    x: int
    y: int
    width: int
    height: int


# --------------------------------------------------------------- 视频信息
def get_video_info(video_path: str) -> dict:
    """获取视频信息（宽/高/时长/帧率）"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=width,height,duration,r_frame_rate",
        "-of", "json", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        rate = stream.get("r_frame_rate", "30/1")
        num, _, den = rate.partition("/")
        fps = float(num) / float(den or 1)
    except Exception:
        stream, fps = {}, 30.0
    return {
        "width": int(stream.get("width", 1920) or 1920),
        "height": int(stream.get("height", 1080) or 1080),
        "duration": float(stream.get("duration", 0) or 0),
        "fps": fps or 30.0,
    }


def compute_crop_window(src_w: int, src_h: int, target_aspect: float) -> Tuple[int, int]:
    """按目标宽高比算出裁切窗口尺寸（尽量占满，且保持偶数）。"""
    crop_w = int(round(src_h * target_aspect))
    crop_h = src_h
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(round(src_w / target_aspect))
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    return max(2, crop_w), max(2, crop_h)


# ----------------------------------------------------------- 表达式构建
def build_crop_expression(
    times: Sequence[float], values: Sequence[float], max_keyframes: int = 60,
) -> str:
    """把 [(t, v)] 折线编译成 ffmpeg 可用的分段线性表达式。

    ffmpeg 的 ``crop`` 滤镜 ``x``/``y`` 支持含 ``t`` 的表达式，因此一次编码即可
    得到连续平滑的跟随位移（而非分段落跳变）。

    例子（两点）::

        build_crop_expression([0, 2], [0, 100])
        -> "if(lt(t,2.000),(0.00+50.00000*(t-0.000)),100.00)"
    """
    pts: List[Tuple[float, float]] = []
    for t, v in zip(times, values):
        t, v = float(t), float(v)
        if pts and t <= pts[-1][0]:
            continue
        pts.append((t, v))
    if not pts:
        return "0"
    if len(pts) == 1 or all(abs(p[1] - pts[0][1]) < 1e-6 for p in pts):
        return f"{pts[0][1]:.2f}"

    if len(pts) > max_keyframes > 1:
        idx = sorted(set(np.linspace(0, len(pts) - 1, max_keyframes).round().astype(int)))
        pts = [pts[i] for i in idx]

    def rec(i: int) -> str:
        if i >= len(pts) - 1:
            return f"{pts[-1][1]:.2f}"
        t0, v0 = pts[i]
        t1, v1 = pts[i + 1]
        if t1 <= t0:
            return rec(i + 1)
        slope = (v1 - v0) / (t1 - t0)
        if abs(slope) < 1e-6:
            seg = f"{v0:.2f}"
        else:
            seg = f"({v0:.2f}+{slope:.5f}*(t-{t0:.3f}))"
        return f"if(lt(t,{t1:.3f}),{seg},{rec(i + 1)})"

    return rec(0)


def track_to_pixel_series(
    track, crop_w: int, crop_h: int, src_w: int, src_h: int,
) -> Tuple[List[float], List[float]]:
    """把归一化主体轨迹换算成裁切窗口左上角像素序列（已钳制在合法范围）。"""
    if track is None or len(track) == 0:
        return [(src_w - crop_w) / 2.0], [(src_h - crop_h) / 2.0]
    max_x = max(0.0, src_w - crop_w)
    max_y = max(0.0, src_h - crop_h)
    xs, ys = [], []
    for cx, cy in zip(track.cx, track.cy):
        xs.append(min(max_x, max(0.0, cx * src_w - crop_w / 2.0)))
        ys.append(min(max_y, max(0.0, cy * src_h - crop_h / 2.0)))
    return xs, ys


def crop_track_to_pixels(
    track, crop_w: int, crop_h: int, src_w: int, src_h: int,
    max_keyframes: int = 60,
) -> Tuple[str, str]:
    """把归一化主体轨迹转成 ffmpeg 的 x(t)/y(t) 表达式（已钳制在合法范围）。"""
    xs, ys = track_to_pixel_series(track, crop_w, crop_h, src_w, src_h)
    if track is None or len(track) == 0:
        return f"{xs[0]:.2f}", f"{ys[0]:.2f}"
    x_expr = build_crop_expression(track.times, xs, max_keyframes)
    y_expr = build_crop_expression(track.times, ys, max_keyframes)
    return x_expr, y_expr


# ------------------------------------------------------------------ 主流程
def _static_crop_filter(src_w: int, src_h: int, crop_w: int, crop_h: int,
                        scale: str = "") -> str:
    x = (src_w - crop_w) // 2
    y = (src_h - crop_h) // 2
    f = f"crop={crop_w}:{crop_h}:{x}:{y}"
    return f"{f},{scale}" if scale else f


def auto_reframe(
    input_path: str,
    output_path: str,
    target_aspect: float = 9 / 16,
    output_width: int = 1080,
    output_height: int = 1920,
    method: str = "subject",
    sample_fps: float = 4.0,
    smooth_alpha: float = 0.35,
    motion_weight: float = 0.45,
    max_keyframes: int = 60,
) -> str:
    """自动竖屏裁切。

    Args:
        input_path: 输入视频
        output_path: 输出视频
        target_aspect: 裁切窗口宽高比（默认 9/16=竖屏）
        output_width / output_height: 输出分辨率
        method: ``subject`` 主体跟随（默认）/ ``center`` 静态居中
        sample_fps: 轨迹采样帧率
        smooth_alpha: 轨迹 EMA 平滑系数（越小越稳、越大越灵敏）
        motion_weight: 运动能量权重（0 纯显著性，1 纯运动）
        max_keyframes: 裁切表达式关键点上限（控制表达式长度）

    Returns:
        输出文件路径
    """
    info = get_video_info(input_path)
    src_w, src_h = info["width"], info["height"]
    scale_filter = f"scale={output_width}:{output_height}:flags=lanczos"

    # 已经是竖屏：直接缩放，不裁
    if src_w <= src_h:
        print(f"  自动裁切: 源已是竖屏，直接缩放 -> {output_path}")
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-vf", scale_filter,
             "-c:v", "libx264", "-crf", "23", "-preset", "medium",
             "-c:a", "copy", output_path],
            capture_output=True,
        )
        return output_path

    crop_w, crop_h = compute_crop_window(src_w, src_h, target_aspect)
    print(f"  自动裁切: {src_w}x{src_h} -> crop {crop_w}x{crop_h} -> {output_width}x{output_height}")

    crop_filter = None
    if method == "subject":
        print("  追踪主体轨迹...")
        try:
            from packages.video_understanding.src.subject_tracker import SubjectTracker

            tracker = SubjectTracker(
                sample_fps=sample_fps,
                smooth_alpha=smooth_alpha,
                motion_weight=motion_weight,
            )
            track = tracker.track(
                input_path,
                win_w_frac=crop_w / src_w,
                win_h_frac=crop_h / src_h,
            )
            if track is not None and len(track) >= 2:
                x_expr, y_expr = crop_track_to_pixels(
                    track, crop_w, crop_h, src_w, src_h, max_keyframes)
                crop_filter = f"crop={crop_w}:{crop_h}:x='{x_expr}':y='{y_expr}'"
                span = max(track.cx) - min(track.cx)
                print(f"  主体轨迹: {len(track)} 个采样点，水平移动幅度 {span * src_w:.0f}px")
            else:
                print("  未取得有效轨迹，改用静态居中裁切")
        except Exception as e:
            print(f"  主体追踪失败（{type(e).__name__}: {e}），改用静态居中裁切")

    if crop_filter is None:
        crop_filter = _static_crop_filter(src_w, src_h, crop_w, crop_h)

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"{crop_filter},{scale_filter}",
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-c:a", "copy", output_path,
    ]
    print("  执行裁切...")
    r = subprocess.run(cmd, capture_output=True, text=True)

    if r.returncode != 0 or not Path(output_path).exists():
        print("  动态裁切失败，回退居中裁切")
        static = _static_crop_filter(src_w, src_h, crop_w, crop_h, scale_filter)
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-vf", static,
             "-c:v", "libx264", "-crf", "23", "-preset", "medium",
             "-c:a", "copy", output_path],
            capture_output=True, check=True,
        )

    print(f"  裁切完成: {output_path}")
    return output_path


def export_crop_track(
    input_path: str, out_json: str,
    target_aspect: float = 9 / 16, **tracker_kwargs,
) -> Optional[str]:
    """把主体轨迹导出成 JSON，便于调试 / 在别处复用。"""
    from packages.video_understanding.src.subject_tracker import SubjectTracker

    info = get_video_info(input_path)
    crop_w, crop_h = compute_crop_window(info["width"], info["height"], target_aspect)
    track = SubjectTracker(**tracker_kwargs).track(
        input_path, win_w_frac=crop_w / info["width"], win_h_frac=crop_h / info["height"])
    if track is None:
        return None
    payload = {
        "video": input_path, "width": track.width, "height": track.height,
        "crop": {"w": crop_w, "h": crop_h}, "fps": track.fps,
        "points": [{"t": t, "cx": round(cx, 4), "cy": round(cy, 4)}
                   for t, cx, cy in zip(track.times, track.cx, track.cy)],
    }
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  轨迹已导出: {out_json}")
    return out_json
