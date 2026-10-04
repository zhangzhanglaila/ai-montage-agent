"""F1.4 冒烟测试：字幕翻译 / 双语字幕

自包含，不需要 whisper，也不需要联网：
    1. 合成一段纯色测试视频
    2. 手工构造中文字幕段（含时间码）
    3. 用自定义 translator（内置小词典）翻译成英文，
       验证 translate_segments 保留时间码且行数一致
    4. 生成 ASS 并烧进视频（原文在上、译文在下）
    5. 抽帧校验画面里确实出现了上下两行文字

用法：
    python scripts/smoke_translate.py
产出：
    output/smoke/translate_demo.mp4
    output/smoke/translate_check.png
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 极简离线词典：仅用于冒烟，证明"原文/译文"两行都渲染出来
FAKE_DICT = {
    "今天聊聊 AI 混剪。": "Let's talk about AI montage today.",
    "先看第一个细节。": "First, look at this detail.",
    "节奏开始加快了。": "The pace is picking up.",
    "这就是它的魅力。": "That's the charm of it.",
}


def fake_translator(texts):
    """模拟翻译后端的批量接口：返回等长译文列表。"""
    return [FAKE_DICT.get(t, f"[EN] {t}") for t in texts]


def make_test_video(path: Path, seconds: int = 4) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x1a1a2e:s=1280x720:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _row_stats(img_path: Path, y0: float, y1: float) -> tuple:
    """统计横向条带内的「白色文字像素」与「黄色文字像素」。

    背景是深色底（0x1a1a2e），因此按颜色区分：
      - 原文行：接近白色的高亮像素
      - 译文行：偏黄的像素（ASS 里译文用 FFE066）
    """
    from PIL import Image

    with Image.open(img_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        band = im.crop((0, int(h * y0), w, int(h * y1)))
        px = band.load()
        white = 0
        yellow = 0
        for y in range(0, band.height, 2):
            for x in range(0, band.width, 2):
                r, g, b = px[x, y]
                if r > 200 and g > 200 and b > 200:
                    white += 1
                elif r > 180 and g > 140 and b < 140:
                    yellow += 1
        return white, yellow


def main() -> int:
    argparse.ArgumentParser(description="F1.4 字幕翻译冒烟测试").parse_args()
    print("=" * 50)
    print("F1.4 冒烟：字幕翻译 / 双语字幕")
    print("=" * 50)

    video = OUT_DIR / "translate_input.mp4"
    out_video = OUT_DIR / "translate_demo.mp4"
    check_png = OUT_DIR / "translate_check.png"

    print("\n[1/4] 生成测试视频...")
    make_test_video(video)
    print(f"  {video}")

    from packages.subtitle_engine import (
        CaptionSegment, CaptionStyle, CaptionBurner,
        translate_segments, build_bilingual_ass,
    )

    print("[2/4] 构造中文字幕段并翻译...")
    src = [
        CaptionSegment(0.2, 1.2, "今天聊聊 AI 混剪。"),
        CaptionSegment(1.2, 2.2, "先看第一个细节。"),
        CaptionSegment(2.2, 3.2, "节奏开始加快了。"),
        CaptionSegment(3.2, 4.0, "这就是它的魅力。"),
    ]
    bilingual = translate_segments(src, target="en", translator=fake_translator)
    assert len(bilingual) == len(src), "译文行数与原文不一致"
    for a, b in zip(src, bilingual):
        assert (a.start, a.end) == (b.start, b.end), "时间码未保留"
        assert b.text2, "译文为空"
        print(f"    {b.start:.1f}-{b.end:.1f}  原:{b.text}  译:{b.text2}")

    print("[3/4] 生成 ASS 并烧录...")
    ass_path = OUT_DIR / "translate_demo.ass"
    build_bilingual_ass(bilingual, out_path=str(ass_path), video_size=(1280, 720), target_lang="en")
    assert Path(ass_path).exists(), "ASS 未生成"
    ass_lines = [l for l in Path(ass_path).read_text(encoding="utf-8-sig").splitlines()
                 if l.startswith("Dialogue:")]
    assert len(ass_lines) == len(src) * 2, f"ASS 事件数应为 {len(src) * 2}，实际 {len(ass_lines)}"
    print(f"  {ass_path}（{len(ass_lines)} 条对话事件）")

    burner = CaptionBurner(style=CaptionStyle.BILINGUAL)
    burner.burn_segments(bilingual, str(video), str(out_video))
    assert out_video.exists() and out_video.stat().st_size > 1000, "双语成片未生成"
    print(f"  {out_video} ({out_video.stat().st_size} bytes)")

    print("[4/4] 抽帧校验上下两行文字...")
    subprocess.run([
        "ffmpeg", "-y", "-ss", "0.4", "-i", str(out_video),
        "-frames:v", "1", "-update", "1", str(check_png),
    ], capture_output=True, check=True)
    # 原文行（白）在上方条带，译文行（黄）在下方条带
    up_white, _ = _row_stats(check_png, 0.74, 0.83)
    _, low_yellow = _row_stats(check_png, 0.83, 0.96)
    assert up_white > 10, f"未检测到原文行白色文字（white={up_white}）"
    assert low_yellow > 10, f"未检测到译文行黄色文字（yellow={low_yellow}）"
    print(f"  {check_png}  原文行白像素={up_white}  译文行黄像素={low_yellow}")

    print("\n" + "=" * 50)
    print("冒烟通过 ✓ （原文/译文双行均已渲染，时间码保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
