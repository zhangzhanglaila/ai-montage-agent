"""
Shot Index Package
镜头索引包 - CLIP/离线特征 语义镜头索引与检索
"""

from .embedder import (
    BaseEmbedder, ClipEmbedder, HeuristicEmbedder,
    get_embedder, embedder_status,
)
from .index_store import (
    ShotIndex, ShotRecord, INDEX_VERSION,
    detect_shots, detect_scene_cuts, extract_frames, probe_duration,
)
from .retriever import ShotRetriever, ShotHit, format_hits

__all__ = [
    "BaseEmbedder", "ClipEmbedder", "HeuristicEmbedder",
    "get_embedder", "embedder_status",
    "ShotIndex", "ShotRecord", "INDEX_VERSION",
    "detect_shots", "detect_scene_cuts", "extract_frames", "probe_duration",
    "ShotRetriever", "ShotHit", "format_hits",
]
