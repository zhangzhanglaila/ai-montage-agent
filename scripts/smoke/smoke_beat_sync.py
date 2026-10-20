"""卡点/无黑场冒烟测试（自包含，不需要外部素材）。

验证四件事：
    1. BGM 前奏被裁掉，时间线从 0s 开始（开头不补黑场）。
    2. 成片总长 == 时间线标称总长（转场不再吞时长）。
    3. 每个切点都落在（平移后的）BGM 节拍上。
    4. 成片中没有黑场；音效 cue 全部落在成片时长内。

用法：
    python scripts/smoke_beat_sync.py
产出：
    output/smoke/beat_sync/ 下的 src.mp4 / bgm.wav / out.mp4
"""

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "output" / "smoke" / "beat_sync"
OUT.mkdir(parents=True, exist_ok=True)

FPS = 25
W, H = 640, 360
BLOCK = 0.6
N_BLOCKS = 8
SR = 22050

FAILED = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def make_source(path: Path) -> None:
    n_frames = int(N_BLOCKS * BLOCK * FPS)
    per = int(BLOCK * FPS)
    yy, xx = np.mgrid[0:H, 0:W]
    colors = [(255, 40, 40), (40, 200, 40), (40, 80, 255), (240, 200, 40),
              (220, 40, 220), (40, 220, 220), (240, 120, 40), (120, 40, 240)]
    rgb = np.zeros((n_frames, H, W, 3), dtype=np.uint8)
    for i in range(N_BLOCKS):
        base = np.array(colors[i], dtype=np.uint8)
        # 细网格提供纹理（场景检测需要），但只占少量面积，主色仍然可辨；
        # 这样帧差能清楚看到切点，不会被"基色/反色各半"平均成灰色。
        grid = (((xx + i * 3) % 16) < 2) | (((yy + i * 3) % 16) < 2)
        rgb[i * per:(i + 1) * per] = np.where(grid[..., None], 255 - base, base)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(path)],
        input=rgb.tobytes(), capture_output=True, check=True,
    )


def make_bgm(path: Path) -> None:
    total = 7.0
    t = np.arange(int(total * SR)) / SR
    y = np.zeros_like(t)
    # 1.0s 前奏静音 + 之后每 0.5s 一个 click
    for beat in np.arange(1.0, total - 0.2, 0.5):
        idx = int(beat * SR)
        n = int(0.03 * SR)
        tt = np.arange(n) / SR
        y[idx:idx + n] += 0.9 * np.sin(2 * np.pi * 1000 * tt) * np.exp(-tt * 120)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True).stdout.strip()
    return float(out)


def min_frame_brightness(path: str) -> float:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "fps=10,scale=64:36",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    n = a.size // (64 * 36)
    if n == 0:
        return 0.0
    return float(a[:n * 64 * 36].reshape(n, 36, 64).mean(axis=(1, 2)).min())


def cut_diff_near(path: str, times, fps: int = 30) -> list:
    """返回每个标称切点附近的最大帧差（证明画面确实在该时刻切换）。

    用 RGB 而不是灰度：有些相邻镜头亮度接近（灰度差很小），但颜色不同。
    """
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale=64:36",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    n = a.size // (64 * 36 * 3)
    frames = a[:n * 64 * 36 * 3].reshape(n, 36, 64, 3)
    d = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2, 3))
    t = (np.arange(len(d)) + 1) / fps
    out = []
    for b in times:
        m = (t >= b - 0.04) & (t <= b + 0.12)
        out.append(float(d[m].max()) if m.any() else 0.0)
    return out


def main() -> int:
    print("=" * 60)
    print("冒烟：卡点 / 无黑场 / 转场不吞时长 / 音效在成片内")
    print("=" * 60)

    src = OUT / "src.mp4"
    bgm = OUT / "bgm.wav"
    make_source(src)
    make_bgm(bgm)

    from pipeline import MontagePipeline, BeatAnalyzer
    from packages.sound_engine import SfxEngine

    p = MontagePipeline(cache_dir=str(OUT / "cache"), output_dir=str(OUT))
    out = p.run(
        [str(src)], str(bgm), style="dynamic", output_name="out.mp4",
        threshold=0.2, enable_sfx=True,
        min_shot_gap=0.0, visual_dedup_threshold=0.0,
    )

    tl = p._last_timeline
    total = sum(float(e.duration) for e in tl)
    rendered = probe_duration(out)
    print(f"\n  时间线条目: {len(tl)}  标称总长: {total:.3f}s  成片: {rendered:.3f}s")

    check("BGM 前奏已裁掉（首拍对齐 0s）", p._last_bgm_offset > 0.5,
          f"offset={p._last_bgm_offset:.2f}s")
    check("时间线从 0s 开始", abs(float(tl[0].start_time)) < 0.01,
          f"start={tl[0].start_time:.3f}")
    check("成片总长 == 时间线标称总长（误差 ≤0.15s）",
          abs(rendered - total) <= 0.15, f"{rendered:.3f} vs {total:.3f}")

    # 用同一套 BeatAnalyzer 重新检测，再平移，检查每个切点
    ba = BeatAnalyzer().analyze(str(bgm))
    offset = min(float(b.time) for b in ba.beats)
    beat_times = [float(b.time) - offset for b in ba.beats]
    worst = 0.0
    for e in tl:
        nearest = min(abs(float(e.start_time) - b) for b in beat_times)
        worst = max(worst, nearest)
    check("每个切点都落在节拍上（误差 ≤0.03s）", worst <= 0.03,
          f"最大偏差 {worst * 1000:.1f}ms")

    brightness = min_frame_brightness(out)
    check("成片无黑场（最暗帧平均亮度 >12）", brightness > 12,
          f"min={brightness:.1f}")

    diffs = cut_diff_near(out, [float(e.start_time) for e in tl[1:]])
    check("每个标称切点附近确实发生了画面切换",
          bool(diffs) and min(diffs) > 8.0,
          f"min frame-diff={min(diffs):.1f}" if diffs else "no cuts")

    cues = SfxEngine().plan(
        [type("B", (), {"time": t, "strength": 0.8, "beat_type": "strong"})()
         for t in beat_times],
        style="dynamic", duration=rendered,
        cut_times=[float(e.start_time) for e in tl[1:]],
    )
    check("音效 cue 全部落在成片时长内",
          bool(cues) and all(0.0 <= c.time < rendered for c in cues),
          f"{len(cues)} cues")

    print("\n" + "=" * 60)
    if FAILED:
        print(f"冒烟失败 {len(FAILED)} 项：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print(f"冒烟通过 - 成片: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
