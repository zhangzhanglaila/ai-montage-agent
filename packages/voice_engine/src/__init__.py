"""
Voice Engine Package
配音引擎包 - 负责旁白脚本生成、语音合成、旁白混音
"""

from .script_writer import (
    ScriptWriter, NarrationLine, estimate_duration, CHARS_PER_SEC,
)
from .tts_engine import TtsEngine, VOICES
from .narration_mixer import mix_narration, probe_duration

__all__ = [
    "ScriptWriter", "NarrationLine", "estimate_duration", "CHARS_PER_SEC",
    "TtsEngine", "VOICES",
    "mix_narration", "probe_duration",
]
