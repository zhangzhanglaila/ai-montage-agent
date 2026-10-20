"""BGM 节拍/能量/结构分析。"""

from typing import Any, Dict, List

import numpy as np

from packages.core_types.models import Beat, BeatAnalysis, MusicSegment


class BeatAnalyzer:
    """节拍分析 - 使用 librosa"""

    def analyze(self, audio_path: str) -> BeatAnalysis:
        """分析 BGM"""
        print(f"  分析 BGM: {audio_path}")

        try:
            import librosa

            # 加载音频
            y, sr = librosa.load(audio_path, sr=22050)
            duration = len(y) / sr

            # 检测节拍 + onset 强度
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
            beat_times = librosa.frames_to_time(beat_frames, sr=sr)

            # 如果没有检测到节拍，生成默认节拍
            if len(beat_times) == 0:
                print("  警告: 未检测到节拍，使用默认节拍")
                beat_interval = 0.5  # 120 BPM
                beat_times = np.arange(0, duration, beat_interval)

            # 计算 onset 强度（用于区分强拍/弱拍）
            onset_env = librosa.onset.onset_strength(y=y, sr=sr)
            onset_times = librosa.frames_to_time(np.arange(len(onset_env)), sr=sr)

            # 计算能量
            rms = librosa.feature.rms(y=y)[0]
            rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr)
            rms_normalized = (rms - rms.min()) / (rms.max() - rms.min() + 1e-8)

            # 为每个节拍分类强/弱拍
            # 强拍：onset 强度高于中位数 + 能量高于中位数
            onset_at_beats = []
            for bt in beat_times:
                idx = np.argmin(np.abs(onset_times - bt))
                onset_at_beats.append(onset_env[idx])

            onset_median = np.median(onset_at_beats) if onset_at_beats else 0
            energy_median = np.median(rms_normalized)

            # 找高潮段
            threshold = np.percentile(rms_normalized, 80)
            drops = []
            in_drop = False
            drop_start = 0

            for i, (t, e) in enumerate(zip(rms_times, rms_normalized)):
                if e > threshold and not in_drop:
                    in_drop = True
                    drop_start = t
                elif e <= threshold and in_drop:
                    in_drop = False
                    if t - drop_start > 1.0:
                        drops.append({"start": drop_start, "end": t})

            # 生成能量曲线
            energy_curve = [
                {"time": float(t), "energy": float(e)}
                for t, e in zip(rms_times[::100], rms_normalized[::100])
            ]

            # 创建节拍对象（区分强拍/弱拍）
            beats = []
            for i, t in enumerate(beat_times):
                onset_val = onset_at_beats[i] if i < len(onset_at_beats) else 0

                # 获取该时刻的能量
                e_idx = np.argmin(np.abs(rms_times - t))
                energy_val = rms_normalized[e_idx]

                # 分类：onset 强度高 + 能量高 = 强拍
                is_strong = (onset_val > onset_median * 1.2) or (energy_val > energy_median * 1.3)

                beat_type = "strong" if is_strong else "normal"
                strength = min(float(onset_val / (onset_median + 1e-8)), 2.0) / 2.0

                beats.append(Beat(
                    time=float(t),
                    strength=strength,
                    beat_type=beat_type,
                ))

            return BeatAnalysis(
                beats=beats,
                tempo=float(tempo),
                energy_curve=energy_curve,
                drops=drops,
                segments=[],
                duration=duration
            )

        except ImportError:
            # 如果没有 librosa，返回模拟数据
            print("  警告: librosa 未安装，使用模拟节拍")
            return self._mock_analysis()

    def _mock_analysis(self) -> BeatAnalysis:
        """模拟分析结果"""
        beats = [
            Beat(time=i * 0.5, strength=0.8, beat_type="normal")
            for i in range(20)
        ]
        return BeatAnalysis(
            beats=beats,
            tempo=120.0,
            energy_curve=[],
            drops=[],
            segments=[],
            duration=10.0
        )


