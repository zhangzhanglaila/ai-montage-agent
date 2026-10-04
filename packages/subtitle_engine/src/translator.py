"""字幕翻译 —— 生成双语字幕

后端优先级（``backend="auto"``）：
    1. LLM（OpenAI 兼容，复用 ai_director / script_writer 的 OPENAI_API_BASE 配置）
    2. deep-translator 的 GoogleTranslator（需 ``pip install deep-translator``）
    3. 离线直通（保留原文，保证流程不中断）

所有后端都**保证返回行数与输入一致**，这样才能把译文按顺序回填到对应时间码，
不会出现错位串行。
"""

from __future__ import annotations

import os
import re
from typing import Callable, List, Optional, Sequence

from .caption_burner import CaptionSegment


# 语言代码 -> 中文名（写进 LLM 提示词，比裸语言码更稳）
LANG_NAMES = {
    "zh": "中文", "zh-cn": "简体中文", "zh-tw": "繁体中文",
    "en": "英文", "ja": "日文", "ko": "韩文",
    "es": "西班牙文", "fr": "法文", "de": "德文",
    "ru": "俄文", "pt": "葡萄牙文", "it": "意大利文", "ar": "阿拉伯文",
}

# 每批送去翻译的最大行数（太大易被截断，太小请求次数多）
DEFAULT_BATCH = 20


def lang_name(code: str) -> str:
    return LANG_NAMES.get((code or "").lower(), code or "")


class Translator:
    """字幕翻译器（LLM / deep-translator / 直通）"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
        backend: str = "auto",
        batch_size: int = DEFAULT_BATCH,
    ):
        self.api_base = api_base or os.environ.get("OPENAI_API_BASE", "http://localhost:11434/v1")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "sk-placeholder")
        self.model = model or os.environ.get("LLM_MODEL", "qwen2.5:7b")
        self.timeout = timeout
        self.backend = backend
        self.batch_size = max(1, batch_size)

    # ------------------------------------------------------------- public
    def translate(
        self,
        texts: Sequence[str],
        target: str = "en",
        source: str = "auto",
    ) -> List[str]:
        """把一批文本翻译成 target；返回列表长度恒等于输入。"""
        texts = [t or "" for t in texts]
        if not texts:
            return []

        if self.backend in ("auto", "llm"):
            out = self._llm(texts, target, source)
            if out is not None:
                return out
            if self.backend == "llm":
                raise RuntimeError("LLM 翻译后端不可用（检查 OPENAI_API_BASE/KEY/LLM_MODEL）")

        if self.backend in ("auto", "deep"):
            out = self._deep(texts, target, source)
            if out is not None:
                return out
            if self.backend == "deep":
                raise RuntimeError("deep-translator 不可用（pip install deep-translator）")

        # 直通兜底：保留原文，保证后续排版不崩
        print("  [翻译] 无可用翻译后端，保留原文（直通）")
        return list(texts)

    # --------------------------------------------------------------- LLM
    def _llm(self, texts: Sequence[str], target: str, source: str) -> Optional[List[str]]:
        try:
            import requests
        except ImportError:
            return None

        url = f"{self.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        tname = lang_name(target)
        sname = lang_name(source) if source and source != "auto" else "原文"
        results: List[str] = []

        for i in range(0, len(texts), self.batch_size):
            chunk = list(texts[i:i + self.batch_size])
            numbered = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(chunk))
            prompt = (
                f"把下面 {len(chunk)} 行{sname}字幕逐行翻译成{tname}。\n"
                f"要求：\n"
                f"1. 严格保持行数与顺序，一行译文对应一行原文；\n"
                f"2. 每行以「序号. 」开头，序号必须与原文一致；\n"
                f"3. 只输出译文，不要解释、不要空行。\n\n"
                f"{numbered}"
            )
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            }
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                print(f"  [翻译] LLM 调用失败: {e}")
                return None

            parsed = _parse_numbered(content, len(chunk))
            if parsed is None:
                print("  [翻译] LLM 返回行数与原文不一致，放弃该后端")
                return None
            results.extend(parsed)
        return results

    # -------------------------------------------------------------- deep
    def _deep(self, texts: Sequence[str], target: str, source: str) -> Optional[List[str]]:
        try:
            from deep_translator import GoogleTranslator
        except ImportError:
            return None

        src = "auto" if not source or source == "auto" else source
        try:
            gt = GoogleTranslator(source=src, target=target)
            out: List[str] = []
            for t in texts:
                if not t.strip():
                    out.append(t)
                    continue
                # Google 单次有长度限制，超长做硬截断保护
                out.append(gt.translate(t[:4900]))
            return out
        except Exception as e:
            print(f"  [翻译] deep-translator 调用失败: {e}")
            return None


def _parse_numbered(content: str, expected: int) -> Optional[List[str]]:
    """解析「1. xxx」形式的编号译文；行数不符返回 None。"""
    lines: List[str] = []
    for raw in (content or "").splitlines():
        t = raw.strip()
        if not t:
            continue
        t = re.sub(r"^\s*[\d]+\s*[.、):：]\s*", "", t)
        lines.append(t)
    if len(lines) != expected:
        return None
    return lines


# --------------------------------------------------------------- segments
def translate_segments(
    segments: Sequence[CaptionSegment],
    target: str = "en",
    source: str = "auto",
    backend: str = "auto",
    translator: Optional[Callable[[Sequence[str]], Sequence[str]]] = None,
    keep_source: bool = True,
) -> List[CaptionSegment]:
    """翻译字幕段，保留原时间码（译文写入 ``text2``）。

    Args:
        segments: 原始字幕段
        target: 目标语言代码
        source: 源语言代码（auto 为自动）
        backend: 翻译后端（auto/llm/deep）
        translator: 自定义翻译函数（取一批文本，返回等长译文列表）；
                    传入时忽略 backend，便于离线测试
        keep_source: True 时原文仍是主行，译文进 text2；False 时译文替换主行

    Returns:
        新的 CaptionSegment 列表（不修改传入对象）
    """
    segs = list(segments)
    if not segs:
        return []

    texts = [s.text for s in segs]
    if translator is not None:
        translated = list(translator(texts))
        if len(translated) != len(texts):
            raise ValueError("自定义 translator 返回行数与输入不一致")
    else:
        translated = Translator(backend=backend).translate(texts, target=target, source=source)

    out: List[CaptionSegment] = []
    for src_seg, tr in zip(segs, translated):
        tr = (tr or "").strip()
        if keep_source:
            out.append(CaptionSegment(
                start=src_seg.start, end=src_seg.end,
                text=src_seg.text, words=src_seg.words, text2=tr,
            ))
        else:
            out.append(CaptionSegment(
                start=src_seg.start, end=src_seg.end,
                text=tr or src_seg.text, words=src_seg.words,
            ))
    return out
