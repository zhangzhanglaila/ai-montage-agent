"""通用小工具：数值/比例/时间/媒体探测。"""

import json
import math
import subprocess
import time


def _safe_float(value, default: float) -> float:
    """把可能为 -inf / inf / nan 的数值安全转换为有限 float。

    loudnorm 在遇到静音或极低电平输入时，会把 input_i 等字段输出为 "-inf"，
    直接带入第二遍滤镜会导致 ffmpeg 报错，因此这里统一兜底。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _parse_ratio(value, default: float = 9 / 16) -> float:
    """解析比例字符串（"9:16" / "0.5625"）为宽高比 float。"""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else default
    s = str(value or "").strip()
    if not s:
        return default
    try:
        if ":" in s:
            a, b = s.split(":", 1)
            fa, fb = float(a), float(b)
            return fa / fb if fb else default
        v = float(s)
        return v if v > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


# 常见目标比例 -> 文件名后缀（直接用浮点数拼名可读性差，这里给可读标签）
_ASPECT_SUFFIX = {
    16 / 9: "16x9",
    9 / 16: "9x16",
    1.0: "1x1",
    4 / 3: "4x3",
    3 / 4: "3x4",
    4 / 5: "4x5",
    21 / 9: "21x9",
    2.35: "235x100",
}


def _ratio_suffix(aspect: float, default_aspect: float = 9 / 16) -> str:
    """把宽高比 float 转成文件名后缀，如 9:16 -> "_9x16"。"""
    try:
        aspect = float(aspect)
    except (TypeError, ValueError):
        aspect = default_aspect
    if not math.isfinite(aspect) or aspect <= 0:
        aspect = default_aspect
    for ratio, label in _ASPECT_SUFFIX.items():
        if abs(aspect - ratio) < 1e-3:
            return f"_{label}"
    # 非常见比例：退化为可读的十进制（小数点转 p，避免文件名带点）
    txt = f"{aspect:.4f}".rstrip("0").rstrip(".").replace(".", "p")
    return f"_{txt}"


def _fmt_ts(ts) -> str:
    """时间戳 -> 本地时间字符串。"""
    if not ts:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


def _print_task_table(rows) -> None:
    """打印任务列表。"""
    if not rows:
        print("  （无任务）")
        return
    print(f"  {'ID':14s} {'KIND':8s} {'STATUS':9s} {'TRY':5s} {'PROG':6s} "
          f"{'UPDATED':15s} MESSAGE")
    for t in rows:
        tries = f"{t.get('attempts', 0)}/{t.get('max_attempts', 0)}"
        print(f"  {t['id'][:14]:14s} {str(t.get('kind', ''))[:8]:8s} "
              f"{t['status']:9s} {tries:5s} {t.get('progress', 0):>4d}%  "
              f"{_fmt_ts(t.get('updated_at')):15s} "
              f"{str(t.get('message') or '')[:38]}")


def _print_task_detail(task) -> None:
    """打印单个任务详情。"""
    for key in ("id", "kind", "status", "progress", "attempts", "max_attempts",
                "priority", "output_path", "error", "message",
                "created_at", "updated_at", "started_at", "finished_at"):
        value = task.get(key)
        if key.endswith("_at"):
            value = _fmt_ts(value)
        print(f"  {key:13s}: {value}")
    payload = task.get("payload") or {}
    brief = json.dumps(payload, ensure_ascii=False)
    print(f"  {'payload':13s}: {brief[:400]}")




def _probe_media_duration(path: str) -> float:
    """获取媒体时长（秒），失败返回 0。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


