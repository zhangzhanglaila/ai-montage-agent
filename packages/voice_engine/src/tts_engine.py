"""语音合成（TTS）。

默认使用 edge-tts（免费、无需 API Key、无需本地模型）。
如需本地离线方案，可实现同签名的适配器替换。
"""

import asyncio
import os
from typing import Optional

# 常用中文音色
VOICES = {
    "zh_female": "zh-CN-XiaoxiaoNeural",
    "zh_male": "zh-CN-YunxiNeural",
    "zh_female_news": "zh-CN-XiaoyiNeural",
    "zh_male_news": "zh-CN-YunjianNeural",
    "zh_female_child": "zh-CN-XiaoshuangNeural",
}


class TtsEngine:
    """edge-tts 语音合成器"""

    def __init__(self, voice: Optional[str] = None, rate: str = "+0%", volume: str = "+0%"):
        self.voice = voice or VOICES["zh_female"]
        self.rate = rate
        self.volume = volume

    @staticmethod
    def available() -> bool:
        """edge-tts 是否可用"""
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, output_path: str) -> str:
        """把文本合成为音频文件。

        Args:
            text: 待合成文本
            output_path: 输出路径（建议 .mp3）

        Returns:
            输出音频路径

        Raises:
            ImportError: 未安装 edge-tts
            RuntimeError: 合成失败（如无网络）
        """
        if not self.available():
            raise ImportError("请安装 edge-tts: pip install edge-tts")
        if not text.strip():
            raise ValueError("旁白文本为空")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        try:
            asyncio.run(self._synthesize_async(text, output_path))
        except RuntimeError:
            # 已存在事件循环（如被异步框架调用）时改用新循环
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self._synthesize_async(text, output_path))
            finally:
                loop.close()

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("TTS 合成失败：未生成音频（请检查网络）")
        return output_path

    async def _synthesize_async(self, text: str, output_path: str) -> None:
        import edge_tts

        communicate = edge_tts.Communicate(
            text, self.voice, rate=self.rate, volume=self.volume
        )
        await communicate.save(output_path)
