"""F2.4 冒烟测试：多轨音频混音（人声/BGM/音效）

验证方式（用频段能量做定量判定，而非"跑通"）：
    源设计：voice=300Hz（只在 1-2s 与 4-5s 有声）、bgm=1200Hz（全程）、sfx=2600Hz（2.5s 处一响）
    1. 三轨都在：输出里 300/1200/2600Hz 三个频段都有能量；
    2. **自动闪避**：人声区间内 1200Hz（BGM）能量必须显著低于非人声区间；
    3. **独立增益**：BGM 从 0dB 调到 -20dB，其频段能量应下降 ≥13dB（功率比 ≥20x）；
    4. 时长与目标一致。

用法：
    python scripts/smoke_mixer.py
产出：
    output/smoke/mix_voice.m4a / mix_bgm.m4a / mix_sfx.m4a
    output/smoke/mix_out.m4a / mix_low_bgm.m4a
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

SR = 44100
TOTAL = 6.0


def _run(cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd[:8])}\n{r.stderr[-500:]}")
    return r


def make_voice(path: Path) -> None:
    """1s 静音 + 1s 300Hz + 2s 静音 + 1s 300Hz + 1s 静音 = 6s"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=1",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=1",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
        "-filter_complex", "[0:a][1:a][2:a][3:a][4:a]concat=n=5:v=0:a=1[a]",
        "-map", "[a]", "-c:a", "aac", str(path),
    ]
    _run(cmd, check=True)


def make_tone(path: Path, freq: float, duration: float) -> None:
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
          f"sine=frequency={freq}:duration={duration}", "-c:a", "aac", str(path)],
         check=True)


def band_energy(path: Path, start: float, dur: float, center: float,
                bw: float = 70.0, sr: int = SR) -> float:
    """取 [start, start+dur] 内 center 附近频段的能量（dB）。"""
    wav = OUT_DIR / "_mixseg.wav"
    _run(["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", str(path),
          "-ac", "1", "-ar", str(sr), "-f", "wav", str(wav)], check=True)
    with wave.open(str(wav), "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
    if len(x) < 256:
        return -120.0
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    mask = (freqs >= center - bw) & (freqs <= center + bw)
    e = float(spec[mask].sum())
    return 10 * np.log10(e + 1e-12)


def main() -> int:
    argparse.ArgumentParser(description="F2.4 多轨混音冒烟测试").parse_args()
    print("=" * 54)
    print("F2.4 冒烟：多轨音频混音（人声/BGM/音效）")
    print("=" * 54)

    from packages.audio_mixer import (
        AudioTrack, AudioMixer, detect_active_windows,
    )

    print("\n[1/5] 生成三条测试音轨...")
    voice, bgm, sfx = OUT_DIR / "mix_voice.m4a", OUT_DIR / "mix_bgm.m4a", OUT_DIR / "mix_sfx.m4a"
    make_voice(voice)
    make_tone(bgm, 1200.0, TOTAL)
    make_tone(sfx, 2600.0, 0.4)
    print(f"  {voice.name} / {bgm.name} / {sfx.name}")

    print("[2/5] 人声时段自动检测...")
    wins = detect_active_windows(str(voice), TOTAL)
    print(f"  检测到有声区间: {wins}")
    assert len(wins) >= 2, f"应检测到 2 段人声，实际 {len(wins)}"
    voice_win = wins[0]
    # 找一个明显在人声之外的时间点
    quiet_win = (2.4, 3.0) if voice_win[1] <= 2.4 else (2.6, 3.2)
    print(f"  人声窗={voice_win}  对照窗={quiet_win}")

    mixer = AudioMixer()
    tracks = [
        AudioTrack(str(bgm), role="bgm", gain_db=0.0, loop=True),
        AudioTrack(str(voice), role="voice", gain_db=0.0),
        AudioTrack(str(sfx), role="sfx", gain_db=0.0, start=2.5),
    ]
    print("[3/5] 混音（自动闪避）...")
    print(mixer.describe(tracks))
    out = OUT_DIR / "mix_out.m4a"
    mixer.mix(tracks, str(out), duration=TOTAL, auto_duck=True)

    print("[4/5] 频段定量校验...")
    e300 = band_energy(out, 1.2, 0.5, 300.0)
    e1200_quiet = band_energy(out, quiet_win[0], quiet_win[1] - quiet_win[0], 1200.0)
    e1200_duck = band_energy(out, voice_win[0] + 0.15,
                             min(0.5, voice_win[1] - voice_win[0] - 0.15), 1200.0)
    e2600 = band_energy(out, 2.55, 0.3, 2600.0)
    print(f"  300Hz(人声)={e300:.1f}dB  1200Hz(BGM)=安静段 {e1200_quiet:.1f}dB / 人声段 {e1200_duck:.1f}dB")
    print(f"  2600Hz(音效)={e2600:.1f}dB")
    assert e300 > -60, f"人声轨缺失（300Hz={e300:.1f}dB）"
    assert e1200_quiet > -60, f"BGM 轨缺失（1200Hz={e1200_quiet:.1f}dB）"
    assert e2600 > -70, f"音效轨缺失（2600Hz={e2600:.1f}dB）"
    drop = e1200_quiet - e1200_duck
    print(f"  闪避落差 = {drop:.1f} dB")
    assert drop > 6.0, f"自动闪避未生效（落差仅 {drop:.1f}dB）"

    print("[5/5] 独立增益校验 + 时长...")
    gain_tracks = [
        AudioTrack(str(bgm), role="bgm", gain_db=-20.0, loop=True),
        AudioTrack(str(voice), role="voice", gain_db=0.0),
    ]
    low = OUT_DIR / "mix_low_bgm.m4a"
    mixer.mix(gain_tracks, str(low), duration=TOTAL, auto_duck=False)
    e1200_low = band_energy(low, quiet_win[0], quiet_win[1] - quiet_win[0], 1200.0)
    e1200_ref = band_energy(out, quiet_win[0], quiet_win[1] - quiet_win[0], 1200.0)
    gain_diff = e1200_ref - e1200_low
    print(f"  BGM 0dB -> {e1200_ref:.1f}dB ; -20dB -> {e1200_low:.1f}dB ; 差 {gain_diff:.1f}dB")
    assert gain_diff > 13.0, f"独立增益未生效（差 {gain_diff:.1f}dB 应 ≥13）"

    dur = float(_run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=noprint_wrappers=1:nokey=1", str(out)]).stdout.strip())
    print(f"  输出时长 = {dur:.2f}s（目标 {TOTAL}s）")
    assert abs(dur - TOTAL) < 0.35, "输出时长与目标不符"

    print("\n" + "=" * 54)
    print("冒烟通过 ✓ （三轨齐全 / 自动闪避 / 独立增益 / 时长）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
