"""镜头 embedding 编码器

两种后端，接口一致（都是 L2 归一化向量，可直接点积算余弦相似度）：

1. ``ClipEmbedder`` —— transformers 的 CLIP（真·跨模态语义，「a smiling face」
   能匹配到微笑画面）。需要 torch + transformers + 模型权重（本地缓存或能联网）。
2. ``HeuristicEmbedder`` —— 纯 numpy/Pillow 的视觉特征向量（亮度/对比度/饱和度/
   色温/边缘密度/颜色分布）。**离线可用、零下载**，文本侧靠中英关键词表映射到同一
   特征空间，适合做「亮/暗、暖/冷、鲜艳、红色/蓝色、特写、模糊」这类检索。

``get_embedder("auto")`` 优先 CLIP，加载失败则自动降级到启发式，保证流程不中断。
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

import numpy as np

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


# ---------------------------------------------------------------- 基础接口
class BaseEmbedder:
    """embedding 后端基类"""

    name: str = "base"
    dim: int = 32

    @property
    def supports_text(self) -> bool:
        """是否支持文本检索（跨模态）"""
        return False

    def encode_images(self, image_paths: Sequence[str]) -> np.ndarray:
        raise NotImplementedError

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError

    @staticmethod
    def _l2(mat: np.ndarray) -> np.ndarray:
        mat = np.asarray(mat, dtype=np.float32)
        norm = np.linalg.norm(mat, axis=-1, keepdims=True)
        norm[norm == 0] = 1.0
        return mat / norm


# ------------------------------------------------------- 启发式（离线兜底）
# 特征布局（共 30 维）：
#   [0] 亮度  [1] 对比度  [2] 饱和度  [3] 色温  [4] 边缘密度
#   [5:29] R/G/B 各 8 bin 颜色直方图
#   [29] 留空（对齐到偶数）
_NUM_COLOR_BINS = 8
_HIST_START = 5
_HIST_END = _HIST_START + 3 * _NUM_COLOR_BINS  # 29
_HEUR_DIM = _HIST_END + 1                      # 30

# 关键词 -> 要拉高/拉低的特征维度
_KEYWORD_RULES = [
    # (词表, 维度, 目标值)
    (["亮", "明亮", "高光", "bright", "brightly", "luminous", "light"], 0, 0.95),
    (["暗", "昏暗", "黑暗", "夜景", "dark", "darkness", "dim", "night"], 0, 0.05),
    (["高对比", "对比强", "high contrast", "contrasty"], 1, 0.9),
    (["低对比", "灰蒙蒙", "flat", "low contrast"], 1, 0.1),
    (["鲜艳", "浓烈", "高饱和", "vivid", "colorful", "saturated", "colourful"], 2, 0.9),
    (["素雅", "低饱和", "淡", "desaturated", "pale", "muted"], 2, 0.12),
    (["暖", "暖色", "夕阳", "warm", "orange", "golden", "sunset"], 3, 0.85),
    (["冷", "冷色", "蓝调", "cool", "cold", "blue tone"], 3, 0.15),
    (["锐", "清晰", "细节多", "sharp", "detailed", "crisp"], 4, 0.85),
    (["模糊", "柔和", "虚化", "blur", "blurry", "soft", "bokeh"], 4, 0.1),
]


def _hist_colors(rgb: np.ndarray, bins: int = _NUM_COLOR_BINS) -> np.ndarray:
    """三通道各自的归一化直方图（rgb: HxWx3, 值域 0~1）"""
    out = []
    for c in range(3):
        h, _ = np.histogram(rgb[:, :, c], bins=bins, range=(0.0, 1.0))
        h = h.astype(np.float32)
        s = h.sum()
        out.append(h / s if s > 0 else h)
    return np.concatenate(out)


class HeuristicEmbedder(BaseEmbedder):
    """离线视觉特征编码器（numpy + Pillow，不依赖任何模型）"""

    name = "heuristic-v1"
    dim = _HEUR_DIM

    def __init__(self, size: int = 64):
        self.size = size

    def _image_features(self, path: str) -> Optional[np.ndarray]:
        if Image is None:
            return None
        try:
            with Image.open(path) as im:
                im = im.convert("RGB").resize((self.size, self.size))
                arr = np.asarray(im, dtype=np.float32) / 255.0
        except Exception:
            return None

        gray = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        brightness = float(gray.mean())
        contrast = float(min(1.0, gray.std() / 0.35))
        cmax, cmin = arr.max(axis=2), arr.min(axis=2)
        saturation = float(np.mean(np.where(cmax > 1e-6, (cmax - cmin) / np.maximum(cmax, 1e-6), 0.0)))
        warmth = float((arr[:, :, 0].mean() - arr[:, :, 2].mean() + 1.0) / 2.0)
        # 梯度幅值近似边缘密度
        gx = np.abs(np.diff(gray, axis=1)).mean() if gray.shape[1] > 1 else 0.0
        gy = np.abs(np.diff(gray, axis=0)).mean() if gray.shape[0] > 1 else 0.0
        edge = float(min(1.0, (gx + gy) * 4.0))

        vec = np.zeros(self.dim, dtype=np.float32)
        vec[0:5] = [brightness, contrast, saturation, warmth, edge]
        vec[_HIST_START:_HIST_END] = _hist_colors(arr)
        return vec

    def encode_images(self, image_paths: Sequence[str]) -> np.ndarray:
        vecs = []
        for p in image_paths:
            v = self._image_features(p)
            if v is None:
                v = np.full(self.dim, 0.5, dtype=np.float32)
            vecs.append(v)
        raw = np.stack(vecs) if vecs else np.zeros((0, self.dim), dtype=np.float32)
        return self._l2(raw - 0.5)   # 以 0.5 为中性基线居中

    @property
    def supports_text(self) -> bool:
        return True

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        raw = np.full((len(texts), self.dim), 0.5, dtype=np.float32)
        for i, t in enumerate(texts):
            tl = (t or "").lower()
            for words, dim_idx, target in _KEYWORD_RULES:
                if any(w in tl for w in words):
                    raw[i, dim_idx] = target
            # 颜色词 -> 拉高对应通道的亮部 bin
            color_map = {
                "红": 0, "red": 0, "橙": 0, "orange": 0,
                "绿": 1, "green": 1, "青": 1, "cyan": 1,
                "蓝": 2, "blue": 2,
            }
            color_boost = {
                "黄": (0, 1), "yellow": (0, 1),
                "紫": (0, 2), "purple": (0, 2), "magenta": (0, 2),
            }
            for w, (c0, c1) in color_boost.items():
                if w in tl:
                    raw[i, _HIST_START + c0 * _NUM_COLOR_BINS + 5] = 0.95
                    raw[i, _HIST_START + c1 * _NUM_COLOR_BINS + 5] = 0.95
            for w, c in color_map.items():
                if w in tl:
                    raw[i, _HIST_START + c * _NUM_COLOR_BINS + 5] = 0.95
            if any(w in tl for w in ["白", "white"]):
                # 白：三通道亮部且低饱和
                raw[i, 2] = 0.05
                raw[i, 0] = 0.9
                for c in range(3):
                    raw[i, _HIST_START + c * _NUM_COLOR_BINS + 7] = 0.95
            if any(w in tl for w in ["黑", "black"]):
                raw[i, 0] = 0.05
                for c in range(3):
                    raw[i, _HIST_START + c * _NUM_COLOR_BINS + 0] = 0.95
        return self._l2(raw - 0.5)


# ------------------------------------------------------------- CLIP（真语义）
class ClipEmbedder(BaseEmbedder):
    """transformers CLIP 后端（跨模态：文本可直接检索画面）"""

    def __init__(self, model_id: Optional[str] = None, device: Optional[str] = None):
        self.model_id = model_id or os.environ.get(
            "MONTAGE_CLIP_MODEL", "openai/clip-vit-base-patch32")
        self.device = device
        self._model = None
        self._processor = None
        self._failed = False
        self.dim = 512

    # -- 状态 --
    @staticmethod
    def library_available() -> bool:
        """轻量探测依赖是否存在（用 find_spec，避免 import 重库的开销）。"""
        import importlib.util
        return (importlib.util.find_spec("transformers") is not None
                and importlib.util.find_spec("torch") is not None)

    def load(self) -> bool:
        """惰性加载模型；失败返回 False（不抛异常）"""
        if self._model is not None:
            return True
        if self._failed:
            return False
        # 无本地缓存且未显式允许下载时直接跳过，避免离线环境卡在 HF 连接超时上
        if not self._download_ok():
            print(f"  [索引] 未找到 CLIP 本地缓存（{self.model_id}），跳过加载；"
                  f"如需联网下载请设 MONTAGE_CLIP_ALLOW_DOWNLOAD=1")
            self._failed = True
            return False
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor

            self._model = CLIPModel.from_pretrained(self.model_id)
            self._processor = CLIPProcessor.from_pretrained(self.model_id)
            if self.device:
                self._model = self._model.to(self.device)
            self._model.eval()
            self.dim = int(getattr(self._model.config, "projection_dim", 512))
            return True
        except Exception as e:
            print(f"  [索引] CLIP 加载失败（{type(e).__name__}: {e}），将使用离线兜底编码器")
            self._failed = True
            return False

    def _download_ok(self) -> bool:
        """判断是否值得尝试加载：有本地缓存，或用户显式允许下载。"""
        if os.environ.get("MONTAGE_CLIP_ALLOW_DOWNLOAD") == "1":
            return True
        try:
            from pathlib import Path as _P
            cache_root = os.environ.get("HF_HOME")
            if not cache_root:
                cache_root = str(_P.home() / ".cache" / "huggingface")
            hub = _P(cache_root) / "hub"
            slug = "models--" + self.model_id.replace("/", "--")
            if (hub / slug).exists():
                return True
            # 也支持用户直接把模型放本地目录
            return _P(self.model_id).exists()
        except Exception:
            return False

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def supports_text(self) -> bool:
        return True

    # -- 编码 --
    def encode_images(self, image_paths: Sequence[str]) -> np.ndarray:
        if not self.load():
            raise RuntimeError("CLIP 模型不可用")
        import torch
        from PIL import Image as _Image

        imgs = []
        for p in image_paths:
            try:
                imgs.append(_Image.open(p).convert("RGB"))
            except Exception:
                imgs.append(_Image.new("RGB", (224, 224), (0, 0, 0)))
        inputs = self._processor(images=imgs, return_tensors="pt")
        if self.device:
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            feats = self._model.get_image_features(**inputs)
        return self._l2(feats.cpu().numpy())

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        if not self.load():
            raise RuntimeError("CLIP 模型不可用")
        import torch

        inputs = self._processor(
            text=list(texts), return_tensors="pt", padding=True, truncation=True)
        if self.device:
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            feats = self._model.get_text_features(**inputs)
        return self._l2(feats.cpu().numpy())


# ------------------------------------------------------------------ 工厂
def get_embedder(prefer: str = "auto", verbose: bool = True) -> BaseEmbedder:
    """获取可用的 embedding 后端。

    Args:
        prefer: ``auto``（优先 CLIP，失败降级）/ ``clip``（强制 CLIP）/
                ``heuristic``（强制离线特征）
    """
    prefer = (prefer or "auto").lower()
    if prefer == "heuristic":
        return HeuristicEmbedder()
    if prefer == "clip":
        emb = ClipEmbedder()
        if not emb.load():
            raise RuntimeError("CLIP 不可用（缺少 transformers/torch 或模型权重）")
        return emb

    # auto
    if ClipEmbedder.library_available():
        emb = ClipEmbedder()
        if emb.load():
            if verbose:
                print(f"  [索引] 使用 CLIP 后端: {emb.model_id}")
            return emb
    if verbose:
        print("  [索引] 使用离线启发式编码器（无 CLIP 权重）")
    return HeuristicEmbedder()


def embedder_status() -> dict:
    """返回各后端可用性，便于 CLI/WebUI 展示。"""
    clip_lib = ClipEmbedder.library_available()
    return {
        "clip_library": clip_lib,
        "clip_model": os.environ.get("MONTAGE_CLIP_MODEL", "openai/clip-vit-base-patch32"),
        "heuristic_dim": HeuristicEmbedder.dim,
        "prefer_env": os.environ.get("MONTAGE_EMBEDDER", "auto"),
    }
