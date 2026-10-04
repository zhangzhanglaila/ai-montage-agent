"""F1.3 冒烟测试：封面 / 缩略图生成

自包含，不需要外部素材：
    1. 合成一段有画面内容的测试视频（testsrc2 有运动纹理，便于清晰度打分）
    2. 自动挑最佳帧
    3. 叠加中文标题生成 16:9 / 9:16 封面
    4. 校验尺寸正确、且文字确实被画上去（底部文字带出现高亮像素）

用法：
    python scripts/smoke_cover.py
产出：
    output/smoke/cover_16x9.png
    output/smoke/cover_9x16.png
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def make_test_video(path: Path, seconds: int = 8) -> None:
    """合成一段有纹理运动的测试视频（够清晰，能被打分挑出）。"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _text_band_brightness(img_path: Path, aspect: str, band_ratio: float = 0.28) -> int:
    """统计封面底部文字带里的亮像素数量，用来证明文字被画上了。"""
    from PIL import Image

    with Image.open(img_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        top = int(h * (1 - band_ratio))
        band = im.crop((0, top, w, h))
        px = band.load()
        bright = 0
        for y in range(0, band.height, 4):
            for x in range(0, band.width, 4):
                r, g, b = px[x, y]
                if r > 235 and g > 235 and b > 235:
                    bright += 1
        return bright


def main() -> int:
    argparse.ArgumentParser(description="F1.3 封面生成冒烟测试").parse_args()
    print("=" * 50)
    print("F1.3 冒烟：封面 / 缩略图生成")
    print("=" * 50)

    video = OUT_DIR / "cover_input.mp4"
    cover_169 = OUT_DIR / "cover_16x9.png"
    cover_916 = OUT_DIR / "cover_9x16.png"

    print("\n[1/4] 生成测试视频...")
    make_test_video(video)
    print(f"  {video}")

    from packages.cover_engine import (
        pick_best_frame, make_cover, generate_cover, ASPECT_SIZES,
    )

    print("[2/4] 自动挑最佳帧...")
    frame = pick_best_frame(str(video), top_n=8)
    assert frame and Path(frame).exists(), "未挑出有效帧"
    print(f"  最佳帧: {frame}")

    print("[3/4] 生成 16:9 封面...")
    out1 = generate_cover(
        str(video), title="AI 自动混剪实战", subtitle="3 分钟看懂全流程",
        size="16:9", style="bold", out_path=str(cover_169),
    )
    assert out1 and Path(out1).exists(), "16:9 封面未生成"
    from PIL import Image
    with Image.open(out1) as im:
        assert im.size == ASPECT_SIZES["16:9"], f"尺寸错误: {im.size}"
    band1 = _text_band_brightness(Path(out1), "16:9")
    assert band1 > 20, f"底部文字带上未检测到文字像素（bright={band1}）"
    print(f"  {out1} 尺寸={ASPECT_SIZES['16:9']} 文字带亮像素={band1}")

    print("[4/4] 生成 9:16 封面（复用心跳帧）...")
    out2 = make_cover(
        frame, title="竖屏封面测试", subtitle="短视频封面",
        size="9:16", style="cinematic", out_path=str(cover_916),
    )
    assert out2 and Path(out2).exists(), "9:16 封面未生成"
    with Image.open(out2) as im:
        assert im.size == ASPECT_SIZES["9:16"], f"尺寸错误: {im.size}"
    band2 = _text_band_brightness(Path(out2), "9:16")
    assert band2 > 20, f"9:16 文字带上未检测到文字像素（bright={band2}）"
    print(f"  {out2} 尺寸={ASPECT_SIZES['9:16']} 文字带亮像素={band2}")

    print("\n" + "=" * 50)
    print("冒烟通过 ✓ （16:9 与 9:16 封面均已生成且含标题）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
