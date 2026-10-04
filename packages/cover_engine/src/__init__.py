"""
Cover Engine Package
封面引擎包 - 自动挑帧并生成带标题的封面图
"""

from .cover_maker import (
    pick_best_frame,
    make_cover,
    generate_cover,
    ASPECT_SIZES,
    COVER_STYLES,
)

__all__ = [
    "pick_best_frame",
    "make_cover",
    "generate_cover",
    "ASPECT_SIZES",
    "COVER_STYLES",
]
