"""F1.5 冒烟测试：卡拉OK 字幕 + 词级时间戳链路

不依赖 whisper：直接用构造好的**词级 JSON** 走字幕压制链路，
验证 `CaptionBurner` 的 karaoke 逐字高亮与 CLI 参数暴露是否正确。

用法：
    python scripts/smoke_subtitles.py
产出：
    output/smoke/karaoke_demo.mp4
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def make_test_video(path: Path, seconds: int = 4) -> None:
    """生成一段带音轨的纯色测试视频（无需外部素材）"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x1a237e:s=640x360:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def make_word_json(path: Path) -> None:
    """构造含词级时间戳的 Whisper JSON"""
    words = [
        {"word": "AI", "start": 0.2, "end": 0.7},
        {"word": "混剪", "start": 0.7, "end": 1.3},
        {"word": "逐字", "start": 1.4, "end": 2.0},
        {"word": "高亮", "start": 2.0, "end": 2.6},
        {"word": "字幕", "start": 2.7, "end": 3.4},
    ]
    data = {
        "segments": [{
            "start": 0.2,
            "end": 3.6,
            "text": "AI 混剪 逐字 高亮 字幕",
            "words": words,
        }]
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check_cli_exposes_karaoke() -> bool:
    """静态检查：CLI 的 --subtitles 是否暴露 karaoke"""
    src = (ROOT / "pipeline.py").read_text(encoding="utf-8")
    ok = '"karaoke"' in src
    print(f"  CLI 暴露 karaoke: {'✓' if ok else '✗'}")
    return ok


def main() -> int:
    argparse.ArgumentParser(description="F1.5 字幕冒烟测试").parse_args()
    print("=" * 50)
    print("F1.5 冒烟：卡拉OK 字幕链路")
    print("=" * 50)

    video = OUT_DIR / "karaoke_input.mp4"
    cap_json = OUT_DIR / "karaoke_input.json"
    out_video = OUT_DIR / "karaoke_demo.mp4"

    print("\n[1/4] 生成测试视频...")
    make_test_video(video)
    print(f"  {video}")

    print("[2/4] 构造词级 JSON...")
    make_word_json(cap_json)
    print(f"  {cap_json}")

    print("[3/4] 检查 CLI 参数暴露...")
    cli_ok = check_cli_exposes_karaoke()

    print("[4/4] 压制卡拉OK字幕...")
    from packages.subtitle_engine import burn_captions, load_captions

    segs = load_captions(str(cap_json))
    has_words = bool(segs and segs[0].words)
    print(f"  解析到 {len(segs)} 段，词级时间戳: {'有' if has_words else '无'}")
    assert has_words, "JSON 未保留词级时间戳"

    burn_captions(str(video), str(cap_json), style="karaoke", output_path=str(out_video))
    assert out_video.exists() and out_video.stat().st_size > 1000, "karaoke 成片未生成"
    print(f"  输出: {out_video} ({out_video.stat().st_size} bytes)")

    print("\n" + "=" * 50)
    if cli_ok and has_words and out_video.exists():
        print("冒烟通过 ✓")
        return 0
    print("冒烟失败 ✗")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
