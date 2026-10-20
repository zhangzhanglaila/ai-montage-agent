"""
默认 BGM 生成器 - 用 numpy 合成基础节拍
当 BGM 搜索失败时自动回退
支持 3 种风格：dynamic(120BPM), calm(90BPM), intense(140BPM)
"""

import numpy as np
import wave
import struct
from pathlib import Path


def _kick(sr: int = 44100) -> np.ndarray:
    """合成 Kick 鼓（低频下扫）"""
    duration = 0.15
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    freq = 150 - (150 - 40) * (t / duration)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    amp = 0.8 * np.exp(-t * 20)
    return amp * np.sin(phase)


def _snare(sr: int = 44100) -> np.ndarray:
    """合成 Snare（中频 + 噪声）"""
    duration = 0.12
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    sine = 0.4 * np.sin(2 * np.pi * 200 * t) * np.exp(-t * 30)
    noise = 0.6 * np.random.uniform(-1, 1, len(t)) * np.exp(-t * 15)
    return sine + noise


def _hihat(sr: int = 44100) -> np.ndarray:
    """合成 Hi-Hat（高频噪声）"""
    duration = 0.05
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return 0.3 * np.random.uniform(-1, 1, len(t)) * np.exp(-t * 60)


def _clap(sr: int = 44100) -> np.ndarray:
    """合成 Clap（多次噪声叠加）"""
    duration = 0.1
    n = int(sr * duration)
    samples = np.zeros(n)
    for offset_s in [0, 0.01, 0.02]:
        start = int(offset_s * sr)
        length = min(int(0.04 * sr), n - start)
        if length > 0:
            t = np.linspace(0, length / sr, length, endpoint=False)
            samples[start:start+length] += 0.25 * np.random.uniform(-1, 1, length) * np.exp(-t * 40)
    return samples


def _bass_note(freq: float, duration: float, sr: int = 44100) -> np.ndarray:
    """合成低音"""
    t = np.linspace(0, duration * 0.8, int(sr * duration * 0.8), endpoint=False)
    amp = 0.25 * np.exp(-t * 5)
    return amp * np.sin(2 * np.pi * freq * t)


def _write_wav(samples: np.ndarray, path: str, sr: int = 44100) -> str:
    """写入 WAV 文件"""
    # 归一化
    max_val = np.max(np.abs(samples))
    if max_val > 0:
        samples = samples / max_val * 0.85

    # 转为 16-bit
    samples_int = np.clip(samples * 32767, -32767, 32767).astype(np.int16)

    with wave.open(path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples_int.tobytes())

    return path


def generate_default_bgm(
    style: str = "dynamic",
    duration: float = 60.0,
    output_path: str = None,
    sr: int = 44100,
) -> str:
    """
    生成默认 BGM

    Args:
        style: 风格 (dynamic/calm/intense)
        duration: 时长（秒）
        output_path: 输出路径
        sr: 采样率

    Returns:
        输出文件路径
    """
    if output_path is None:
        cache_dir = Path("var/cache/bgm")
        cache_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(cache_dir / f"default_{style}.wav")

    if Path(output_path).exists():
        return output_path

    # 风格参数
    style_params = {
        "dynamic": {"bpm": 120, "kick": [1,0,0,0,1,0,0,0], "snare": [0,0,1,0,0,0,1,0],
                    "hihat": [1,0,1,0,1,0,1,0], "clap": [0,0,1,0,0,0,1,0], "bass": 55},
        "calm":    {"bpm": 90,  "kick": [1,0,0,0,0,0,0,0], "snare": [0,0,0,0,1,0,0,0],
                    "hihat": [0,0,1,0,0,0,1,0], "clap": [0,0,0,0,0,0,0,0], "bass": 45},
        "intense": {"bpm": 140, "kick": [1,0,1,0,1,0,1,0], "snare": [0,0,1,0,0,0,1,0],
                    "hihat": [1,1,1,1,1,1,1,1], "clap": [0,0,1,0,0,0,1,0], "bass": 65},
    }

    params = style_params.get(style, style_params["dynamic"])
    bpm = params["bpm"]
    sixteenth_dur = 60.0 / bpm / 4

    # 预合成打击乐
    kick = _kick(sr)
    snare = _snare(sr)
    hihat = _hihat(sr)
    clap = _clap(sr)
    bass = _bass_note(params["bass"], sixteenth_dur, sr)

    # 总采样数
    total_samples = int(sr * duration)
    output = np.zeros(total_samples, dtype=np.float64)

    # 计算每步的采样偏移
    step_samples = int(sixteenth_dur * sr)
    total_steps = int(duration / sixteenth_dur)

    for step in range(total_steps):
        pos = step * step_samples
        if pos >= total_samples:
            break

        idx = step % 8

        # Kick
        if params["kick"][idx]:
            end = min(pos + len(kick), total_samples)
            output[pos:end] += kick[:end-pos]

        # Snare
        if params["snare"][idx]:
            end = min(pos + len(snare), total_samples)
            output[pos:end] += snare[:end-pos]

        # Hi-Hat
        if params["hihat"][idx]:
            end = min(pos + len(hihat), total_samples)
            output[pos:end] += hihat[:end-pos]

        # Clap
        if params["clap"][idx]:
            end = min(pos + len(clap), total_samples)
            output[pos:end] += clap[:end-pos]

        # Bass（每拍第一下）
        if idx == 0 or (style == "intense" and idx == 4):
            end = min(pos + len(bass), total_samples)
            output[pos:end] += bass[:end-pos]

    print(f"  生成默认 BGM ({style}, {bpm}BPM, {duration}s): {output_path}")
    _write_wav(output, output_path, sr)
    return output_path


def get_default_bgm_path(style: str = "dynamic") -> str:
    """获取默认 BGM 路径（如不存在则生成）"""
    cache_dir = Path("var/cache/bgm")
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = str(cache_dir / f"default_{style}.wav")

    if not Path(path).exists():
        generate_default_bgm(style=style, output_path=path)

    return path


if __name__ == "__main__":
    for style in ["dynamic", "calm", "intense"]:
        path = generate_default_bgm(style=style, duration=30.0)
        print(f"  已生成: {path}")
