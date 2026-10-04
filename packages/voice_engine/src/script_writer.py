"""旁白脚本生成。

优先用 OpenAI 兼容 LLM 生成解说词；LLM 不可用时自动降级为模板脚本，
保证离线也能跑通流程。

API 配置与 ai_director 保持一致：
    OPENAI_API_BASE / OPENAI_API_KEY / LLM_MODEL
"""

import os
import re
from dataclasses import dataclass
from typing import List, Optional

# 中文语音语速估算（字/秒），用于粗略估算时长
CHARS_PER_SEC = 4.5


@dataclass
class NarrationLine:
    """一句旁白"""
    text: str
    est_duration: float = 0.0

    def to_dict(self) -> dict:
        return {"text": self.text, "est_duration": round(self.est_duration, 2)}


def estimate_duration(text: str, chars_per_sec: float = CHARS_PER_SEC) -> float:
    """粗略估算中文朗读时长（秒）"""
    n = len(re.sub(r"\s", "", text))
    return round(n / chars_per_sec, 2)


class ScriptWriter:
    """旁白脚本生成器"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        self.api_base = api_base or os.environ.get("OPENAI_API_BASE", "http://localhost:11434/v1")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "sk-placeholder")
        self.model = model or os.environ.get("LLM_MODEL", "qwen2.5:7b")
        self.timeout = timeout

    # ------------------------------------------------------------------ write
    def write(self, topic: str, target_sec: int = 30, style: str = "解说") -> List[NarrationLine]:
        """生成旁白脚本。

        Args:
            topic: 主题
            target_sec: 目标时长（秒）
            style: 风格（解说/盘点/煽情/科普...）

        Returns:
            NarrationLine 列表；LLM 不可用时返回模板脚本
        """
        prompt = (
            f"你是短视频{style}文案，请为主题「{topic}」写一段约 {target_sec} 秒的中文旁白。\n"
            f"要求：口语化、有节奏、每句不超过 25 字；只输出旁白正文，"
            f"每句一行，不要编号、不要多余说明。"
        )
        raw = self._query_llm(prompt)
        if not raw:
            print("  [旁白] LLM 不可用，使用模板脚本兜底")
            return self.offline(topic, target_sec)
        return self.from_text(raw)

    def offline(self, topic: str, target_sec: int = 30) -> List[NarrationLine]:
        """离线模板脚本（LLM 不可用时兜底）

        每句都刻意写短：既贴合口语节奏，也便于逐字字幕排版。
        """
        n = max(3, min(10, int(target_sec // 5)))
        templates = [
            f"今天聊聊{topic}。",
            "先看第一个细节。",
            "这个画面张力十足。",
            "节奏开始加快了。",
            "注意这里的转折。",
            "越到后面越精彩。",
            "这就是它的魅力。",
            "喜欢记得点赞关注。",
        ]
        lines = [NarrationLine(t, estimate_duration(t)) for t in templates[:n]]
        return lines

    def from_text(self, text: str) -> List[NarrationLine]:
        """从纯文本构造脚本（按行/句切分）"""
        lines: List[NarrationLine] = []
        for raw in text.splitlines():
            t = raw.strip().strip("“”\"'")
            t = re.sub(r"^\s*[\d]+[.、)]\s*", "", t)  # 去掉行首编号
            if not t:
                continue
            # 一行里多句时按句号再切
            for seg in re.split(r"(?<=[。！？!?])", t):
                seg = seg.strip()
                if seg:
                    lines.append(NarrationLine(seg, estimate_duration(seg)))
        return lines

    def from_file(self, path: str) -> List[NarrationLine]:
        with open(path, "r", encoding="utf-8") as f:
            return self.from_text(f.read())

    @staticmethod
    def to_text(lines: List[NarrationLine]) -> str:
        return "\n".join(l.text for l in lines)

    @staticmethod
    def total_duration(lines: List[NarrationLine]) -> float:
        return round(sum(l.est_duration for l in lines), 2)

    # ------------------------------------------------------------------ llm
    def _query_llm(self, prompt: str) -> Optional[str]:
        """调用 OpenAI 兼容 API，失败返回 None"""
        try:
            import requests

            url = f"{self.api_base.rstrip('/')}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception:
            return None
