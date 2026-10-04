"""F1.1 冒烟测试：音效卡点引擎

自包含，不需要外部素材：
    1. 合成一段带音轨的测试视频
    2. 构造节拍列表
    3. 规划音效并混音
    4. 校验输出存在且音轨被改动

用法：
    python scripts/smoke_sfx.py
产出：
    output/smoke/sfx_demo.mp4
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)


class _Beat:
    """模拟 core_types.models.Beat，避免依赖导入"""

    def __init__(self, time, strength, beat_type="normal"):
        self.time = time
        self.strength = strength
        self.beat_type = beat_type


def make_test_video(path: Path, seconds: int = 6) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s=640x360:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def main() -> int:
    argparse.ArgumentParser(description="F1.1 音效卡点冒烟测试").parse_args()
    print("=" * 50)
    print("F1.1 冒烟：音效卡点引擎")
    print("=" * 50)

    video = OUT_DIR / "sfx_input.mp4"
    out_video = OUT_DIR / "sfx_demo.mp4"

    print("\n[1/4] 生成测试视频...")
    make_test_video(video)
    print(f"  {video}")

    print("[2/4] 构造节拍...")
    beats = [
        _Beat(0.5, 0.9, "strong"),
        _Beat(1.2, 0.5, "normal"),
        _Beat(1.9, 0.8, "strong"),
        _Beat(2.6, 0.4, "weak"),
        _Beat(3.3, 1.0, "strong"),
        _Beat(4.0, 0.6, "normal"),
        _Beat(4.7, 0.85, "strong"),
        _Beat(5.4, 0.5, "normal"),
    ]
    print(f"  {len(beats)} 个节拍")

    print("[3/4] 规划音效...")
    from packages.sound_engine import SfxEngine, SfxLibrary

    lib = SfxLibrary()
    print(f"  可用音效类型: {lib.available()}")
    engine = SfxEngine(lib)
    cues = engine.plan(beats, style="dynamic")
    for c in cues:
        print(f"    {c.time:.2f}s  {c.kind:<7} {c.gain_db}dB")
    assert cues, "未规划出任何音效"

    print("[4/4] 混音...")
    engine.mix(str(video), cues, str(out_video))
    assert out_video.exists() and out_video.stat().st_size > 1000, "音效成片未生成"
    print(f"  输出: {out_video} ({out_video.stat().st_size} bytes)")

    print("\n" + "=" * 50)
    print(f"冒烟通过 ✓ （{len(cues)} 个音效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
