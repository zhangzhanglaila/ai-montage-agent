"""冒烟：镜头选择不得产生"重复镜头"（同源入选镜头的源区间互不相接）

背景（bug）：
    原 `pipeline.run()` 里的选镜头是
        min_gap = 20;  if abs(shot.shot_id - last_id) >= min_gap: 选中
    两个毛病导致成片里出现重复镜头：
      1) 只跟「上一个入选者」比，不跟**所有已选镜头**比。循环按分数降序走，于是
         shot2[1.00-2.00] 与 shot4[2.13-3.00] 这种源上紧挨着的镜头，只要各自离
         "上一个"够远就都能入选 —— 实测 15 个镜头里 9 对源起点差 <2s，
         id71/75/77 三个全挤在 32.87~36.37 这 3.5s 窗口里。
      2) 比的是「起点差」而不是「区间空隙」：id61[26.40-28.43] 与
         id62[28.43-29.63] 起点差 2.03s 看着够远，实则两段**严丝合缝相接**。
    现已抽出 `MontagePipeline.select_highlight_shots()`，对**所有**已选镜头做
    「源区间空隙 >= min_sep」的全对约束（min_sep 随源跨度自适应）。

可证伪断言：
    A. 复刻真实失败案例：新算法不允许"源上紧挨"的两段同时入选；
       而**旧算法会允许**（反证：这条断言就是"本修复生效"的判别依据）。
    B. 全对约束：任两个入选镜头的源区间空隙都 >= min_sep（不只跟上一个比）。
    C. min_shot_gap=0 能关掉约束（回到旧行为，允许相接）——证明开关有效。
    D. 每个源各自受 max_per_video 限制。
    E. 源跨度大时间隔自适应放大（300s 源 -> 间隔 >= 300/(15*2.5) = 8s）。
    F. 确定性：同样输入两次结果一致。
    G. 真实链路：用 ffmpeg 合成多切点视频，跑真 `ShotDetector` 拿到**连续相接**的
       镜头，经新算法后入选镜头两两不相接；产出 selection.json。

用法：
    MSYS2_ARG_CONV_EXCL="*" python scripts/smoke_shot_dedup.py
产出：
    output/smoke/shot_dedup/selection.json + cuts.mp4
"""

import json
import subprocess
import sys
from pathlib import Path
from types import MethodType

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke" / "shot_dedup"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FAILED = []
EPS = 1e-6


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def interval_gap(a, b):
    """两镜头在源时间轴上的空隙（重叠为负）"""
    return max(a.start_time, b.start_time) - min(a.end_time, b.end_time)


def min_pair_gap(shots, intra_only=True):
    """所有(SameSource 的)镜头对里最小的源区间空隙"""
    gs = [interval_gap(a, b)
          for i, a in enumerate(shots) for b in shots[i + 1:]
          if not intra_only or a.source_video == b.source_video]
    return min(gs) if gs else None


def legacy_select(shots, max_per_video=15, min_gap=20):
    """**旧算法**（原样复刻，仅用于反证）：只跟上一个入选者比 shot_id 差"""
    by_src = {}
    for s in shots:
        by_src.setdefault(s.source_video, []).append(s)
    out = []
    for _, group in by_src.items():
        selected, last_id = [], -999
        for s in sorted(group, key=lambda x: -x.highlight_score):
            if len(selected) >= max_per_video:
                break
            if abs(s.shot_id - last_id) >= min_gap:
                selected.append(s)
                last_id = s.shot_id
        out += selected
    return out


def make_shot(shot_id, start, end, score, src):
    from packages.core_types.models import Shot
    return Shot(shot_id=shot_id, start_time=start, end_time=end,
                duration=end - start, file_path=f"shot_{shot_id:05d}.mp4",
                source_video=src, highlight_score=score)


def synth_multi_cut_video(path: Path, n_cuts: int = 24, seg: float = 0.6) -> None:
    """合成一段"每 seg 秒硬切一次"的视频，供真 ShotDetector 使用"""
    palette = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00, 0xFF00FF, 0x00FFFF,
               0xFF8000, 0x8000FF, 0x008000, 0x000080, 0x800000, 0x808000]
    inputs = []
    for i in range(n_cuts):
        inputs += ["-f", "lavfi", "-i",
                   f"color=c=0x{palette[i % len(palette)]:06X}:s=320x180:d={seg}"]
    fc = "".join(f"[{i}:v]" for i in range(n_cuts)) + \
         f"concat=n={n_cuts}:v=1:a=0[v]"
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", "[v]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, capture_output=True, check=True)


def main() -> int:
    print("=" * 60)
    print("冒烟：镜头选择不得产生「重复镜头」")
    print("=" * 60)

    from pipeline import MontagePipeline

    pipeline = MontagePipeline.__new__(MontagePipeline)  # 只测静态逻辑，不建实例
    select = MethodType(MontagePipeline.__dict__["select_highlight_shots"], pipeline)

    # ---------------- Part A：复刻真实失败案例 ----------------
    print("\n[A] 复刻真实失败案例（shot2 与 shot4 源上紧挨）")
    pool = [
        make_shot(2,   1.00,  2.00, 0.50, "a.mp4"),
        make_shot(40,  9.27, 10.63, 0.49, "a.mp4"),
        make_shot(4,   2.13,  3.00, 0.45, "a.mp4"),
        make_shot(61, 26.40, 28.43, 0.48, "a.mp4"),
        make_shot(62, 28.43, 29.63, 0.48, "a.mp4"),
        make_shot(7,  40.00, 41.00, 0.30, "a.mp4"),
    ]
    span = max(s.end_time for s in pool) - min(s.start_time for s in pool)  # 40.0
    expect_sep = max(1.0, span / (15 * 2.5))                                # ~1.07

    new_sel = select(pool, min_shot_gap=1.0, verbose=False)
    ids_new = sorted(s.shot_id for s in new_sel)
    print(f"  新算法入选: {ids_new}")
    check("A1 新算法不允许源上紧挨的 2/4 同时入选", not ({2, 4} <= set(ids_new)),
          f"ids={ids_new}")
    check("A1 新算法不允许严丝合缝相接的 61/62 同时入选", not ({61, 62} <= set(ids_new)),
          f"ids={ids_new}")

    legacy_sel = legacy_select(pool)
    ids_legacy = sorted(s.shot_id for s in legacy_sel)
    legacy_gap = min_pair_gap(legacy_sel)
    print(f"  旧算法入选: {ids_legacy}   最小源区间空隙 {legacy_gap:.3f}s")
    check("A2 反证：旧算法**确实**会同时选中 2 与 4（源上仅隔 0.13s）",
          {2, 4} <= set(ids_legacy), f"legacy ids={ids_legacy}")
    check("A2 反证：旧算法违反 min_sep 约束（存在源区间近邻）",
          legacy_gap is not None and legacy_gap < expect_sep - EPS,
          f"legacy min_gap={legacy_gap:.3f}s < min_sep={expect_sep:.3f}s")

    # ---------------- Part B：全对约束 ----------------
    print("\n[B] 全对约束（不只跟上一个比）")
    g = min_pair_gap(new_sel)
    check("B1 任两个入选镜头的源区间空隙都 >= min_sep", g >= expect_sep - EPS,
          f"实测最小 {g:.3f}s >= 期望 {expect_sep:.3f}s")
    check("B2 入选数未塌缩（>=2 个）", len(new_sel) >= 2, f"{len(new_sel)} 个")

    # ---------------- Part C：开关 ----------------
    print("\n[C] min_shot_gap=0 应关闭约束（回到旧行为）")
    off_sel = select(pool, min_shot_gap=0.0, verbose=False)
    ids_off = sorted(s.shot_id for s in off_sel)
    check("C1 关闭约束后允许相接（最小空隙 <= 0）",
          min_pair_gap(off_sel) <= 0.0, f"min_gap={min_pair_gap(off_sel):.3f}s ids={ids_off}")
    check("C2 关闭约束后入选数 >= 开启时", len(off_sel) >= len(new_sel),
          f"{len(off_sel)} >= {len(new_sel)}")

    # ---------------- Part D：每源上限 ----------------
    print("\n[D] 每个源各自受 max_per_video 限制")
    multi = []
    for src in ("a.mp4", "b.mp4", "c.mp4"):
        for i in range(40):
            multi.append(make_shot(i, i * 1.0, i * 1.0 + 0.8,
                                   0.5 + (i % 7) * 0.05, src))
    sel_multi = select(multi, max_per_video=15, min_shot_gap=1.0, verbose=False)
    per_src = {}
    for s in sel_multi:
        per_src[s.source_video] = per_src.get(s.source_video, 0) + 1
    check("D1 每个源入选数 <= max_per_video", all(v <= 15 for v in per_src.values()),
          f"{per_src}")
    check("D2 三个源都被用上（交替排列）", len(per_src) == 3, f"{sorted(per_src)}")
    check("D3 同源内不相接", min_pair_gap(sel_multi) >= 1.0 - EPS,
          f"min_gap={min_pair_gap(sel_multi):.3f}s")

    # ---------------- Part E：长源自适应 ----------------
    print("\n[E] 长源时间隔应自适应放大")
    long_pool = [make_shot(i, i * 0.5, i * 0.5 + 0.4, 0.5 + (i % 11) * 0.03, "long.mp4")
                 for i in range(600)]  # 跨度 300s
    sel_long = select(long_pool, min_shot_gap=1.0, verbose=False)
    exp_long = 300.0 / (15 * 2.5)
    check(f"E1 300s 源的最小间隔 >= {exp_long:.1f}s（非固定 1.0s）",
          min_pair_gap(sel_long) >= exp_long - EPS,
          f"实测 {min_pair_gap(sel_long):.2f}s")
    check("E2 长源仍能选满 max_per_video", len(sel_long) >= 14, f"{len(sel_long)} 个")

    # ---------------- Part F：确定性 ----------------
    print("\n[F] 确定性")
    a_ids = [s.shot_id for s in select(multi, min_shot_gap=1.0, verbose=False)]
    b_ids = [s.shot_id for s in select(multi, min_shot_gap=1.0, verbose=False)]
    check("F1 同样输入两次结果一致", a_ids == b_ids)

    # ---------------- Part G：真实链路 ----------------
    print("\n[G] 真实链路：ffmpeg 合成多切点视频 -> 真 ShotDetector -> 新算法")
    video = OUT_DIR / "cuts.mp4"
    synth_multi_cut_video(video, n_cuts=24, seg=0.6)
    from pipeline import ShotDetector
    shots = ShotDetector(str(OUT_DIR / "shots")).detect(str(video), threshold=0.2)
    check("G1 真检测器检出 >=8 个镜头", len(shots) >= 8, f"{len(shots)} 个")
    adjacent = sum(1 for i in range(len(shots) - 1)
                   if abs(shots[i].end_time - shots[i + 1].start_time) < 0.05)
    check("G2 检测出的镜头确实是首尾相接的（构成本 bug 的土壤）",
          adjacent >= len(shots) - 2, f"{adjacent}/{len(shots) - 1} 对相接")

    for s in shots:
        s.highlight_score = round(0.4 + (s.shot_id * 37 % 13) / 13 * 0.6, 4)
    real_sel = select(shots, min_shot_gap=1.0, verbose=False)
    ids_real = sorted(s.shot_id for s in real_sel)
    real_gap = min_pair_gap(real_sel)
    print(f"  真链路入选 {len(real_sel)} 个: {ids_real}")
    check("G3 真链路上入选镜头两两不相接（空隙 >0）", real_gap > 0.0,
          f"min_gap={real_gap:.2f}s")
    check("G4 入选镜头都来自检出集合",
          set(ids_real) <= {s.shot_id for s in shots})
    legacy_real = legacy_select(shots)
    legacy_real_gap = min_pair_gap(legacy_real)
    note = ("仅选出 <2 个，凑不出成片" if legacy_real_gap is None
            else f"最小空隙 {legacy_real_gap:.3f}s")
    print(f"  旧算法在同素材上选出 {len(legacy_real)} 个（{note}）")
    check("G5 反证：同素材上旧算法不合格（要么选不出、要么违反 min_sep）",
          legacy_real_gap is None or legacy_real_gap < 1.0 - EPS,
          f"旧算法 {len(legacy_real)} 个 / {note}")
    check("G6 新算法在镜头数上不差于旧算法",
          len(real_sel) > len(legacy_real),
          f"新 {len(real_sel)} vs 旧 {len(legacy_real)}")

    artifact = OUT_DIR / "selection.json"
    artifact.write_text(json.dumps({
        "detected": [{"id": s.shot_id, "start": s.start_time, "end": s.end_time}
                     for s in shots],
        "selected": [{"id": s.shot_id, "start": s.start_time, "end": s.end_time,
                      "score": s.highlight_score} for s in real_sel],
        "legacy_selected_ids": [s.shot_id for s in legacy_real],
        "new_min_gap": round(real_gap, 4),
        "legacy_min_gap": (round(legacy_real_gap, 4)
                           if legacy_real_gap is not None else None),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    check("G7 产出 selection.json", artifact.exists() and artifact.stat().st_size > 0,
          str(artifact))

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
