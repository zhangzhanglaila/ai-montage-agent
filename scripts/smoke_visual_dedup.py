"""冒烟：画面级去重 —— 素材片自带的「片头回顾/闪回」不能变成成片里的重复镜头

背景（真实事故）：
    用户反馈"一个片段用了 4 次"。查到最后剩下的根因不是切割、也不是区间选取，
    而是**素材片自己**重复了同一段画面：实测 output/test_montage8.mp4 里
    源 9.25-10.38s == 38.88-40.00s（逐像素 MSE 0.0），源 1.0s ≈ 3.0s ≈ 22.6s ≈ 31.4s。
    选中这些镜头时，它们的**源区间毫无重叠**，`select_highlight_shots` 的区间空隙
    约束全部放行，但成片里就是同一段画面出现两次。只能比对**画面内容**。
    修复前的成片（output/avfix2_sfx.mp4）实测有 1 处重复画面；修复后 0 处。

自包含：直接造一段「6 个 1s 块、其中第 6 块与第 1 块图案完全相同」的源，
模拟素材片的回顾片段。不依赖任何外部素材。

可证伪断言：
    1. 检出 >=6 个镜头，且它们的**源区间互不重叠**（说明区间约束这次是放行的，
       即"重复"不可能靠区间去重发现）
    2. 源上第 1 块与第 6 块的**画面内容确实相同** —— 用与指纹无关的
       10fps/96x54 彩色滑窗独立复核，MSE 接近 0
    3. `drop_visual_duplicates` 丢掉的正是低分的那一个（保留高分代表）
    4. 丢掉后，任意两镜头的指纹 MSE >= 阈值（性质断言）
    5. 【反证】阈值设 0（关闭）时不丢任何镜头
    6. 【反证】两个明显不同的镜头，指纹 MSE 远大于阈值（阈值不是"什么都判重复"）
    7. `select_highlight_shots` 的返回值里不存在画面重复的镜头

用法：
    MSYS2_ARG_CONV_EXCL="*" python scripts/smoke_visual_dedup.py
产出：
    output/smoke/visual_dedup/ 下的 repeat_src.mp4、shots/
"""

import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke" / "visual_dedup"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FPS = 25
BLOCK = 1.0
PW, PH = 96, 54
FP_FPS = 10          # 独立复核用的指纹帧率
# 6 个块，第 6 块(下标 5)复用第 1 块(下标 0)的图案 —— 模拟源里的回顾片段
PATTERN = [0, 1, 2, 3, 4, 0]

FAILED = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def block_palette(n):
    lv = [0, 51, 102, 153, 204, 255]
    out = []
    for i in range(n):
        k = (i * 37) % 216
        out.append((lv[k // 36], lv[(k // 6) % 6], lv[k % 6]))
    return out


def synth_source(path: Path) -> None:
    """每秒一换、带棋盘纹理（纯色块 scene 分数 <0.2，检测不到切换）"""
    pal = block_palette(len(PATTERN))
    per = int(BLOCK * FPS)
    n_frames = per * len(PATTERN)
    yy, xx = np.mgrid[0:PH, 0:PW]
    rgb = np.zeros((n_frames, PH, PW, 3), dtype=np.uint8)
    for i, pat in enumerate(PATTERN):
        base = np.array(pal[pat], dtype=np.uint8)
        phase = (pat * 7) % 16          # 相位只由图案决定 → 相同图案给相同画面
        cb = (((xx + phase) // 8) + (yy // 8)) % 2
        rgb[i * per:(i + 1) * per] = np.where(cb[..., None] == 1, base, 255 - base)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{PW}x{PH}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-g", "25", "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", str(path)],
        input=rgb.tobytes(), capture_output=True, check=True)


def hires_best_window(path_a: str, path_b: str):
    """与产品指纹**无关**的独立实现：10fps / 96x54 RGB，找最像的 0.5s 窗口对"""
    def frames(p):
        cmd = ["ffmpeg", "-v", "error", "-i", str(p),
               "-vf", f"fps={FP_FPS},scale={PW}:{PH}", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-"]
        out = subprocess.run(cmd, capture_output=True, check=True).stdout
        a = np.frombuffer(out, dtype=np.uint8).astype(np.float32)
        px = PW * PH * 3
        n = a.size // px
        return a[:n * px].reshape(n, px)

    a, b = frames(path_a), frames(path_b)
    win = 5
    if a.shape[0] < win or b.shape[0] < win:
        return float("inf")
    Wa = np.lib.stride_tricks.sliding_window_view(a, win, axis=0)
    Wb = np.lib.stride_tricks.sliding_window_view(b, win, axis=0)
    best = float("inf")
    for i in range(Wa.shape[0]):
        best = min(best, float(((Wb - Wa[i]) ** 2).mean(axis=(1, 2)).min()))
    return best


def main() -> int:
    print("=" * 60)
    print("冒烟：画面级去重（素材片自带回顾/闪回）")
    print("=" * 60)

    from pipeline import ShotDetector, MontagePipeline
    from packages.core_types.models import Shot

    THRESH = 200.0     # 16x16 灰度尺度下的默认阈值

    print(f"\n[1/4] 合成源视频（{len(PATTERN)} 个 1s 块，第 6 块图案 == 第 1 块）...")
    src = OUT_DIR / "repeat_src.mp4"
    synth_source(src)
    detector = ShotDetector(str(OUT_DIR / "shots"))
    shots = detector.detect(str(src), threshold=0.2)
    print(f"  检出 {len(shots)} 个镜头")
    check("1a. 检出 >=6 个镜头", len(shots) >= 6, f"{len(shots)} 个")

    live = [s for s in shots if s.file_path and Path(s.file_path).exists()]
    overlaps = [(a, b) for i, a in enumerate(live) for b in live[i + 1:]
                if MontagePipeline._interval_gap(a, b) < 0]
    check("1b. 镜头源区间互不重叠（区间去重这次放行全部）", not overlaps,
          f"{len(overlaps)} 对重叠")

    # 让第 1 块高分、第 6 块低分：去重应保留高分代表
    live[0].highlight_score = 0.90
    live[-1].highlight_score = 0.30
    for s in live:
        s.source_video = str(src)

    pair = (live[0], live[-1])
    print(f"\n[2/4] 断言 2：源上第 1 块与第 6 块画面确实相同")
    m_hi = hires_best_window(pair[0].file_path, pair[1].file_path)
    ctrl_hi = hires_best_window(live[1].file_path, live[2].file_path)
    print(f"  (重复对) 高分镜头 vs 低分镜头  高分辨率最像窗口 MSE {m_hi:9.1f}")
    print(f"  (对照)   第 2 块 vs 第 3 块    高分辨率最像窗口 MSE {ctrl_hi:9.1f}")
    check("2a. 重复对的高分辨率 MSE 接近 0（独立复核，不是指纹自证）",
          m_hi < 60.0, f"{m_hi:.1f}")
    check("2b. 对照组的 MSE 远大于重复对", ctrl_hi > 20 * max(m_hi, 1.0),
          f"对照 {ctrl_hi:.1f} vs 重复 {m_hi:.1f}")

    print("\n[3/4] 断言 3/4/5/6：drop_visual_duplicates 的行为")
    mp = MontagePipeline(cache_dir=str(OUT_DIR / "cache"), output_dir=str(OUT_DIR))
    kept = mp.drop_visual_duplicates(list(live), threshold=THRESH, verbose=False)
    kept_ids = {s.shot_id for s in kept}
    dropped = [s for s in live if s.shot_id not in kept_ids]
    check("3a. 恰好丢掉 1 个镜头", len(dropped) == 1, f"丢掉 {len(dropped)} 个")
    check("3b. 丢掉的是低分那一个", bool(dropped) and dropped[0].shot_id == pair[1].shot_id,
          f"丢掉 id{dropped[0].shot_id if dropped else '?'}"
          f"（低分 id{pair[1].shot_id}，高分 id{pair[0].shot_id}）")

    sig = MontagePipeline._visual_signature
    mse = MontagePipeline._visual_shared_window_mse
    ksig = [sig(s.file_path) for s in kept]
    worst = min(((mse(ksig[i], ksig[j]), kept[i], kept[j])
                 for i in range(len(kept)) for j in range(i + 1, len(kept))),
                default=(float("inf"), None, None), key=lambda x: x[0])
    check("4. 去重后任意两镜头的指纹 MSE >= 阈值", worst[0] >= THRESH,
          f"最小 {worst[0]:.1f} (id{worst[1].shot_id} vs id{worst[2].shot_id})")

    kept_off = mp.drop_visual_duplicates(list(live), threshold=0, verbose=False)
    check("5. 【反证】阈值 0（关闭）时不丢镜头", len(kept_off) == len(live),
          f"{len(kept_off)}/{len(live)}")

    m_diff = mse(sig(live[1].file_path), sig(live[2].file_path))
    check("6. 【反证】不同镜头的指纹 MSE 远大于阈值", m_diff > THRESH,
          f"第 2 块 vs 第 3 块 MSE {m_diff:.1f} vs 阈值 {THRESH}")

    print("\n[4/4] 断言 7：select_highlight_shots 的返回值里没有画面重复")
    picked = mp.select_highlight_shots(
        list(live), max_per_video=10, min_shot_gap=0.0, max_total=10,
        visual_dedup_threshold=THRESH, verbose=False)
    psig = [sig(s.file_path) for s in picked]
    bad = [(picked[i], picked[j]) for i in range(len(psig))
           for j in range(i + 1, len(psig)) if mse(psig[i], psig[j]) < THRESH]
    check("7a. 返回 >=5 个镜头（去重要补位，不能只减不补）", len(picked) >= 5,
          f"{len(picked)} 个")
    check("7b. 返回的镜头两两画面不重复", not bad,
          f"{len(bad)} 对重复" if bad else f"{len(picked)} 个镜头全部互不相同")

    print("\n" + "=" * 60)
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
