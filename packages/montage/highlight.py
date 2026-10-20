"""镜头高光评分。"""

import math

from packages.core_types.models import MotionData, Shot


class HighlightScorer:
    """高光评分 - 5 维评分系统

    维度：运动幅度(0.25)、镜头多样性(0.20)、人脸情感(0.25)、镜头运动(0.15)、音频(0.15)
    """

    def __init__(self):
        self.weights = {
            "motion": 0.25,
            "shot_diversity": 0.20,
            "face_emotion": 0.25,
            "camera_movement": 0.15,
            "audio": 0.15,
        }

    def score(self, shot: Shot, motion: MotionData) -> float:
        """计算高光分数（5 维）"""
        w = self.weights

        # 1. 运动幅度（sigmoid 归一化）
        motion_score = self._sigmoid(motion.magnitude, center=3.0, scale=1.0)

        # 2. 镜头时长多样性（1-3 秒最佳）
        duration_score = self._duration_score(shot.duration)

        # 3. 人脸情感（如果有 action 信息）
        face_score = self._face_emotion_score(shot)

        # 4. 镜头运动（shake + zoom）
        camera_score = self._camera_movement_score(motion)

        # 5. 音频能量（如果可用）
        audio_score = self._audio_score(shot)

        total = (
            w["motion"] * motion_score +
            w["shot_diversity"] * duration_score +
            w["face_emotion"] * face_score +
            w["camera_movement"] * camera_score +
            w["audio"] * audio_score
        )

        return min(max(total, 0.0), 1.0)

    def _sigmoid(self, x: float, center: float = 3.0, scale: float = 1.0) -> float:
        """Sigmoid 归一化"""
        import math
        return 1.0 / (1.0 + math.exp(-scale * (x - center)))

    def _duration_score(self, duration: float) -> float:
        """时长分数（1-3 秒最佳）"""
        if 1.0 <= duration <= 3.0:
            return 1.0
        elif duration < 1.0:
            return duration
        else:
            return max(0.3, 1.0 - (duration - 3.0) * 0.1)

    def _face_emotion_score(self, shot: Shot) -> float:
        """人脸情感分数（基于 action 信息）"""
        if not shot.actions:
            return 0.5  # 默认中性

        # 情感权重映射
        emotion_weights = {
            "angry": 0.9, "fear": 0.8, "surprise": 0.7,
            "sad": 0.6, "happy": 0.5, "disgust": 0.4, "neutral": 0.1,
        }

        # 从 actions 中提取情感
        for action in shot.actions:
            action_lower = action.lower() if isinstance(action, str) else ""
            for emotion, weight in emotion_weights.items():
                if emotion in action_lower:
                    return weight

        return 0.3  # 无情感信息

    def _camera_movement_score(self, motion: MotionData) -> float:
        """镜头运动分数（shake + zoom 复合）"""
        shake_score = min(motion.shake / 3.0, 1.0) * 0.4
        zoom_score = min(abs(motion.zoom - 1.0) / 0.5, 1.0) * 0.3
        direction_score = min(
            (abs(motion.direction_x) + abs(motion.direction_y)) / 5.0, 1.0
        ) * 0.3
        return shake_score + zoom_score + direction_score

    def _audio_score(self, shot: Shot) -> float:
        """音频能量分数"""
        # 如果 shot 有额外的音频信息，使用它
        # 否则基于时长估算
        if shot.duration < 0.5:
            return 0.2
        elif shot.duration > 10:
            return 0.4
        return 0.6


