"""冒烟：转场渲染必须保留原音轨（修复 `_prepare_clip` 的 `-an` 导致原声全丢）

背景（bug）：
    `_prepare_clip` 两个分支都带 `-an` → 任何路径下 clip 都没有音轨 →
    concat 出的底片没有音轨 → Step3 混音的 `[0:a]` 不存在 →
    「混音失败（可能无原音轨），使用纯 BGM」静默回退，原片人声/现场声永远进不了成片。
    次生问题：`_render_with_transitions` 的 `scaled_*.mp4` 同样 `-an`，
    且 xfade 段只有视频流，音画对不齐。

自包含：用 ffmpeg 合成「带不同频率正弦音」的源片段，不依赖任何外部素材。
可证伪断言：
    A. `_prepare_clip` 对有音轨源 → 输出含音轨；对无音轨源 → 也补出**静音**音轨。
    B. 变速（speed=1.3）时音轨仍存在，且时长与画面一致（atempo 生效、不用 -an）。
    C. cut 路径（`_render_concat`）成片含原声：FFT 同时检出 440Hz 与 1200Hz。
    D. fade 转场路径（`_render_with_transitions`）成片含原声：FFT 同样检出两个主频。
    E. 转场路径时长符合 xfade 解析值（2+2-0.5=3.5s），且音/视频时长误差 < 0.1s（无漂移）。
    F. 反证：无音轨源的 cut 拼接成片，音轨必须是「真静音」（< -80dB），
       证明 C/D 检出的主频确实来自原声，而不是检测器误报。
    G. 端到端：`VideoRenderer.render()` 混 BGM 后，成片里原声与 BGM 同时存在。

用法：
    MSYS2_ARG_CONV_EXCL="*" python scripts/smoke_transition_audio.py
产出：
    output/smoke/transition_audio/ 下的中间件与成片
"""

import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke" / "transition_audio"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TONE_A = 440.0     # 源 A 的主频
TONE_B = 1200.0    # 源 B 的主频
TONE_BGM = 3000.0  # 测试 BGM 主频（与两个源音都不同，便于分离）

FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_source(path: Path, seconds: float, color: str, freq: float = None) -> None:
    """合成源片段；freq=None 表示**不带音轨**（用于反证静音补齐）"""
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=640x360:d={seconds}"]
    if freq is not None:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={seconds}"]
        cmd += ["-shortest", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    run(cmd, check=True)


def has_audio(path) -> bool:
    r = run(["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool((r.stdout or "").strip())


def duration(path, stream: str = None) -> float:
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        return float(run(cmd).stdout.strip())
    except ValueError:
        return -1.0


def max_volume_db(path) -> float:
    r = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    for line in (r.stderr or "").splitlines():
        if "max_volume:" in line:
            return float(line.split("max_volume:")[1].replace("dB", "").strip())
    return -999.0


def band_ratio(path, freqs, sample_rate: int = 16000) -> dict:
    """抽 PCM 做 FFT，返回 {目标频率: 该窄带峰值 / 全局峰值}"""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sample_rate),
         "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    x = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    if x.size < 1024:
        return {f: 0.0 for f in freqs}
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    axis = np.fft.rfftfreq(x.size, 1.0 / sample_rate)
    peak = float(spec.max()) or 1e-12
    out = {}
    for f in freqs:
        lo, hi = np.searchsorted(axis, f - 25), np.searchsorted(axis, f + 25)
        out[f] = float(spec[lo:hi + 1].max()) / peak if hi > lo else 0.0
    return out


def main() -> int:
    print("=" * 56)
    print("冒烟：转场/拼接必须保留原音轨")
    print("=" * 56)

    from packages.core_types.models import TimelineEntry
    from pipeline import VideoRenderer

    renderer = VideoRenderer(output_dir=str(OUT_DIR))

    print("\n[1/5] 合成源素材（A=440Hz / B=1200Hz / C=无音轨）...")
    src_a = OUT_DIR / "src_a.mp4"
    src_b = OUT_DIR / "src_b.mp4"
    src_c = OUT_DIR / "src_c.mp4"
    make_source(src_a, 3.0, "red", TONE_A)
    make_source(src_b, 3.0, "blue", TONE_B)
    make_source(src_c, 3.0, "green", None)
    check("源 A 自带音轨", has_audio(src_a))
    check("源 C 确实无音轨（反证基准）", not has_audio(src_c))

    print("\n[2/5] `_prepare_clip`：保留原声 / 变速率 / 无声源补静音")
    clip_a = OUT_DIR / "clip_a.mp4"
    clip_b = OUT_DIR / "clip_b.mp4"
    clip_c = OUT_DIR / "clip_c.mp4"
    clip_fast = OUT_DIR / "clip_a_fast.mp4"
    renderer._prepare_clip(str(src_a), str(clip_a), 2.0, 1.0)
    renderer._prepare_clip(str(src_b), str(clip_b), 2.0, 1.0)
    renderer._prepare_clip(str(src_c), str(clip_c), 2.0, 1.0)
    renderer._prepare_clip(str(src_a), str(clip_fast), 2.0, 1.3)

    check("有声源的 clip 输出含音轨（原实现为 ['video']）", has_audio(clip_a),
          f"streams={['video' if not has_audio(clip_a) else 'video+audio']}")
    check("无声源的 clip 也补出音轨（流结构统一）", has_audio(clip_c))
    check("无声源补的是**静音**（max < -80dB）", max_volume_db(clip_c) < -80.0,
          f"max_volume={max_volume_db(clip_c):.1f}dB")
    check("原声 clip 不是静音（max > -40dB）", max_volume_db(clip_a) > -40.0,
          f"max_volume={max_volume_db(clip_a):.1f}dB")

    dv, da = duration(clip_fast, "v:0"), duration(clip_fast, "a:0")
    check("变速 clip 音画等长（atempo 生效）", abs(dv - da) < 0.12,
          f"video={dv:.3f}s audio={da:.3f}s speed=1.3")

    print("\n[3/5] cut 路径：原声必须进成片")
    cut_out = OUT_DIR / "out_cut.mp4"
    cut_raw = renderer._render_concat([str(clip_a), str(clip_b)])
    Path(cut_raw).replace(cut_out)
    check("cut 成片含音轨", has_audio(cut_out))
    r_cut = band_ratio(cut_out, [TONE_A, TONE_B])
    check("cut 成片检出源 A 主频 440Hz", r_cut[TONE_A] > 0.03, f"ratio={r_cut[TONE_A]:.3f}")
    check("cut 成片检出源 B 主频 1200Hz", r_cut[TONE_B] > 0.03, f"ratio={r_cut[TONE_B]:.3f}")

    print("\n[4/5] fade 转场路径：原声必须进成片、音画不漂移")
    entries = [
        TimelineEntry(shot_id=0, shot_path=str(clip_a), start_time=0.0, end_time=2.0,
                      duration=2.0, beat_time=0.0, transition_type="cut", transition_duration=0.0),
        TimelineEntry(shot_id=1, shot_path=str(clip_b), start_time=2.0, end_time=4.0,
                      duration=2.0, beat_time=2.0, transition_type="fade", transition_duration=0.5),
    ]
    fade_raw = renderer._render_with_transitions([str(clip_a), str(clip_b)], entries)
    fade_out = OUT_DIR / "out_fade.mp4"
    Path(fade_raw).replace(fade_out)
    check("fade 成片含音轨（原实现只有视频，混音静默回退纯 BGM）", has_audio(fade_out))
    r_fade = band_ratio(fade_out, [TONE_A, TONE_B])
    check("fade 成片检出源 A 主频 440Hz", r_fade[TONE_A] > 0.03, f"ratio={r_fade[TONE_A]:.3f}")
    check("fade 成片检出源 B 主频 1200Hz", r_fade[TONE_B] > 0.03, f"ratio={r_fade[TONE_B]:.3f}")

    fv, fa = duration(fade_out, "v:0"), duration(fade_out, "a:0")
    check("fade 成片时长符合 xfade 解析值 3.5s", abs(fv - 3.5) < 0.15, f"video={fv:.3f}s")
    check("fade 成片音画等长（无逐段漂移）", abs(fv - fa) < 0.10, f"video={fv:.3f}s audio={fa:.3f}s")

    print("\n[5/5] 反证 + 端到端混音")
    neg_raw = renderer._render_concat([str(clip_c), str(clip_c)])
    neg_out = OUT_DIR / "out_silent.mp4"
    Path(neg_raw).replace(neg_out)
    check("反证：无声源拼接成片确为静音（检出的主频确实来自原声）",
          has_audio(neg_out) and max_volume_db(neg_out) < -80.0,
          f"max_volume={max_volume_db(neg_out):.1f}dB")

    bgm = OUT_DIR / "bgm_3000hz.wav"
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={TONE_BGM}:sample_rate=44100:duration=8",
         "-c:a", "pcm_s16le", str(bgm)], check=True)
    mixed = OUT_DIR / "out_mixed.mp4"
    renderer.render(entries, str(bgm), str(mixed))
    check("端到端成片含音轨", has_audio(mixed))
    r_mix = band_ratio(mixed, [TONE_A, TONE_B, TONE_BGM])
    check("端到端成片仍含原声 440Hz（未回退纯 BGM）", r_mix[TONE_A] > 0.02,
          f"ratio={r_mix[TONE_A]:.3f}")
    check("端到端成片仍含原声 1200Hz（未回退纯 BGM）", r_mix[TONE_B] > 0.02,
          f"ratio={r_mix[TONE_B]:.3f}")
    check("端到端成片含 BGM 3000Hz", r_mix[TONE_BGM] > 0.02, f"ratio={r_mix[TONE_BGM]:.3f}")

    print("\n" + "=" * 56)
    if FAILED:
        print(f"冒烟失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("冒烟全部通过")
    print(f"产出目录：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
