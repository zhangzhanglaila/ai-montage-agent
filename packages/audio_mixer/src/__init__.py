"""
Audio Mixer Package
多轨音频混音包 - 人声/BGM/音效 独立增益、延迟、循环与自动闪避
"""

from .mixer import (
    AudioTrack, AudioMixer, ROLES, DEFAULT_SR,
    detect_active_windows, merge_windows,
    mix_tracks_from_spec, analyze_audio,
    probe_duration, has_audio,
)

__all__ = [
    "AudioTrack", "AudioMixer", "ROLES", "DEFAULT_SR",
    "detect_active_windows", "merge_windows",
    "mix_tracks_from_spec", "analyze_audio",
    "probe_duration", "has_audio",
]
