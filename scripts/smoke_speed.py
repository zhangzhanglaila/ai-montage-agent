"""F2.3 冒烟测试：变速曲线（速度斜坡）

验证方式（不止"跑通"）：
    1. 曲线数学：线性斜坡的 output_duration 与解析积分一致；
    2. atempo 拆解：所有因子必须落在 [0.5, 2.0]（老版 ffmpeg 兼容区间）；
    3. 渲染后**视频流时长 ≈ 曲线预期时长**；
    4. **音画同步**：同一文件里视频流与音频流时长必须一致（漂移即失败）；
    5. **音频真的被变速**：源为「前 2s 300Hz + 后 2s 1200Hz」，以恒定 2x 变速后，
       输出前段仍应是 300Hz、后段仍是 1200Hz（atempo 变速不变调），
       若音频没被处理则会在输出里读到 1200Hz —— 用 FFT 主频判定。

用法：
    python scripts/smoke_speed.py
产出：
    output/smoke/speed_src.mp4 / speed_rush.mp4 / speed_2x.mp4
"""

import argparse
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def make_source(path: Path) -> None:
    """源：4s 画面 + 前 2s 300Hz / 后 2s 1200Hz 音频。"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=640x360:d=4:r=25",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=1200:duration=2",
        "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    r = _run(cmd)
    if r.returncode != 0 or not path.exists():
        raise RuntimeError(f"合成源视频失败: {r.stderr[-500:]}")


def stream_durations(path: Path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type,duration", "-of", "csv=p=0", str(path)])
    out = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def dominant_freq(path: Path, start: float, dur: float, sr: int = 16000) -> float:
    wav = OUT_DIR / "_seg.wav"
    _run(["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", str(path),
          "-ac", "1", "-ar", str(sr), "-f", "wav", str(wav)])
    with wave.open(str(wav), "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if len(x) < 128:
        return 0.0
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    mask = (freqs > 100) & (freqs < 2000)
    return float(freqs[mask][np.argmax(spec[mask])])


def main() -> int:
    argparse.ArgumentParser(description="F2.3 变速曲线冒烟测试").parse_args()
    print("=" * 54)
    print("F2.3 冒烟：变速曲线（速度斜坡）")
    print("=" * 54)

    from packages.montage_engine import (
        SpeedCurve, SpeedSegment, atempo_chain,
        apply_speed_curve, build_filter_complex,
    )

    print("\n[1/5] 曲线数学自检...")
    dur, steps = 4.0, 10
    ramp = SpeedCurve.ramp(dur, 1.0, 3.0, steps=steps)
    # 解析积分: ∫ d/(1+2u) du  (u 从 0..1) = (d/2)*ln(3)
    analytic = (dur / (3.0 - 1.0)) * np.log(3.0 / 1.0)
    got = ramp.output_duration()
    print(f"  ramp 1.0->3.0 : 采样估算 {got:.3f}s, 解析积分 {analytic:.3f}s")
    assert abs(got - analytic) < 0.08, f"斜坡时长偏差过大 ({got:.3f} vs {analytic:.3f})"
    assert ramp.output_duration() < ramp.input_duration(), "加速斜坡的总时长应缩短"

    slow = SpeedCurve.from_preset("slowmo", dur, steps=steps)
    assert slow.output_duration() > slow.input_duration(), "slowmo 的总时长应拉长"
    print(f"  slowmo 曲线 : 输入 {slow.input_duration():.2f}s -> 输出 {slow.output_duration():.2f}s")
    for name in ("rush", "slowmo", "hero", "punch"):
        c = SpeedCurve.from_preset(name, dur, steps=steps)
        assert len(c) > 0 and c.output_duration() > 0

    print("[2/5] atempo 因子区间自检...")
    for s in (0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 8.0, 40.0):
        fs = atempo_chain(s)
        prod = float(np.prod(fs))
        assert all(0.5 - 1e-9 <= f <= 2.0 + 1e-9 for f in fs), f"speed={s} 因子越界: {fs}"
        assert abs(prod - s) < max(1e-3, s * 1e-4), f"speed={s} 因子乘积 {prod} != {s}"
    print(f"  例: 8.0x -> {atempo_chain(8.0)}   0.1x -> {atempo_chain(0.1)}")

    print("[3/5] 合成源视频（300Hz + 1200Hz）...")
    src = OUT_DIR / "speed_src.mp4"
    make_source(src)
    d0 = stream_durations(src)
    print(f"  源: {d0}")

    print("[4/5] 应用 rush 曲线 -> 时长与音画同步...")
    rush = SpeedCurve.from_preset("rush", 4.0, steps=10)
    rush_out = OUT_DIR / "speed_rush.mp4"
    apply_speed_curve(str(src), str(rush_out), rush)
    d1 = stream_durations(rush_out)
    expect = rush.output_duration()
    print(f"  预期输出 {expect:.2f}s, 实测 {d1}")
    assert abs(d1.get("video", 0) - expect) < 0.35, "视频时长与曲线预期不符"
    if "audio" in d1:
        assert abs(d1["video"] - d1["audio"]) < 0.25, \
            f"音画不同步: video={d1['video']:.3f} audio={d1['audio']:.3f}"
        print(f"  音画同步 OK（差 {abs(d1['video'] - d1['audio']):.3f}s）")

    print("[5/5] 恒定 2x 的「变速不变调」FFT 判定...")
    two_x = SpeedCurve.constant(4.0, 2.0)
    x2_out = OUT_DIR / "speed_2x.mp4"
    apply_speed_curve(str(src), str(x2_out), two_x)
    d2 = stream_durations(x2_out)
    f_early = dominant_freq(x2_out, 0.15, 0.5)
    f_late = dominant_freq(x2_out, 1.35, 0.5)
    print(f"  2x 输出时长={d2.get('video'):.2f}s（源 4s）")
    print(f"  输出前段主频={f_early:.0f}Hz（应≈300）  后段主频={f_late:.0f}Hz（应≈1200）")
    assert abs(d2.get("video", 0) - 2.0) < 0.3, "2x 后总时长应约 2s"
    assert abs(f_early - 300) < 60, f"前段主频不对（{f_early:.0f}Hz 应≈300）"
    assert abs(f_late - 1200) < 90, f"后段主频不对（{f_late:.0f}Hz 应≈1200）"
    assert f_late > f_early * 2, "音频未被变速（两段主频关系不对）"

    print("\n" + "=" * 54)
    print("冒烟通过 ✓ （曲线数学 / atempo 区间 / 时长 / 音画同步 / 变速不变调）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
