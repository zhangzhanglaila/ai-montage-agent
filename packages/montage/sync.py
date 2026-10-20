"""卡点同步：镜头时长量化到节拍、选择转场。"""

import random
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from packages.core_types.models import Beat, Shot, TimelineEntry


# 卡点转场的最长时长（秒）。
# xfade 会吞掉转场时长，导致后面的切点整体提前；这里把转场压到 0.08s，
# 并用"前一镜头尾部冻结 0.08s"来补偿，使成片总长仍等于时间线标称长度。
# 0.08s @30fps 约 2~3 帧，肉眼只看到一次很短的柔和交叉，不会出现黑场。
MAX_BEAT_XFADE = 0.08

# 这些转场会制造黑场/闪黑（实测 circlecrop/rectcrop 中间帧 mean≈0，
# flash/blur/motion_blur/shake 原先映射到 fadeblack），统一降级为 fade。
BLACK_UNSAFE_TRANSITIONS = {
    "fadeblack", "flash", "blur", "motion_blur", "shake",
    "circlecrop", "rectcrop", "zoom",
}




class BeatSyncEngine:
    """卡点同步引擎 - 镜头时长严格量化到整拍，结束点对齐节拍边界"""

    # 转场类型映射
    TRANSITION_TYPES = ["cut", "fade", "dissolve", "wipe", "flash", "zoom", "blur"]

    @staticmethod
    def _shot_seconds(shot) -> float:
        """镜头文件**实际可用画面**的秒数；取不到返回 None（表示放弃约束）"""
        try:
            d = float(getattr(shot, "duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if d > 0:
            return d
        path = getattr(shot, "file_path", None)
        if path and Path(path).exists():
            cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
            out = subprocess.run(cmd, capture_output=True, text=True).stdout
            try:
                v = float((out or "").strip())
                return v if v > 0 else None
            except ValueError:
                return None
        return None

    def sync(
        self,
        shots: List[Shot],
        beats: List[Beat],
        style: str = "dynamic",
        transition_pattern: dict = None,
    ) -> List[TimelineEntry]:
        """
        将镜头与节拍同步
        核心逻辑：
        - 每个镜头时长 = N 拍（N 由高光分数和拍类型决定）
        - 结束点严格对齐节拍边界
        - 速度因子范围 0.8~1.2（不破坏节奏感）
        """
        if not shots or not beats:
            return []

        # 计算节拍间隔（BPM 的倒数）
        if len(beats) >= 2:
            # 用中位数间隔，避免异常值
            intervals = [beats[i+1].time - beats[i].time for i in range(len(beats)-1)]
            beat_interval = sorted(intervals)[len(intervals) // 2]
        else:
            beat_interval = 0.5

        # 风格参数：拍数范围 + 速度范围
        style_cfg = self._get_style_config(style)

        timeline = []
        current_beat_idx = 0

        for shot in shots:
            if current_beat_idx >= len(beats):
                break

            # 当前节拍时间点
            beat_time = beats[current_beat_idx].time
            beat = beats[current_beat_idx]

            # 根据高光分数 + 拍类型决定拍数（N 拍）
            beat_count = self._calc_beat_count(
                shot.highlight_score, beat.beat_type, style_cfg
            )

            # 速度因子：基于高光分数微调（范围 0.8~1.2）
            speed_factor = self._calc_speed(
                shot.highlight_score, beat.beat_type, style_cfg
            )

            # 画面长度约束（关键）：
            # 优先"降速填满"，而不是"克隆末帧凑时长"。镜头不够长时，把速度降到
            # min_speed 以上来撑满整拍；连 min_speed 都撑不满才少占一拍/跳过。
            # 这样卡点处不会冻结，也不会为了凑时长把动作放慢到不可接受。
            avail = self._shot_seconds(shot)
            min_speed = float(style_cfg.get("speed_range", (0.8, 1.2))[0])
            if avail is not None:
                fit_beats = int(avail / max(0.1, min_speed) / beat_interval + 1e-6)
                if fit_beats < 1:
                    continue
                beat_count = min(beat_count, fit_beats)

            # 时长优先用"实际节拍边界"，而不是 N × 中位拍间隔。
            # 真实 BGM 的拍间隔并不完全恒定（实测 0.488~0.53s），用中位数
            # 会让切点逐段积累偏差；用 beats[end_idx] 可保证切点严格落在拍上。
            end_idx = min(current_beat_idx + beat_count, len(beats) - 1)
            duration = float(beats[end_idx].time) - float(beat_time)
            if duration <= 0:
                duration = beat_count * beat_interval

            # 实际拍边界可能比中位拍更长：按"最低可接受速度"再校验一次
            if avail is not None:
                while (duration > 0 and avail / duration < min_speed - 1e-4
                       and beat_count > 1):
                    beat_count -= 1
                    end_idx = min(current_beat_idx + beat_count, len(beats) - 1)
                    duration = float(beats[end_idx].time) - float(beat_time)
                if duration <= 0 or avail / duration < min_speed - 1e-4:
                    continue
                # 降速填满画面（不冻结），速度不超过当前风格值
                max_speed = avail / duration
                speed_factor = max(min_speed, min(speed_factor, max_speed))

            # 转场：支持模板系统
            transition_type = self._pick_transition(beat, shot, style, transition_pattern)
            # 黑场类转场统一降级为 fade（渲染层也会兜底映射）
            if transition_type in BLACK_UNSAFE_TRANSITIONS:
                transition_type = "fade"
            if transition_pattern and transition_type != "cut":
                # 用模板的时长比例
                dur_ratio = transition_pattern.get(
                    "strong_duration_ratio" if beat.beat_type == "strong" else "weak_duration_ratio",
                    0.5
                )
                transition_duration = min(
                    beat_interval * dur_ratio, MAX_BEAT_XFADE
                )
            else:
                transition_duration = (
                    min(beat_interval * 0.5, MAX_BEAT_XFADE)
                    if transition_type != "cut" else 0.0
                )

            # 创建时间线条目
            entry = TimelineEntry(
                shot_id=shot.shot_id,
                shot_path=shot.file_path or "",
                start_time=beat_time,
                end_time=beat_time + duration,
                duration=duration,
                beat_time=beat_time,
                speed_factor=speed_factor,
                transition_type=transition_type,
                transition_duration=transition_duration
            )

            timeline.append(entry)

            # 移动到下一个节拍位置（跳过已用的拍数）
            current_beat_idx += beat_count

        # 按时间排序
        timeline.sort(key=lambda x: x.start_time)

        return timeline

    def _get_style_config(self, style: str) -> Dict[str, Any]:
        """获取风格配置"""
        configs = {
            "dynamic": {
                "strong_beats": (1, 2),      # 强拍镜头：1-2 拍
                "normal_beats": (2, 3),       # 普通镜头：2-3 拍
                "weak_beats": (3, 4),         # 弱拍镜头：3-4 拍
                "speed_range": (0.85, 1.15),  # 速度范围
                "strong_speed": 0.9,          # 强拍速度（略慢，强调）
                "weak_speed": 1.1,            # 弱拍速度（略快，紧凑）
            },
            "calm": {
                "strong_beats": (2, 3),
                "normal_beats": (3, 4),
                "weak_beats": (4, 6),
                "speed_range": (0.9, 1.1),
                "strong_speed": 0.95,
                "weak_speed": 1.05,
            },
            "intense": {
                "strong_beats": (1, 1),
                "normal_beats": (1, 2),
                "weak_beats": (2, 3),
                "speed_range": (0.8, 1.2),
                "strong_speed": 0.85,
                "weak_speed": 1.15,
            },
        }
        return configs.get(style, configs["dynamic"])

    def _calc_beat_count(
        self, highlight_score: float, beat_type: str, cfg: Dict
    ) -> int:
        """
        根据高光分数和拍类型决定镜头占几拍
        高光分数越高 + 拍越强 -> 拍数越少（镜头越短，节奏越快）
        """
        if beat_type == "strong":
            # 强拍：高分镜头用最少拍数
            if highlight_score > 0.7:
                beat_range = cfg["strong_beats"]
            else:
                beat_range = (cfg["strong_beats"][0] + 1, cfg["strong_beats"][1] + 1)
        elif beat_type == "weak":
            # 弱拍：用较多拍数
            beat_range = cfg["weak_beats"]
        else:
            # 普通拍
            beat_range = cfg["normal_beats"]

        # 在范围内随机选（增加变化感）
        min_b, max_b = beat_range
        if min_b == max_b:
            return min_b
        # 高分偏短，低分偏长
        if highlight_score > 0.6:
            return min_b
        elif highlight_score > 0.3:
            return (min_b + max_b) // 2
        else:
            return max_b

    def _calc_speed(
        self, highlight_score: float, beat_type: str, cfg: Dict
    ) -> float:
        """
        计算速度因子（范围 0.8~1.2）
        强拍略慢（强调），弱拍略快（紧凑）
        """
        base_speed = cfg["strong_speed"] if beat_type == "strong" else cfg["weak_speed"]

        # 根据高光分数微调
        if highlight_score > 0.7:
            # 高光镜头：强拍更慢，弱拍更快
            adjustment = 0.05 if beat_type == "strong" else -0.05
        else:
            adjustment = 0.0

        speed = base_speed + adjustment

        # 限制范围
        min_speed, max_speed = cfg["speed_range"]
        return max(min_speed, min(speed, max_speed))

    def _pick_transition(self, beat: Beat, shot: Shot, style: str,
                         transition_pattern: dict = None) -> str:
        """根据拍类型、风格和转场模板选择转场效果"""
        if transition_pattern:
            from packages.montage_engine.src.transition_patterns import pick_transition
            motion = getattr(shot, 'motion_score', 0.0) or 0.0
            trans_type, _ = pick_transition(transition_pattern, beat.beat_type, motion)
            return trans_type

        # 旧逻辑：根据风格随机选
        if beat.beat_type == "strong":
            if style == "intense":
                return random.choice(["flash", "zoom", "cut"])
            elif style == "calm":
                return random.choice(["fade", "dissolve"])
            else:
                return random.choice(["cut", "fade", "flash"])

        if style == "intense":
            return "cut"
        elif style == "calm":
            return random.choice(["dissolve", "fade"])
        else:
            return random.choice(["cut", "dissolve"])


