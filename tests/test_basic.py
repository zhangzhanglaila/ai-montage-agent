"""
基本功能测试
"""

import pytest
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent / "packages"))

def test_shot_detector_import():
    """测试ShotDetector导入"""
    from video_understanding.src.shot_detector import ShotDetector
    assert ShotDetector is not None

def test_beat_detector_import():
    """测试BeatDetector导入"""
    from beat_engine.src.beat_detector import BeatDetector
    assert BeatDetector is not None

def test_highlight_scorer_import():
    """测试HighlightScorer导入"""
    from video_understanding.src.highlight_scorer import HighlightScorer
    assert HighlightScorer is not None

def test_beat_sync_engine_import():
    """测试BeatSyncEngine导入"""
    from beat_engine.src.beat_sync_engine import BeatSyncEngine
    assert BeatSyncEngine is not None

def test_timeline_planner_import():
    """测试TimelinePlanner导入"""
    from timeline_engine.src.timeline_planner import TimelinePlanner
    assert TimelinePlanner is not None

def test_transition_engine_import():
    """测试TransitionEngine导入"""
    from montage_engine.src.transition_engine import TransitionEngine
    assert TransitionEngine is not None

def test_transition_patterns_import():
    """测试转场模板可导入（历史上 Path.mkdir 返回值被当装饰器，WebUI 必崩）"""
    from montage_engine.src.transition_patterns import get_pattern
    pattern = get_pattern("auto")
    assert pattern["id"] == "auto"

def test_ffmpeg_executor_import():
    """测试FFmpegExecutor导入"""
    from render_engine.src.ffmpeg_executor import FFmpegExecutor
    assert FFmpegExecutor is not None

def test_emotion_curve():
    """测试情绪曲线生成"""
    from timeline_engine.src.emotion_curve import EmotionCurve

    curve = EmotionCurve()
    points = curve.generate(10.0, "dynamic")

    assert len(points) > 0
    assert all(0 <= p.intensity <= 1 for p in points)

def test_rhythm_planner():
    """测试节奏规划器"""
    from timeline_engine.src.rhythm_planner import RhythmPlanner

    planner = RhythmPlanner()
    points = planner.generate(10.0, "dynamic")

    assert len(points) > 0
    assert all(0 <= p.energy <= 1 for p in points)

def test_highlight_scorer():
    """测试高光评分"""
    from video_understanding.src.highlight_scorer import HighlightScorer

    scorer = HighlightScorer()

    # 测试评分
    score = scorer.score_shot(
        shot_id=1,
        motion_data={"magnitude": 5.0},
        face_data={"emotions": [{"type": "angry", "confidence": 0.9}]},
        camera_data={"shake": 0.5, "zoom": 0.2, "pan_speed": 0.3}
    )

    assert 0 <= score.overall_score <= 1

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
