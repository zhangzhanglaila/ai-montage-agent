"""F2.5 冒烟测试：低码率实时预览

验证方式：
    1. 代理生成：宽度 ≤ 480、码率显著低于原片、耗时记录；
    2. **缓存命中**：第二次请求必须直接返回缓存（cached=True 且耗时≈0）；
    3. 参数预览：带调色的小片段，尺寸/时长符合请求；
    4. **HTTP 端点**：用 FastAPI TestClient 真实打一遍
       /proxy、/proxy/video、/preview、/preview/{id}；
    5. 清缓存生效。

用法：
    python scripts/smoke_preview.py
产出：
    cache/preview/*.mp4
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REAL_CANDIDATES = [
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
    for c in REAL_CANDIDATES:
        if c.exists() and c.stat().st_size > 100_000:
            return c
    raise FileNotFoundError("找不到可用的测试视频")


def main() -> int:
    ap = argparse.ArgumentParser(description="F2.5 实时预览冒烟测试")
    ap.add_argument("--video", default=None)
    args = ap.parse_args()

    print("=" * 54)
    print("F2.5 冒烟：低码率实时预览")
    print("=" * 54)

    src = pick_video(args.video)
    print(f"\n素材: {src}")

    from packages.webui.src.preview import (
        get_proxy, get_param_preview, file_bitrate_kbps, probe_size,
        clear_cache, CACHE_DIR,
    )

    clear_cache(CACHE_DIR)
    src_kbps = file_bitrate_kbps(str(src))
    print(f"原片码率 ≈ {src_kbps:.0f} kbps, 尺寸 {probe_size(str(src))}")

    print("\n[1/5] 生成代理...")
    t0 = time.time()
    info = get_proxy(str(src), width=480, crf=32)
    wall = time.time() - t0
    print(f"  {Path(info['path']).name}  {info['width']}x{info['height']}  "
          f"{info['kbps']}kbps  build={info['build_ms']}ms  cached={info['cached']}")
    assert Path(info["path"]).exists(), "代理文件未生成"
    assert info["width"] <= 480, f"代理宽度应 ≤480，实际 {info['width']}"
    assert info["kbps"] < src_kbps * 0.7, \
        f"代理码率未明显低于原片（{info['kbps']} vs {src_kbps}）"
    assert not info["cached"], "首次生成不应命中缓存"
    print(f"  墙钟耗时 {wall:.2f}s")

    print("[2/5] 缓存命中...")
    info2 = get_proxy(str(src), width=480, crf=32)
    print(f"  cached={info2['cached']}  build={info2['build_ms']}ms")
    assert info2["cached"] is True, "第二次请求未命中缓存"
    assert info2["path"] == info["path"], "缓存路径不一致"
    assert info2["build_ms"] < 50, f"缓存命中耗时过高: {info2['build_ms']}ms"

    print("[3/5] 参数预览（带调色）...")
    pv = get_param_preview(str(src), start=0.5, duration=4.0, width=480,
                           color_preset="cinematic")
    print(f"  {Path(pv['path']).name}  {pv['width']}x{pv['height']}  "
          f"dur={pv['duration']}s  {pv['kbps']}kbps  build={pv['build_ms']}ms")
    assert Path(pv["path"]).exists(), "参数预览未生成"
    assert pv["duration"] <= 4.6, f"预览时长超出请求（{pv['duration']}s）"
    assert pv["width"] <= 480

    print("[4/5] HTTP 端点（TestClient）...")
    from fastapi.testclient import TestClient
    from packages.webui.src.app import app, _tasks

    tid = uuid.uuid4().hex
    _tasks[tid] = {
        "id": tid, "status": "done", "progress": 100,
        "message": "完成", "output_path": str(src), "previews": [],
    }
    client = TestClient(app)

    r = client.get(f"/api/task/{tid}/proxy", params={"width": 480, "crf": 32})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get("status") == "ok", j
    assert j.get("cached") is True, f"应命中缓存: {j}"
    print(f"  GET /proxy -> ok cached={j['cached']} url={j['url']}")

    rv = client.get(f"/api/task/{tid}/proxy/video", params={"width": 480, "crf": 32})
    assert rv.status_code == 200 and len(rv.content) > 2000, "代理视频响应异常"
    print(f"  GET /proxy/video -> {len(rv.content)} bytes")

    rp = client.post(f"/api/task/{tid}/preview",
                     data={"start": 0, "duration": 3, "color_grade": "cinematic"})
    assert rp.status_code == 200, rp.text
    jp = rp.json()
    assert jp.get("status") == "ok", jp
    print(f"  POST /preview -> {jp['preview_id']} {jp['width']}x{jp['height']} "
          f"{jp['duration']}s build={jp['build_ms']}ms url={jp['url']}")

    rpf = client.get(jp["url"])
    assert rpf.status_code == 200 and len(rpf.content) > 2000, "预览文件响应异常"
    print(f"  GET {jp['url']} -> {len(rpf.content)} bytes")

    print("[5/5] 清空缓存...")
    n = clear_cache(CACHE_DIR)
    print(f"  删除 {n} 个缓存文件")
    assert n > 0, "缓存清空无效"

    print("\n" + "=" * 54)
    print("冒烟通过 ✓ （代理 / 缓存命中 / 参数预览 / HTTP 端点 / 清缓存）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
