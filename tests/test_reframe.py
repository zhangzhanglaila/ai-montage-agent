"""自动裁切 / 比例命名 回归测试（纯逻辑，不调用 ffmpeg）。

覆盖两个曾经的缺陷：
1. auto_reframe 把输出尺寸硬编码为 1080x1920，target_aspect=16/9 时裁出
   16:9 窗口后又强行缩放到 9:16，画面被非等比拉伸；
2. pipeline 把裁切产物后缀硬编码为 ``_9x16``，与真实比例不符。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from packages.video_enhancement.src.auto_reframe import (  # noqa: E402
    _output_size,
    compute_crop_window,
)


@pytest.mark.parametrize(
    "aspect,expect",
    [
        (9 / 16, (1080, 1920)),
        (16 / 9, (1920, 1080)),
        (1.0, (1080, 1080)),
        (4 / 5, (1080, 1350)),
        (3 / 4, (1080, 1440)),
    ],
)
def test_output_size_canonical(aspect, expect):
    """常见比例应映射到规范输出尺寸。"""
    cw, ch = compute_crop_window(854, 426, aspect)
    assert _output_size(cw, ch, aspect) == expect


def test_output_size_never_distorts():
    """输出比例与裁切窗口比例的偏差必须 < 1%（即不拉伸画面）。"""
    for aspect in (9 / 16, 16 / 9, 1.0, 4 / 5, 3 / 4):
        cw, ch = compute_crop_window(1920, 1080, aspect)
        w, h = _output_size(cw, ch, aspect)
        stretch = (w / h) / (cw / ch)
        assert 0.99 < stretch < 1.01, f"{aspect}: 拉伸比 {stretch:.4f} 超出容差"


def test_output_size_explicit_override():
    """显式同时给出宽高时原样采用（兼容旧调用方式）。"""
    got = _output_size(100, 200, 9 / 16, output_width=720, output_height=1280)
    assert got == (720, 1280)


@pytest.mark.parametrize(
    "aspect,expect",
    [
        (9 / 16, "_9x16"),
        (16 / 9, "_16x9"),
        (1.0, "_1x1"),
        (4 / 5, "_4x5"),
    ],
)
def test_ratio_suffix(aspect, expect):
    """裁切产物后缀应随实际比例变化（回归：曾硬编码 _9x16）。"""
    from pipeline import _ratio_suffix

    assert _ratio_suffix(aspect) == expect


def test_ratio_suffix_bad_input_safe():
    """非法/异常输入应安全兜底为默认竖屏后缀。"""
    from pipeline import _ratio_suffix

    assert _ratio_suffix("oops") == "_9x16"
    assert _ratio_suffix(-1) == "_9x16"
    assert _ratio_suffix(0) == "_9x16"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
