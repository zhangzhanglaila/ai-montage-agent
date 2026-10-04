"""用**真实素材**生成一个可交付的演示成片。

与 `scripts/smoke_*.py`（合成测试夹具）不同，本脚本产出的是可观看效果的
演示：真实画面 + 真实音频 + 配音旁白（含 BGM 闪避）+ 中文逐字字幕。

用法：
    python scripts/make_demo.py                       # 用默认素材
    python scripts/make_demo.py --video xxx.mp4 --duration 18
产出：
    output/demo/demo_real.mp4
    output/demo/demo_cover.jpg（如 opencv/pillow 可用）
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "demo"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 默认素材：仓库内已有的真实混剪（含真实音频）
DEFAULT_VIDEO = ROOT / "output" / "test_montage8.mp4"


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def has_audio(path: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def clip_video(src: Path, dst: Path, start: float, duration: float) -> None:
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(duration),
           "-c:v", "libx264", "-preset", "fast", "-crf", "20",
           "-c:a", "aac", "-b:a", "192k", str(dst)]
    subprocess.run(cmd, capture_output=True, check=True)


def build_word_json(lines, narration_duration: float, out_path: Path) -> None:
    """按字数比例把旁白时长分配到每个字，生成逐字时间戳 JSON。

    说明：edge-tts 对中文不返回 WordBoundary 事件，这里按字数比例近似分配。
    """
    total_est = sum(l.est_duration for l in lines) or 1.0
    scale = narration_duration / total_est

    segments = []
    t = 0.0
    for line in lines:
        chars = [c for c in line.text if not c.isspace()]
        if not chars:
            continue
        d = line.est_duration * scale
        per = d / len(chars)
        words, ct = [], t
        for c in chars:
            words.append({"word": c, "start": round(ct, 3), "end": round(ct + per, 3)})
            ct += per
        segments.append({
            "start": round(t, 3), "end": round(ct, 3),
            "text": "".join(chars), "words": words,
        })
        t = ct

    out_path.write_text(json.dumps({"segments": segments}, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成真实素材演示")
    ap.add_argument("--video", default=str(DEFAULT_VIDEO), help="演示用素材视频")
    ap.add_argument("--duration", type=float, default=18.0, help="截取时长（秒）")
    ap.add_argument("--start", type=float, default=0.0, help="截取起点（秒）")
    ap.add_argument("--topic", default="武侠混剪", help="旁白主题")
    ap.add_argument("--voice", default=None, help="TTS 音色")
    args = ap.parse_args()

    print("=" * 50)
    print("生成真实素材演示")
    print("=" * 50)

    src = Path(args.video)
    if not src.exists():
        print(f"素材不存在: {src}")
        return 1
    src_dur = probe_duration(src)
    print(f"素材: {src.name}  时长 {src_dur:.1f}s  音轨: {'有' if has_audio(src) else '无'}")

    # 1) 截取
    base = OUT_DIR / "demo_base.mp4"
    dur = min(args.duration, src_dur - args.start)
    clip_video(src, base, args.start, dur)
    print(f"[1/4] 截取 {dur:.1f}s -> {base.name}")

    # 2) 旁白脚本 + TTS
    from packages.voice_engine import ScriptWriter, TtsEngine
    writer = ScriptWriter()
    lines = writer.offline(args.topic, target_sec=int(dur))
    script_text = ScriptWriter.to_text(lines)
    print(f"[2/4] 旁白 {len(lines)} 句:")
    for l in lines:
        print(f"      · {l.text}")

    narration = OUT_DIR / "demo_narration.mp3"
    tts = TtsEngine(voice=args.voice)
    tts.synthesize(script_text, str(narration))
    narr_dur = probe_duration(narration)
    print(f"      旁白音频 {narr_dur:.1f}s")

    # 3) 旁白混音（含 BGM 闪避）
    from packages.voice_engine import mix_narration
    voiced = OUT_DIR / "demo_voiced.mp4"
    mix_narration(str(base), str(narration), str(voiced), duck=True, duck_db=-9.0)
    print(f"[3/4] 旁白混音 -> {voiced.name}")

    # 4) 逐字字幕
    from packages.subtitle_engine import burn_captions
    word_json = OUT_DIR / "demo_words.json"
    build_word_json(lines, narr_dur, word_json)
    final = OUT_DIR / "demo_real.mp4"
    burn_captions(str(voiced), str(word_json), style="karaoke", output_path=str(final))
    print(f"[4/4] 逐字字幕 -> {final.name}")

    print("\n" + "=" * 50)
    print(f"演示成片: {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
