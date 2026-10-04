"""F2.1 冒烟测试：语义镜头索引与检索

用**真实成片**建索引（不是合成测试图案），然后做行为断言：
    1. 建索引 N 个镜头，存/读往返一致
    2. 多个查询都返回按分数降序的结果
    3. 「暗」的 top-1 代表帧平均亮度 < 「亮」的 top-1  —— 证明检索确实有意义
    4. 命中镜头可切片导出

素材优先级：output/test_montage8.mp4 → output/demo_final.mp4 → 合成兜底。

用法：
    python scripts/smoke_index.py [--video xxx.mp4] [--prefer auto|heuristic]
"""

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CANDIDATES = [
    ROOT / "output" / "test_montage8.mp4",
    ROOT / "output" / "demo_final.mp4",
    ROOT / "output" / "test_montage.mp4",
]


def pick_video(explicit: str = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(f"指定视频不存在: {explicit}")
        return p
    for c in CANDIDATES:
        if c.exists() and c.stat().st_size > 100_000:
            return c
    return _make_synthetic()


def _make_synthetic() -> Path:
    """兜底：合成一段有明暗/色彩变化的视频（仅当没有真实素材时用）。"""
    dst = OUT_DIR / "index_synthetic.mp4"
    if dst.exists():
        return dst
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=0x101010:s=640x360:d=2",
        "-f", "lavfi", "-i", "color=c=0xf0f0f0:s=640x360:d=2",
        "-f", "lavfi", "-i", "color=c=0xd02020:s=640x360:d=2",
        "-f", "lavfi", "-i", "color=c=0x2040d0:s=640x360:d=2",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dst),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return dst


def mean_brightness(frame_path: str) -> float:
    from PIL import Image
    with Image.open(frame_path) as im:
        g = np.asarray(im.convert("L"), dtype=np.float32)
    return float(g.mean())


def main() -> int:
    ap = argparse.ArgumentParser(description="F2.1 语义镜头检索冒烟测试")
    ap.add_argument("--video", default=None, help="指定素材视频")
    ap.add_argument("--prefer", default="auto", choices=["auto", "heuristic", "clip"])
    ap.add_argument("--max-shots", type=int, default=14)
    args = ap.parse_args()

    print("=" * 52)
    print("F2.1 冒烟：语义镜头索引与检索")
    print("=" * 52)

    video = pick_video(args.video)
    print(f"\n素材: {video}")

    from packages.shot_index import (
        get_embedder, ShotIndex, ShotRetriever, format_hits,
    )

    emb = get_embedder(prefer=args.prefer)
    print(f"编码器: {emb.name} (dim={emb.dim})")

    print("\n[1/5] 建索引...")
    idx = ShotIndex(embedder=emb)
    idx.build(str(video), max_shots=args.max_shots, fps=2.0)
    assert len(idx) > 0, "索引为空"
    assert all(r.embedding for r in idx.records), "存在未编码的镜头"
    print(f"  共 {len(idx)} 个镜头")

    print("[2/5] 存/读往返...")
    idx_path = OUT_DIR / "shot_index.json"
    idx.save(str(idx_path))
    idx2 = ShotIndex.load(str(idx_path), embedder=emb)
    assert len(idx2) == len(idx), "往返后镜头数不一致"
    assert np.allclose(idx.matrix(), idx2.matrix(), atol=1e-5), "往返后 embedding 不一致"
    print(f"  OK（{idx_path.name} + .npy）")

    retriever = ShotRetriever(idx2)

    print("[3/5] 多查询排序检查...")
    queries = ["明亮的画面", "暗的画面", "暖色 夕阳", "冷色 蓝色", "鲜艳 色彩丰富"]
    results = {}
    for q in queries:
        hits = retriever.search(query=q, top_k=5)
        assert hits, f"查询「{q}」无命中"
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True), f"「{q}」结果未按分数降序"
        results[q] = hits
        print(f"  「{q}」 top1: {hits[0].label()}")

    print("[4/5] 行为断言：亮 vs 暗 的 top-1 是否真的分得开...")
    b_bright = mean_brightness(results["明亮的画面"][0].frame_path)
    b_dark = mean_brightness(results["暗的画面"][0].frame_path)
    print(f"  「明亮」top1 亮度={b_bright:.1f}   「暗」top1 亮度={b_dark:.1f}")
    assert b_bright > b_dark, (
        f"检索未区分明暗（亮={b_bright:.1f} 应 > 暗={b_dark:.1f}）")

    print("[5/5] 导出命中切片...")
    files = retriever.export_clips(results["鲜艳 色彩丰富"][:3], str(OUT_DIR / "index_clips"))
    assert files, "未导出任何切片"
    for f in files:
        assert Path(f).stat().st_size > 1000

    print("\n" + "=" * 52)
    print("冒烟通过 ✓ （索引/往返/排序/明暗区分/切片导出 全部 OK）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
