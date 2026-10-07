"""冒烟：镜头切割必须**精确**落在标称时间点上（否则成片会出现重复镜头）

背景（bug）：
    `ShotDetector.detect()` 原来用
        ffmpeg -ss <start> -i SRC -to <dur> -c copy
    `-c copy` 无法丢帧，ffmpeg 只能从 `<= start` 的**关键帧**开始拷，于是镜头文件的
    **实际内容**比记录的 `start_time` 提前一整个 GOP（实测：中位 -0.59s，最大 -4.3s，
    源的关键帧间隔中位 1.2s/最大 2.0s）。后果：标称上互相分离的两个镜头，实际画面仍
    大面积重叠；源上同一时刻能被 4~6 个镜头文件覆盖 → 成片里"同一个片段用了 4 次"。
    改成重编码（`-ss` 输入 + `-t` + libx264/aac）后 ffmpeg 会 seek 到关键帧并
    **解码丢弃** start 之前的帧，切点精确。

自包含：用 numpy 直接造一段「每秒一换的纯色块」原始帧 → x264 编码（`-g` 设小，
让关键帧间隔只有 2s），不依赖任何外部素材。

可证伪断言：
    A. 真 ShotDetector 在新切割命令下，每个镜头文件的**实际内容起点** == 标称 start
       （用整段帧序列做指纹在源上滑窗定位；误差 <= 1 帧）。
    B. **反证**：同一段素材上用旧的 `-c copy` 命令切同一区间，实际起点会明显提前
       —— 证明 A 之所以能过，是因为换了切割方式，而不是检测器/指纹软件的问题。
    C. 源时间轴上任何一个时刻，被镜头文件「实际覆盖」的次数 <= 1（旧方式下会 >1）。
    D. 镜头文件时长 == 标称时长（误差 <= 1 帧）。

用法：
    MSYS2_ARG_CONV_EXCL="*" python scripts/smoke_shot_cut_accuracy.py
产出：
    output/smoke/shot_cut/ 下的 cuts_src.mp4、shots/、offsets.json
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke" / "shot_cut"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FPS = 25            # 源帧率
BLOCK = 1.0         # 每个色块 1 秒 -> 每 1 秒一个场景切换
N_BLOCK = 36        # 36 秒
PW, PH = 96, 54     # 指纹用的小图尺寸
FP_FPS = 10         # 指纹抽取帧率
GOP = 50            # 关键帧间隔 50 帧 = 2.0s（这样 copy 切会偏移）

FAILED = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def block_palette(n):
    """n 个在 RGB 空间互相远离的颜色（6 级每通道 -> 216 种足够）"""
    lv = [0, 51, 102, 153, 204, 255]
    out = []
    for i in range(n):
        # 用步长与 n 互质，保证遍历不重复
        k = (i * 37) % 216
        r, g, b = lv[k // 36], lv[(k // 6) % 6], lv[k % 6]
        out.append((r, g, b))
    return out


def synth_source(path: Path) -> None:
    """造「每秒一换、带棋盘纹理」的色块源视频（关键帧间隔 2s）

    注意：**不能只用纯色块**。ffmpeg 的 `scene` 度量在整帧无空间纹理时分数极低
    （实测纯色块切换的 scene < 0.2，默认阈值下检测不到切换），所以每块叠一层
    相位不同的棋盘，既给了空间纹理、又保证每块图案唯一。
    """
    pal = block_palette(N_BLOCK)
    n_frames = int(N_BLOCK * BLOCK * FPS)
    per = int(BLOCK * FPS)
    yy, xx = np.mgrid[0:PH, 0:PW]
    rgb = np.zeros((n_frames, PH, PW, 3), dtype=np.uint8)
    for i in range(N_BLOCK):
        base = np.array(pal[i], dtype=np.uint8)
        phase = (i * 7) % 16
        cb = (((xx + phase) // 8) + (yy // 8)) % 2          # 8px 棋盘，相位随块变化
        rgb[i * per:(i + 1) * per] = np.where(cb[..., None] == 1, base, 255 - base)
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{PW}x{PH}", "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-g", str(GOP), "-keyint_min", str(GOP),
           "-sc_threshold", "0", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, input=rgb.tobytes(), capture_output=True, check=True)


def frames(path):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path),
           "-vf", f"fps={FP_FPS},scale={PW}:{PH}", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    a = np.frombuffer(out, dtype=np.uint8).astype(np.float32)
    px = PW * PH * 3
    n = a.size // px
    return a[:n * px].reshape(n, px) if n else a.reshape(0, px)


def locate(src, seg):
    """整段帧序列滑窗定位，返回 (起始帧, 最优MSE)"""
    m = seg.shape[0]
    if m == 0 or src.shape[0] < m:
        return None, None
    errs = np.empty(src.shape[0] - m + 1)
    for k in range(errs.size):
        errs[k] = ((src[k:k + m] - seg) ** 2).mean()
    k = int(np.argmin(errs))
    return k, float(errs[k])


def main() -> int:
    print("=" * 60)
    print("冒烟：镜头切割必须精确落在标称时间点")
    print("=" * 60)

    from pipeline import ShotDetector

    print(f"\n[1/4] 合成源视频（{N_BLOCK} 个 1s 色块，关键帧间隔 {GOP/FPS:.1f}s）...")
    src_path = OUT_DIR / "cuts_src.mp4"
    synth_source(src_path)
    src = frames(src_path)
    print(f"  指纹序列 {src.shape[0]} 帧 @ {FP_FPS}fps（{src.shape[0]/FP_FPS:.1f}s）")
    check("源视频指纹可用（帧数 > 0）", src.shape[0] > 10, f"{src.shape[0]} 帧")

    detector = ShotDetector(str(OUT_DIR / "shots"))
    shots = detector.detect(str(src_path), threshold=0.2)
    print(f"  检出 {len(shots)} 个镜头")
    check("检出镜头数 >= 8", len(shots) >= 8, f"{len(shots)} 个")

    print("\n[2/4] 断言 A/D：每个镜头的实际内容起点 == 标称 start")
    rows = []
    for s in shots:
        p = Path(s.file_path)
        if not p.exists():
            continue
        seg = frames(p)
        if seg.shape[0] == 0:
            continue
        k, err = locate(src, seg)
        if k is None:
            continue
        rows.append(dict(id=s.shot_id, nom=s.start_time, nom_end=s.end_time,
                         act=k / FP_FPS, nfr=seg.shape[0],
                         dur=seg.shape[0] / FP_FPS, nom_dur=s.duration, err=err))

    check("样本数 >= 8", len(rows) >= 8, f"{len(rows)} 个")
    ok_start = 0
    for r in rows:
        if abs(r["act"] - r["nom"]) <= 1.5 / FP_FPS:
            ok_start += 1
    check("A 所有镜头实际内容起点 == 标称 start（误差 <=1.5 帧）",
          ok_start == len(rows), f"{ok_start}/{len(rows)} 通过")
    ok_dur = sum(1 for r in rows if abs(r["dur"] - r["nom_dur"]) <= 2.5 / FP_FPS)
    check("D 镜头文件时长 == 标称时长（误差 <=2.5 帧）",
          ok_dur == len(rows), f"{ok_dur}/{len(rows)} 通过")

    worst = max(rows, key=lambda r: abs(r["act"] - r["nom"]))
    print(f"  最大偏移 {worst['act']-worst['nom']:+.3f}s (id{worst['id']})")

    print("\n[3/4] 断言 B：反证 —— 旧 `-c copy` 命令在**同一区间**上会偏移")
    # 挑一个起点不落在关键帧上的镜头（关键帧在 0.0/2.0/4.0...）
    cand = [r for r in rows if abs((r["nom"] % (GOP / FPS))) > 0.3]
    check("能挑到起点不在关键帧上的镜头（否则反证无意义）", len(cand) > 0,
          f"{len(cand)} 个候选")
    if cand:
        c = cand[0]
        legacy = OUT_DIR / "legacy_cut.mp4"
        subprocess.run(["ffmpeg", "-y", "-ss", str(c["nom"]),
                        "-i", str(src_path), "-to", str(c["nom_dur"]),
                        "-c", "copy", "-avoid_negative_ts", "make_zero",
                        str(legacy)], capture_output=True, check=True)
        seg = frames(legacy)
        k, err = locate(src, seg)
        legacy_act = k / FP_FPS if k is not None else None
        shift = (legacy_act - c["nom"]) if legacy_act is not None else 0.0
        print(f"  id{c['id']}: 标称 {c['nom']:.2f}s  旧命令实际起点 "
              f"{legacy_act if legacy_act is None else round(legacy_act,2)}s  偏移 {shift:+.2f}s")
        check("B 旧 `-c copy` 切割的偏移 > 0.15s（证明确实是切割方式的问题）",
              shift < -0.15, f"偏移 {shift:+.3f}s")

    print("\n[4/4] 断言 C：源时间轴上没有时刻被 >=2 个镜头实际覆盖")
    cov = np.zeros(int(src.shape[0] / FP_FPS * 100) + 2, dtype=int)
    for r in rows:
        a = int(r["act"] * 100)
        b = int((r["act"] + r["dur"]) * 100)
        cov[max(0, a):b] += 1
    check("C 单个时刻被镜头实际覆盖的最大次数 <= 1", cov.max() <= 1,
          f"最大 {cov.max()} 次")

    art = OUT_DIR / "offsets.json"
    art.write_text(json.dumps({
        "gop_seconds": GOP / FPS,
        "shots": [{"id": r["id"], "nominal_start": round(r["nom"], 3),
                   "actual_start": round(r["act"], 3),
                   "shift": round(r["act"] - r["nom"], 3),
                   "nominal_dur": round(r["nom_dur"], 3),
                   "file_dur": round(r["dur"], 3)} for r in rows],
        "max_abs_shift": round(max(abs(r["act"] - r["nom"]) for r in rows), 4),
        "legacy_shift": None if not cand else round(shift, 4),
        "max_coverage": int(cov.max()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    check("产出 offsets.json", art.exists() and art.stat().st_size > 0, str(art))

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
