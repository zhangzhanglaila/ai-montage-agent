"""
Montage Engine Package
蒙太奇引擎包 - 负责转场效果、视频合成
"""

from .transition_engine import TransitionEngine
from .video_composer import VideoComposer
from .speed_curve import (
    SpeedCurve, SpeedSegment, PRESETS,
    apply_speed_curve, speed_curve_for_video,
    build_filter_complex, atempo_chain, probe_duration,
)

__all__ = [
    "TransitionEngine", "VideoComposer",
    "SpeedCurve", "SpeedSegment", "PRESETS",
    "apply_speed_curve", "speed_curve_for_video",
    "build_filter_complex", "atempo_chain", "probe_duration",
]
