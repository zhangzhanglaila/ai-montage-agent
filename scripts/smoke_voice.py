"""F1.2 冒烟测试：配音旁白引擎

自包含、可离线运行：
    1. 脚本生成（LLM 不可用时走模板兜底）
    2. 旁白混音（用 ffmpeg 合成一段音频模拟 TTS 产物）
    3. 校验成片音轨时长与视频一致（闪避链路生效）
    4. 若 edge-tts 且网络可用，额外实测一次真实 TTS（失败不判负）

用法：
    python scripts/smoke_voice.py
产出：
    output/smoke/voice_demo.mp4
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
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s=640x360:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def make_fake_narration(path: Path, seconds: float = 4.0) -> None:
    """用 ffmpeg 合成一段音频，模拟 TTS 产物（离线可跑）"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=660:duration={seconds}",
        "-ar", "44100", "-ac", "2",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def main() -> int:
    argparse.ArgumentParser(description="F1.2 配音旁白冒烟测试").parse_args()
    print("=" * 50)
    print("F1.2 冒烟：配音旁白引擎")
    print("=" * 50)

    video = OUT_DIR / "voice_input.mp4"
    narration = OUT_DIR / "voice_narration.wav"
    out_video = OUT_DIR / "voice_demo.mp4"

    print("\n[1/5] 生成脚本（离线兜底）...")
    from packages.voice_engine import ScriptWriter, mix_narration, probe_duration
    writer = ScriptWriter()
    lines = writer.offline("AI 混剪", target_sec=20)
    assert lines, "脚本为空"
    print(f"  {len(lines)} 句，预计 {ScriptWriter.total_duration(lines)}s")
    for ln in lines[:3]:
        print(f"    · {ln.text}")

    print("[2/5] 校验文本解析...")
    parsed = writer.from_text("1. 第一句。第二句！\n2. 第三句？")
    print(f"  解析出 {len(parsed)} 句: {[p.text for p in parsed]}")
    assert len(parsed) == 3, "文本解析数量不符"

    print("[3/5] 生成测试视频与模拟旁白...")
    make_test_video(video)
    make_fake_narration(narration)
    print(f"  视频: {probe_duration(str(video)):.2f}s  旁白: {probe_duration(str(narration)):.2f}s")

    print("[4/5] 混音（含 BGM 闪避）...")
    mix_narration(str(video), str(narration), str(out_video), duck=True, duck_db=-9.0)
    assert out_video.exists() and out_video.stat().st_size > 1000, "旁白成片未生成"
    v_dur = probe_duration(str(video))
    o_dur = probe_duration(str(out_video))
    print(f"  输出: {out_video} ({out_video.stat().st_size} bytes)")
    print(f"  时长: 输入 {v_dur:.2f}s -> 输出 {o_dur:.2f}s")
    assert abs(v_dur - o_dur) < 0.6, "输出时长与输入不一致（混音链路异常）"

    print("[5/5] 真实 TTS 实测（可选）...")
    from packages.voice_engine import TtsEngine
    if not TtsEngine.available():
        print("  未安装 edge-tts，跳过（pip install edge-tts）")
    else:
        try:
            tts = TtsEngine()
            real = OUT_DIR / "voice_tts.mp3"
            tts.synthesize("AI 混剪，逐帧高光。", str(real))
            print(f"  真实 TTS 成功: {real} ({probe_duration(str(real)):.2f}s)")
        except Exception as e:
            print(f"  真实 TTS 不可用（不影响冒烟）: {e}")

    print("\n" + "=" * 50)
    print("冒烟通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
