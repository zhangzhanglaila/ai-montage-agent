"""
基本功能测试（核心实现已统一到 packages/montage）
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages"))


def test_montage_core_imports():
    """核心类都能从 packages.montage 导入"""
    from packages.montage import (
        BeatAnalyzer, BeatSyncEngine, HighlightScorer, MontagePipeline,
        MotionAnalyzer, ShotDetector, VideoRenderer,
    )
    assert all([
        MontagePipeline, ShotDetector, MotionAnalyzer, BeatAnalyzer,
        HighlightScorer, BeatSyncEngine, VideoRenderer,
    ])


def test_pipeline_shim_imports():
    """根 pipeline.py 兼容入口仍导出旧名字"""
    from pipeline import MontagePipeline, ShotDetector, VideoRenderer
    assert MontagePipeline and ShotDetector and VideoRenderer


def test_parse_ratio():
    from pipeline import _parse_ratio
    assert abs(_parse_ratio("9:16") - 9 / 16) < 1e-6
    assert abs(_parse_ratio("16:9") - 16 / 9) < 1e-6
    assert _parse_ratio("bad") == 9 / 16


def test_highlight_scorer():
    from packages.core_types.models import MotionData, Shot
    from packages.montage import HighlightScorer

    shot = Shot(shot_id=1, start_time=0.0, end_time=3.0, duration=3.0)
    score = HighlightScorer().score(
        shot, MotionData(magnitude=5.0, shake=0.5, zoom=1.2)
    )
    assert 0.0 <= score <= 1.0


def test_transition_engine_import():
    from montage_engine.src.transition_engine import TransitionEngine
    assert TransitionEngine is not None


def test_transition_patterns_import():
    """转场模板可导入（历史上 Path.mkdir 返回值被当装饰器，WebUI 必崩）"""
    from montage_engine.src.transition_patterns import get_pattern
    pattern = get_pattern("auto")
    assert pattern["id"] == "auto"


def test_ffmpeg_executor_import():
    from render_engine.src.ffmpeg_executor import FFmpegExecutor
    assert FFmpegExecutor is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
