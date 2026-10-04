"""音效素材库。

优先使用 ``assets/sfx/<kind>.wav``（用户可放入自有免版权素材）；
缺失时用 ffmpeg 程序化合成，保证零外部依赖、零版权风险、可离线运行。

支持的音效类型：
    - impact  低频撞击（用于强拍）
    - whoosh  呼啸过渡（用于剪辑点）
    - riser   上升音（用于高潮前铺垫）
    - click   轻微点击（用于弱拍点缀）
"""

import subprocess
from pathlib import Path
from typing import List

# 支持的音效类型
KINDS = ("impact", "whoosh", "riser", "click")

# 程序化合成配方：kind -> ffmpeg lavfi 参数
_SYNTH_RECIPES = {
    "impact": [
        "-f", "lavfi", "-i", "sine=frequency=65:duration=0.55",
        "-af", "afade=t=out:st=0.03:d=0.52,volume=1.8",
    ],
    "whoosh": [
        "-f", "lavfi", "-i", "anoisesrc=d=0.55:c=pink:a=0.6",
        "-af", "highpass=f=500,lowpass=f=4500,"
               "afade=t=in:d=0.12,afade=t=out:st=0.3:d=0.25",
    ],
    "riser": [
        "-f", "lavfi", "-i", "aevalsrc=sin(2*PI*(300+1400*t)*t):d=0.9:s=44100",
        "-af", "afade=t=in:d=0.15,volume=0.8",
    ],
    "click": [
        "-f", "lavfi", "-i", "sine=frequency=2200:duration=0.06",
        "-af", "afade=t=out:st=0.012:d=0.048,volume=0.5",
    ],
}


class SfxLibrary:
    """音效素材获取器。"""

    def __init__(self, cache_dir: str = "cache/sfx", assets_dir: str = "assets/sfx"):
        self.cache_dir = Path(cache_dir)
        self.assets_dir = Path(assets_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def available(self) -> List[str]:
        """返回当前可用的音效类型（自带素材 ∪ 可合成类型）。"""
        kinds = set(KINDS)
        if self.assets_dir.exists():
            for f in self.assets_dir.glob("*.wav"):
                if f.stem in kinds:
                    kinds.add(f.stem)
        return sorted(kinds)

    def get(self, kind: str) -> str:
        """获取指定音效文件路径；不存在则合成并缓存。

        Args:
            kind: 音效类型，见 ``KINDS``

        Returns:
            音效 wav 文件路径
        """
        if kind not in KINDS:
            raise ValueError(f"未知音效类型: {kind}（可选 {KINDS}）")

        # 1) 优先使用用户自备素材
        user_asset = self.assets_dir / f"{kind}.wav"
        if user_asset.exists():
            return str(user_asset)

        # 2) 缓存
        cached = self.cache_dir / f"{kind}.wav"
        if cached.exists():
            return str(cached)

        # 3) 合成
        self._synthesize(kind, cached)
        return str(cached)

    def _synthesize(self, kind: str, out_path: Path) -> None:
        recipe = _SYNTH_RECIPES.get(kind)
        if recipe is None:
            raise ValueError(f"没有 {kind} 的合成配方")
        cmd = ["ffmpeg", "-y", *recipe,
               "-ar", "44100", "-ac", "2", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError(
                f"合成音效失败: {kind}\n{proc.stderr.decode('utf-8', errors='ignore')[-500:]}"
            )
