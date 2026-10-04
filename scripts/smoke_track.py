"""F2.2 冒烟测试：主体追踪 + 竖屏跟随裁切

验证方式（刻意做成可证伪的）：
    A. 合成一段「白色方块从左到右匀速移动」的视频，追踪器算出的 cx 必须与
       真实位置**强相关**（否则说明追踪没在工作）；
    B. 生成的分段线性裁切表达式 x(t) 必须随时间单调递增；
    C. auto_reframe 在合成视频与**真实素材**上都能产出 1080x1920 竖屏成片；
    D. 静态居中法（method="center"）作为对照也要能出片。

用法：
    python scripts/smoke_track.py
产出：
    output/smoke/track_src.mp4 / track_follow.mp4 / track_center.mp4 / track_real.mp4
"""

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)

REAL_CANDIDATES = [
    ROOT / "output" / "test_montage8.mp4",
    ROOT / "output" / "demo_final.mp4",
]


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def make_moving_subject(path: Path, seconds: int = 8, w: int = 1280, h: int = 720) -> None:
    """合成：黑底 + 白色方块从左到右匀速移动（主体位置已知，可做相关性断言）。

    注：`drawbox` 的时间表达式在本机 ffmpeg 上不生效（画面全黑），
    改用 `overlay` + `x='(W-w)*t/T'`，实测位移正确。
    """
    box_w, box_h = 160, 320
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:d={seconds}:r=25",
        "-f", "lavfi", "-i", f"color=c=white:s={box_w}x{box_h}:d={seconds}:r=25",
        "-filter_complex",
        f"[0][1]overlay=x='(W-w)*t/{seconds}':y='(H-h)/2'",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ]
    r = _run(cmd)
    if r.returncode != 0 or not path.exists():
        raise RuntimeError(f"合成测试视频失败: {r.stderr[-500:]}")
    # 自检：确认方块真的在动
    a, b = _frame_bright_cols(path, 0.4), _frame_bright_cols(path, seconds - 1.0)
    if not a or not b or b[0] <= a[0]:
        raise RuntimeError(f"合成视频中的主体未移动: {a} -> {b}")


def _frame_bright_cols(video: Path, tt: float):
    png = OUT_DIR / "_chk.png"
    _run(["ffmpeg", "-y", "-ss", str(tt), "-i", str(video),
          "-frames:v", "1", "-update", "1", str(png)])
    from PIL import Image
    with Image.open(png) as im:
        a = np.asarray(im.convert("L"), dtype=np.float32)
    cols = np.where(a.max(axis=0) > 100)[0]
    return (int(cols.min()), int(cols.max())) if len(cols) else None


def probe_size(path: Path) -> tuple:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)])
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def main() -> int:
    ap = argparse.ArgumentParser(description="F2.2 主体追踪冒烟测试")
    ap.add_argument("--real", default=None, help="真实素材路径（默认自动挑）")
    args = ap.parse_args()

    print("=" * 54)
    print("F2.2 冒烟：主体追踪 + 竖屏跟随裁切")
    print("=" * 54)

    from packages.video_understanding.src.subject_tracker import SubjectTracker
    from packages.video_enhancement.src.auto_reframe import (
        auto_reframe, crop_track_to_pixels, track_to_pixel_series, compute_crop_window,
    )

    src = OUT_DIR / "track_src.mp4"
    print("\n[1/5] 合成「方块左→右移动」视频...")
    make_moving_subject(src)
    print(f"  {src}")

    print("[2/5] 追踪并做相关性断言...")
    track = SubjectTracker(sample_fps=8.0, smooth_alpha=0.5).track(
        str(src), win_w_frac=0.5625, win_h_frac=1.0)
    assert track is not None and len(track) >= 8, "轨迹采样点不足"
    cx = np.asarray(track.cx)
    t = np.asarray(track.times)
    assert cx.min() >= -0.01 and cx.max() <= 1.01, f"cx 越界: {cx.min()}/{cx.max()}"
    corr = float(np.corrcoef(cx, t)[0, 1])
    print(f"  采样点={len(track)}  cx范围=[{cx.min():.3f},{cx.max():.3f}]  与时间相关系数={corr:.3f}")
    assert corr > 0.8, f"追踪未跟随主体（corr={corr:.3f} 应 > 0.8）"

    print("[3/5] 校验裁切路径与表达式...")
    crop_w, crop_h = compute_crop_window(1280, 720, 9 / 16)
    xs, ys = track_to_pixel_series(track, crop_w, crop_h, 1280, 720)
    mid = len(xs) // 2
    print(f"  裁切窗口 x 像素: 首={xs[0]:.0f} 中={xs[mid]:.0f} 末={xs[-1]:.0f}（上限 {1280 - crop_w}）")
    assert xs[0] < xs[mid] < xs[-1], "裁切窗口未随主体右移"
    assert 0 <= min(xs) and max(xs) <= 1280 - crop_w + 1e-6, "裁切 x 越出合法范围"

    x_expr, y_expr = crop_track_to_pixels(track, crop_w, crop_h, 1280, 720)
    assert "if(lt(t," in x_expr and "*(t-" in x_expr, \
        f"x 表达式不是时间函数: {x_expr[:90]}"
    print(f"  x(t) 表达式长度={len(x_expr)} 字符，前 80 字符: {x_expr[:80]}")

    print("[4/5] 合成视频跑 auto_reframe（subject + center）...")
    follow = OUT_DIR / "track_follow.mp4"
    auto_reframe(str(src), str(follow), method="subject")
    assert follow.exists() and probe_size(follow) == (1080, 1920), "竖屏成片尺寸不对"
    print(f"  subject -> {probe_size(follow)}")

    center = OUT_DIR / "track_center.mp4"
    auto_reframe(str(src), str(center), method="center")
    assert center.exists() and probe_size(center) == (1080, 1920)
    print(f"  center  -> {probe_size(center)}")

    # 跟随裁切的画面中心应逐渐变亮（方块进入画面中心）
    def center_brightness(video: Path, tt: float) -> float:
        png = OUT_DIR / f"_cb_{tt}.png"
        _run(["ffmpeg", "-y", "-ss", str(tt), "-i", str(video),
              "-frames:v", "1", "-update", "1", str(png)])
        from PIL import Image
        with Image.open(png) as im:
            a = np.asarray(im.convert("L"), dtype=np.float32)
            h, w = a.shape
            return float(a[h // 3:2 * h // 3, w // 3:2 * w // 3].mean())

    b_early = center_brightness(follow, 0.3)
    b_mid = center_brightness(follow, 4.0)
    print(f"  跟随裁切后画面中心亮度: t=0.3 -> {b_early:.1f},  t=4.0 -> {b_mid:.1f}")
    assert b_mid > b_early, "跟随裁切后主体未进入画面中心（中心亮度没上升）"

    print("[5/5] 真实素材端到端...")
    real = None
    if args.real:
        real = Path(args.real)
    else:
        for c in REAL_CANDIDATES:
            if c.exists() and c.stat().st_size > 100_000:
                real = c
                break
    if real is not None:
        out_real = OUT_DIR / "track_real.mp4"
        auto_reframe(str(real), str(out_real), method="subject", max_keyframes=40)
        assert out_real.exists() and probe_size(out_real) == (1080, 1920)
        print(f"  {real.name} -> {probe_size(out_real)}")
    else:
        print("  （无真实素材，跳过）")

    print("\n" + "=" * 54)
    print("冒烟通过 ✓ （跟随相关性 / 表达式单调 / 竖屏输出 / 中心亮度上升）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
