"""把用户的一句话效果描述解析成 pipeline 参数。

不依赖 LLM：用关键词 + 正则做确定性映射，离线可用。
WebUI 的"效果描述"输入框和 CLI 都可以复用。
"""

import re
from typing import Any, Dict


def _has(text: str, *words: str) -> bool:
    return any(w.lower() in text for w in words)


def parse_effect_prompt(prompt: str) -> Dict[str, Any]:
    """解析效果描述，返回只包含识别到的参数。

    支持的值：
        style: dynamic / intense / calm
        enable_sfx: bool
        shot_density: standard / dense / max
        threshold: float
        target_duration: float
        mute_source_audio: bool
        clean_black: bool
        aspect_ratio: "16/9" / "9/16" / "1/1" / "4/5"
    """
    text = (prompt or "").strip().lower()
    if not text:
        return {}

    out: Dict[str, Any] = {}

    # 风格
    if _has(text, "高燃", "热血", "战斗", "打斗", "动作", "爆炸", "燃", "amv", "踩点", "卡点"):
        out["style"] = "intense"
    elif _has(text, "丝滑", "柔和", "治愈", "唯美", "抒情", "慢节奏", "温柔"):
        out["style"] = "calm"

    # 音效
    if _has(text, "音效", "打击", "impact", "whoosh", "卡点", "踩点", "鼓点"):
        out["enable_sfx"] = True

    # 镜头密度
    if _has(text, "极密", "尽量多", "用满", "快剪", "高燃", "密集"):
        out["shot_density"] = "max"
    elif _has(text, "多一点", "密", "紧凑"):
        out["shot_density"] = "dense"

    # 动漫素材通常切点更碎，阈值高一点更稳
    if _has(text, "动漫", "动画", "二次元", "amv"):
        out.setdefault("threshold", 0.5)

    # 时长："10秒" / "20s" / "做成15秒"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec)", text)
    if m:
        val = float(m.group(1))
        if 2 <= val <= 300:
            out["target_duration"] = val

    # 音频处理
    if _has(text, "只留bgm", "只保留bgm", "静音原声", "去掉原声", "不要原声", "纯音乐"):
        out["mute_source_audio"] = True
    elif _has(text, "保留原声", "原声"):
        out["mute_source_audio"] = False

    # 黑场
    if _has(text, "不要黑场", "无黑场", "去黑场", "去掉黑场", "不要黑屏"):
        out["clean_black"] = True

    # 比例
    if _has(text, "竖屏", "9:16", "9/16", "抖音", "tiktok"):
        out["aspect_ratio"] = "9/16"
    elif _has(text, "方形", "1:1", "1/1"):
        out["aspect_ratio"] = "1/1"
    elif _has(text, "4:5", "4/5", "小红书"):
        out["aspect_ratio"] = "4/5"

    return out
