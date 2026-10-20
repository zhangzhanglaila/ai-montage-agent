"""
转场模板系统 - 预定义转场节奏模式

每个模板定义了强拍和弱拍分别使用什么转场，以及转场的时长比例。
模板存储在 JSON 文件中，支持用户自定义扩展。
"""

import json
import random
from pathlib import Path
from typing import Dict, List, Optional

PATTERNS_DIR = Path(__file__).parent / "transition_patterns"

# FFmpeg xfade 支持的完整映射
XFADE_MAP = {
    # 基础
    "cut": None,  # 不用 xfade，直接拼接
    "fade": "fade",
    "dissolve": "dissolve",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    # 擦除
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "wipeup": "wipeup",
    "wipedown": "wipedown",
    # 滑动
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    # 平滑
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
    "smoothup": "smoothup",
    "smoothdown": "smoothdown",
    # 圆形
    "circleopen": "circleopen",
    "circleclose": "circleclose",
    "circlecrop": "circlecrop",
    # 矩形
    "rectcrop": "rectcrop",
    # 对角线
    "diagtl": "diagtl",
    "diagtr": "diagtr",
    "diagbl": "diagbl",
    "diagbr": "diagbr",
    # 垂直/水平开合
    "vertopen": "vertopen",
    "vertclose": "vertclose",
    "horzopen": "horzopen",
    "horzclose": "horzclose",
    # 特效
    "radial": "radial",
    "pixelize": "pixelize",
    "distance": "distance",
    # 切片
    "hlslice": "hlslice",
    "hrslice": "hrslice",
    "vuslice": "vuslice",
    "vdslice": "vdslice",
    # 无
    "none": None,
}


PATTERNS_DIR.mkdir(parents=True, exist_ok=True)


def _load_patterns_from_dir() -> Dict[str, dict]:
    """从目录加载所有转场模板"""
    patterns = {}
    if not PATTERNS_DIR.exists():
        return patterns
    for f in sorted(PATTERNS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "id" in data:
                patterns[data["id"].lower()] = data
        except (json.JSONDecodeError, KeyError):
            continue
    return patterns


# 内置模板（不需要文件）
BUILTIN_PATTERNS = {
    "auto": {
        "id": "auto",
        "name": "智能推荐",
        "description": "根据节拍和风格自动选择转场",
        "icon": "🤖",
        "strong_beats": ["cut", "fade", "flash", "zoom"],
        "weak_beats": ["cut", "dissolve"],
        "strong_duration_ratio": 0.3,
        "weak_duration_ratio": 0.5,
        "use_motion": True,
    },
    "hard_cut": {
        "id": "hard_cut",
        "name": "硬切流",
        "description": "全部硬切，干净利落，适合快节奏集锦",
        "icon": "✂️",
        "strong_beats": ["cut"],
        "weak_beats": ["cut"],
        "strong_duration_ratio": 0.0,
        "weak_duration_ratio": 0.0,
        "use_motion": False,
    },
    "mtv_energy": {
        "id": "mtv_energy",
        "name": "MTV 高能",
        "description": "闪光+缩放+硬切交替，MV/高燃集锦必备",
        "icon": "⚡",
        "strong_beats": ["flash", "zoom", "cut", "shake"],
        "weak_beats": ["cut", "cut", "dissolve"],
        "strong_duration_ratio": 0.25,
        "weak_duration_ratio": 0.0,
        "use_motion": False,
    },
    "cinematic_flow": {
        "id": "cinematic_flow",
        "name": "电影质感",
        "description": "溶解+淡入淡出为主，优雅平滑",
        "icon": "🎬",
        "strong_beats": ["dissolve", "fade", "fadeblack"],
        "weak_beats": ["dissolve", "fade", "wipeleft"],
        "strong_duration_ratio": 0.6,
        "weak_duration_ratio": 0.8,
        "use_motion": False,
    },
    "smooth_slide": {
        "id": "smooth_slide",
        "name": "丝滑滑动",
        "description": "方向滑动转场，流畅的空间感",
        "icon": "🌊",
        "strong_beats": ["slideleft", "slideright", "smoothleft"],
        "weak_beats": ["dissolve", "wipeleft", "smoothright"],
        "strong_duration_ratio": 0.4,
        "weak_duration_ratio": 0.6,
        "use_motion": True,
    },
    "action_whip": {
        "id": "action_whip",
        "name": "动作甩切",
        "description": "快速模糊+滑动，打斗/运动场景专用",
        "icon": "💥",
        "strong_beats": ["motion_blur", "slideleft", "cut", "wipeleft"],
        "weak_beats": ["cut", "cut"],
        "strong_duration_ratio": 0.2,
        "weak_duration_ratio": 0.0,
        "use_motion": True,
    },
    "dreamy_fade": {
        "id": "dreamy_fade",
        "name": "梦幻渐变",
        "description": "圆形开合+径向+像素化，Lo-Fi/氛围感",
        "icon": "🌙",
        "strong_beats": ["circleopen", "radial", "fade"],
        "weak_beats": ["dissolve", "circleclose", "pixelize"],
        "strong_duration_ratio": 0.7,
        "weak_duration_ratio": 0.9,
        "use_motion": False,
    },
    "retro_wipe": {
        "id": "retro_wipe",
        "name": "复古擦除",
        "description": "对角线+矩形裁切，80年代复古风",
        "icon": "📼",
        "strong_beats": ["diagtl", "diagbr", "rectcrop"],
        "weak_beats": ["wipeleft", "dissolve", "fadeblack"],
        "strong_duration_ratio": 0.5,
        "weak_duration_ratio": 0.7,
        "use_motion": False,
    },
    "vlog_gentle": {
        "id": "vlog_gentle",
        "name": "Vlog 温柔",
        "description": "淡入淡出+柔和溶解，日常 Vlog 适用",
        "icon": "📷",
        "strong_beats": ["fade", "dissolve"],
        "weak_beats": ["fade", "dissolve", "wipeleft"],
        "strong_duration_ratio": 0.5,
        "weak_duration_ratio": 0.7,
        "use_motion": False,
    },
    "random_mix": {
        "id": "random_mix",
        "name": "随机混搭",
        "description": "每次都不一样的随机转场组合",
        "icon": "🎲",
        "strong_beats": [
            "fade", "dissolve", "wipeleft", "slideleft", "circleopen",
            "radial", "flash", "zoom", "diagtl", "pixelize", "fadeblack"
        ],
        "weak_beats": [
            "cut", "dissolve", "fade", "wipeleft", "smoothleft"
        ],
        "strong_duration_ratio": 0.4,
        "weak_duration_ratio": 0.6,
        "use_motion": False,
    },
}


def load_all_patterns() -> Dict[str, dict]:
    """加载所有转场模板（内置 + 文件）"""
    patterns = dict(BUILTIN_PATTERNS)
    patterns.update(_load_patterns_from_dir())
    return patterns


def get_pattern(pattern_id: str) -> dict:
    """获取指定模板"""
    patterns = load_all_patterns()
    key = pattern_id.lower()
    if key not in patterns:
        return patterns.get("auto", BUILTIN_PATTERNS["auto"])
    return patterns[key]


def list_patterns() -> List[dict]:
    """列出所有模板（供 API 使用）"""
    patterns = load_all_patterns()
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "description": p["description"],
            "icon": p.get("icon", "🎨"),
        }
        for p in patterns.values()
    ]


def pick_transition(pattern: dict, beat_type: str, motion_score: float = 0.0) -> tuple:
    """
    根据模板和拍类型选择转场

    Args:
        pattern: 转场模板
        beat_type: "strong" 或 "weak"
        motion_score: 运动分数 (0-1)，use_motion=True 时影响选择

    Returns:
        (transition_type, duration_ratio) 元组
    """
    if beat_type == "strong":
        candidates = pattern.get("strong_beats", ["cut"])
        dur_ratio = pattern.get("strong_duration_ratio", 0.3)
    else:
        candidates = pattern.get("weak_beats", ["cut"])
        dur_ratio = pattern.get("weak_duration_ratio", 0.5)

    if not candidates:
        return "cut", 0.0

    # 如果启用运动感知，高运动时偏向快速转场
    if pattern.get("use_motion") and motion_score > 0.7:
        fast_types = {"cut", "slideleft", "slideright", "wipeleft", "motion_blur", "flash"}
        fast_candidates = [c for c in candidates if c in fast_types]
        if fast_candidates:
            candidates = fast_candidates
            dur_ratio *= 0.6  # 高运动时缩短转场时长

    chosen = random.choice(candidates)
    return chosen, dur_ratio


def transition_to_xfade(transition_type: str) -> Optional[str]:
    """将转场类型映射到 FFmpeg xfade 名称"""
    return XFADE_MAP.get(transition_type, "fade")
