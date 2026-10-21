"""向后兼容入口：核心实现已拆到 packages/montage。"""

import sys
from pathlib import Path

# Windows 控制台默认 GBK，标题里的特殊字符不应让任务直接失败
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))

from packages.montage import (  # noqa: E402
    BeatAnalyzer, BeatSyncEngine, HighlightScorer, MontagePipeline,
    MotionAnalyzer, ShotDetector, VideoRenderer,
)
from packages.montage.cli import (  # noqa: E402
    _apply_enhancement, _apply_subtitles, _crawl_videos, _export_timeline, main,
)
from packages.montage.helpers import (  # noqa: E402
    _fmt_ts, _parse_ratio, _print_task_detail, _print_task_table,
    _probe_media_duration, _ratio_suffix, _safe_float,
)

__all__ = [
    "MontagePipeline", "ShotDetector", "MotionAnalyzer", "BeatAnalyzer",
    "HighlightScorer", "BeatSyncEngine", "VideoRenderer",
    "_safe_float", "_parse_ratio", "_ratio_suffix", "_fmt_ts",
    "_print_task_table", "_print_task_detail", "_probe_media_duration",
    "_apply_enhancement", "_apply_subtitles", "_export_timeline",
    "_crawl_videos", "main",
]

if __name__ == "__main__":
    main()
