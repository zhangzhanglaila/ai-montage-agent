"""冒烟：渲染出的每一段必须"音画等长"，成片视频流不得被截断。

背景（真实事故）：
  卡点时长只由**歌曲**决定（拍数 × 拍间隔），从不看镜头实际有多少画面；
  而 `apad` 只补音频。于是短镜头那一段会变成"画面 0.3s + 声音 1.5s"：
    - concat demuxer 的 `-c copy` 按流各自拼接 → 音画逐段漂移；
    - xfade 的 `offset` 用 `format=duration`（= 音频时长）算 → 越界，成片视频流被截断。
  实测成片只剩 1.53s 画面 / 7.0s 声音。本脚本用可证伪的断言锁住修复。

断言（8 条）：
  1. `_prepare_clip` 对"画面比请求时长更短"的源，产出画面长度 == 请求时长（±2 帧）
  2. 该 clip 音画长度差 <= 0.05s
  3. 该 clip 仍带音轨（修复不能靠丢音轨绕过）
  4. 【负对照】直接用老滤镜链（截断 + apad，无 tpad）→ 画面 < 0.5s 而音频 ~1.5s
     （证明第 1 条是被新加的补帧逻辑修好的，而不是本来就长）
  5. `_get_duration` 对"音频比画面长"的文件返回**视频流**时长（< 1.0s），不是 1.5s
  6. `BeatSyncEngine.sync` 不会给出超过镜头可用画面的时长
  7. `BeatSyncEngine.sync` 会跳过连 1 拍都撑不满的镜头
  8. 端到端 `VideoRenderer.render` 产出的成片：画面长度 ≈ 音频长度（±0.2s）且 > 0.8s
"""

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = ROOT / "output" / "smoke" / "av_length"
FPS = 30
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def vdur(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return -1.0


def adur(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a:0",
               "-show_entries", "stream=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return -1.0


def n_frames(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-count_frames", "-show_entries", "stream=nb_read_frames",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    try:
        return int(out)
    except ValueError:
        return -1


def make_source(path, v_sec, a_sec, freq):
    """画面 v_sec 秒、声音 a_sec 秒（两者不等长，模拟"短镜头"）"""
    run(["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FPS}:duration={v_sec}",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={a_sec}",
         "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
         str(path)])


def main():
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    from pipeline import VideoRenderer, BeatSyncEngine
    from packages.core_types.models import Shot, Beat, TimelineEntry

    # 短源：画面 0.30s、声音 0.30s
    short = WORK / "short.mp4"
    long_ = WORK / "long.mp4"
    make_source(short, 0.30, 0.30, 440)
    make_source(long_, 3.00, 3.00, 1200)
    print(f"夹具：{short.name} 画面 {vdur(short):.3f}s / {long_.name} 画面 {vdur(long_):.3f}s")

    r = VideoRenderer(output_dir=str(WORK))

    # ---- 1/2/3：补帧后画面必须补到请求时长 ----
    print("\n[1-3] _prepare_clip 画面补足")
    out1 = WORK / "clip_pad.mp4"
    REQ = 1.50
    r._prepare_clip(str(short), str(out1), REQ, 1.0)
    v1, a1 = vdur(out1), adur(out1)
    check("1. 画面补足到请求时长(±2帧)", abs(v1 - REQ) <= 2 / FPS,
          f"请求 {REQ}s → 画面 {v1:.3f}s")
    check("2. clip 音画等长(<=0.05s)", abs(v1 - a1) <= 0.05,
          f"画面 {v1:.3f} / 声音 {a1:.3f}")
    check("3. clip 仍带音轨", a1 > 0, f"声音 {a1:.3f}s")

    # ---- 4：负对照，老滤镜链会截断 ----
    print("\n[4] 负对照：老滤镜链（无 tpad）")
    old = WORK / "clip_old.mp4"
    run(["ffmpeg", "-y", "-i", str(short),
         "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black",
         "-map", "0:v:0", "-map", "0:a:0", "-af", "apad",
         "-t", str(REQ),
         "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", str(old)])
    vo, ao = vdur(old), adur(old)
    check("4. 老链路确实截断（画面 <0.5s 而声音 ~1.5s）",
          vo < 0.5 and ao > 1.2, f"画面 {vo:.3f}s / 声音 {ao:.3f}s")

    # ---- 5：_get_duration 取视频流时长 ----
    print("\n[5] _get_duration 取视频流而非容器时长")
    mism = WORK / "mismatch.mp4"
    run(["ffmpeg", "-y", "-i", str(short),
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-map", "0:v:0", "-map", "1:a:0", "-t", "1.5",
         "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", str(mism)])
    container = 1.5
    gd = r._get_duration(str(mism))
    check("5. _get_duration 返回视频流时长", gd < 1.0 and gd > 0.1,
          f"容器 {container}s → 返回 {gd:.3f}s（实际画面 {vdur(mism):.3f}s）")

    # ---- 6/7：拍数不得超过画面能撑满的整拍数；撑不满 1 拍则跳过 ----
    print("\n[6-7] BeatSyncEngine 拍数受画面长度约束")
    beats = [Beat(time=round(0.5 * i, 3), strength=1.0,
                  beat_type="strong" if i % 2 == 0 else "normal")
             for i in range(40)]
    shots = [
        Shot(shot_id=1, start_time=0.0, end_time=1.0, duration=1.00,
             file_path=str(long_), highlight_score=0.9),   # 3.0s 画面
        Shot(shot_id=2, start_time=0.0, end_time=0.20, duration=0.20,
             file_path=str(short), highlight_score=0.9),    # 0.20s 画面 → 应被跳过
        Shot(shot_id=3, start_time=0.0, end_time=1.05, duration=1.05,
             file_path=str(long_), highlight_score=0.5),    # 1.05s 画面
    ]
    tl = BeatSyncEngine().sync(shots, beats, style="calm")
    durs = {e.shot_id: e.duration for e in tl}
    avail = {1: 1.00, 3: 1.05}
    over = [(k, d) for k, d in durs.items() if d > avail.get(k, 0.0) + 1e-6]
    check("6. 无条目时长超过镜头可用画面", not over,
          f"时长 {durs} / 可用 {avail}")
    check("7. 0.20s 镜头被跳过（撑不满 1 拍）", 2 not in durs,
          f"入选 shot_id {sorted(durs)}")

    # ---- 8：端到端成片音画等长 ----
    print("\n[8] 端到端 render：成片音画等长")
    bgm = WORK / "bgm.m4a"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=12",
         "-c:a", "aac", "-b:a", "128k", str(bgm)])
    entries = [e for e in tl if e.shot_id in (1, 3)]
    out_final = WORK / "final.mp4"
    r.render(entries, str(bgm), str(out_final))
    vf, af = vdur(out_final), adur(out_final)
    check("8. 成片画面≈声音(±0.2s) 且 >0.8s",
          abs(vf - af) <= 0.2 and vf > 0.8,
          f"画面 {vf:.3f}s / 声音 {af:.3f}s ({n_frames(out_final)} 帧)")

    print("\n" + "=" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
