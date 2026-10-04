"""
Sound Engine Package
音效引擎包 - 负责按节拍规划并叠加音效
"""

from .sfx_engine import SfxEngine, SfxCue, DEFAULT_GAIN_DB, STYLE_DENSITY
from .sfx_library import SfxLibrary, KINDS

__all__ = [
    "SfxEngine", "SfxCue", "DEFAULT_GAIN_DB", "STYLE_DENSITY",
    "SfxLibrary", "KINDS",
]
