"""主体追踪 —— 为竖屏自动裁切提供「主体轨迹」。

设计取舍
--------
项目原实现依赖 OpenCV（`cv2` Haar 人脸 + 光流）。但在很多环境里 cv2/whisper 并不
安装，导致 `auto_reframe` 直接抛 ImportError。这里改为**纯 numpy/Pillow** 的
显著性 + 运动分析：

1. 低帧率抽帧（默认 4fps，缩到 160px 宽）——一次解码。
2. 每帧算「显著性」：局部对比度（|灰度 - 局部均值|） + 饱和度增益。
3. 相邻帧差算「运动」能量，与显著性加权融合。
4. 把能量沿 x/y 投影成两条一维曲线，用**滑动窗口最大化**求出最优裁切窗口位置
   （直接回答「窗口放哪」，比取质心稳得多）。
5. 一维 Kalman/EMA 平滑 + 时间方向移动平均，得到稳定的跟随轨迹。

可选：若环境装了 cv2，则额外做人脸检测，人脸存在时优先以人脸为中心的窗口
（保持主角居中）。cv2 不存在时静默跳过，不影响主流程。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


# --------------------------------------------------------------------- 数据
@dataclass
class SubjectTrack:
    """主体轨迹（时间 + 归一化中心点）"""
    times: List[float] = field(default_factory=list)
    cx: List[float] = field(default_factory=list)   # 0~1
    cy: List[float] = field(default_factory=list)   # 0~1
    conf: List[float] = field(default_factory=list)
    width: int = 0
    height: int = 0
    fps: float = 0.0

    def __len__(self) -> int:
        return len(self.times)

    def center_at(self, t: float) -> Tuple[float, float]:
        """按时间线性插值取中心点（归一化）。"""
        if not self.times:
            return 0.5, 0.5
        if t <= self.times[0]:
            return self.cx[0], self.cy[0]
        if t >= self.times[-1]:
            return self.cx[-1], self.cy[-1]
        i = int(np.searchsorted(self.times, t) - 1)
        i = max(0, min(i, len(self.times) - 2))
        t0, t1 = self.times[i], self.times[i + 1]
        r = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return (self.cx[i] + (self.cx[i + 1] - self.cx[i]) * r,
                self.cy[i] + (self.cy[i + 1] - self.cy[i]) * r)


# ------------------------------------------------------------------ 工具函数
def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe_video(video_path: str) -> Tuple[int, int, float]:
    """返回 (宽, 高, 时长秒)。"""
    r = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path,
    ])
    try:
        w, h = r.stdout.decode().strip().split(",")[:2]
        w, h = int(w), int(h)
    except Exception:
        w, h = 0, 0
    r2 = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path,
    ])
    try:
        dur = float(r2.stdout.decode().strip())
    except Exception:
        dur = 0.0
    return w, h, dur


def _moving_average(a: np.ndarray, k: int) -> np.ndarray:
    """边界保持的移动平均。"""
    if k <= 1 or len(a) < 2:
        return a
    k = min(k, len(a))
    pad = k // 2
    padded = np.pad(a, (pad, pad), mode="edge")
    kernel = np.ones(k, dtype=np.float32) / k
    return np.convolve(padded, kernel, mode="valid")[:len(a)]


def _best_window(profile: np.ndarray, win_frac: float) -> float:
    """在 1D 能量曲线上找宽度为 win_frac 的最优窗口中心（归一化）。

    为什么不能只取 ``argmax``：当主体明显小于裁切窗口时（例如小人物 + 大裁切框），
    只要能"整个装进窗口"，窗口和就几乎相同，形成一段**平台**，``argmax`` 会固定在
    平台最左端，导致窗口纹丝不动。这里改为：先取平台（近似最优的一批窗口），
    再从中挑**中心最贴近能量质心**的那个 —— 平台内该选谁就有意义了，
    同时保留了"多主体时选能量最密集窗口"的鲁棒性。
    """
    n = int(len(profile))
    if n == 0:
        return 0.5
    w = max(1, int(round(n * win_frac)))
    if w >= n:
        return 0.5

    p = np.asarray(profile, dtype=np.float64)
    p = np.clip(p, 0.0, None)
    total = float(p.sum())
    if total <= 1e-12:
        return 0.5

    c = np.concatenate([[0.0], np.cumsum(p)])
    sums = c[w:] - c[:-w]                     # 长度 n-w+1
    best = float(sums.max())
    tol = 0.02 * abs(best) if abs(best) > 1e-12 else 1e-12
    cand = np.where(sums >= best - tol)[0]
    if len(cand) == 0:
        cand = np.array([int(np.argmax(sums))])

    idx = np.arange(n, dtype=np.float64)
    centroid = float((p * idx).sum() / total)
    win_centers = cand.astype(np.float64) + w / 2.0
    j = int(cand[int(np.argmin(np.abs(win_centers - centroid)))])
    return float((j + w / 2.0) / n)


def _smooth_series(values: Sequence[float], alpha: float = 0.35,
                   ma: int = 3) -> List[float]:
    """EMA + 移动平均双重平滑。"""
    if not values:
        return []
    v = np.asarray(values, dtype=np.float32)
    ema = np.empty_like(v)
    ema[0] = v[0]
    for i in range(1, len(v)):
        ema[i] = alpha * v[i] + (1 - alpha) * ema[i - 1]
    ema = _moving_average(ema, ma)
    return [float(x) for x in ema]


# ------------------------------------------------------------------ 追踪器
class SubjectTracker:
    """纯 numpy 主体追踪器（cv2 可选增强）"""

    def __init__(
        self,
        sample_fps: float = 4.0,
        prox_width: int = 160,
        smooth_alpha: float = 0.35,
        motion_weight: float = 0.45,
        center_prior: float = 0.10,
        use_face: bool = True,
    ):
        self.sample_fps = sample_fps
        self.prox_width = prox_width
        self.smooth_alpha = smooth_alpha
        self.motion_weight = motion_weight
        self.center_prior = center_prior
        self.use_face = use_face
        self._cv2_failed = False

    # ---------------------------------------------------------- 抽帧
    def _extract(self, video_path: str, tmp_dir: str, fps: float) -> List[Tuple[float, str]]:
        out = Path(tmp_dir)
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("p_*.jpg"):
            old.unlink(missing_ok=True)
        pattern = str(out / "p_%05d.jpg")
        r = _run([
            "ffmpeg", "-y", "-i", video_path,
            "-vf", f"fps={fps},scale={self.prox_width}:-2",
            "-q:v", "4", pattern,
        ])
        frames = sorted(out.glob("p_*.jpg"))
        if r.returncode != 0 and not frames:
            return []
        return [((int(p.stem.split("_")[1]) - 1) / fps, str(p)) for p in frames]

    def _load_gray_rgb(self, path: str):
        if Image is None:
            return None, None
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                rgb = np.asarray(im, dtype=np.float32) / 255.0
        except Exception:
            return None, None
        gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        return gray, rgb

    @staticmethod
    def _box_blur2d(a: np.ndarray, k: int) -> np.ndarray:
        """快速 kxk 均值模糊（累加和实现）。"""
        h, w = a.shape
        k = max(1, min(k, h, w))
        if k <= 1:
            return a
        pad = k // 2
        padded = np.pad(a, pad, mode="edge")
        c = np.cumsum(np.cumsum(padded, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)))
        box = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
        return box[:h, :w]

    @staticmethod
    def _local_contrast(gray: np.ndarray) -> np.ndarray:
        """局部对比度：|灰度 - 均值池化灰度|（近似 box 模糊）。"""
        k = max(3, (min(gray.shape) // 12) | 1)
        return np.abs(gray - SubjectTracker._box_blur2d(gray, k))

    def _saliency(self, gray: np.ndarray, rgb: np.ndarray) -> np.ndarray:
        lc = self._local_contrast(gray)
        cmax = rgb.max(axis=2)
        cmin = rgb.min(axis=2)
        sat = np.where(cmax > 1e-6, (cmax - cmin) / np.maximum(cmax, 1e-6), 0.0)
        s = lc * 1.6 + sat * 0.6
        # 显著性能量向中心轻微加权，抑制边缘噪声导致的乱跑
        h, w = s.shape
        yy, xx = np.mgrid[0:h, 0:w]
        cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
        d2 = ((xx - cx) / max(1.0, w)) ** 2 + ((yy - cy) / max(1.0, h)) ** 2
        prior = np.exp(-d2 / 0.18)
        return s * (1.0 - self.center_prior + self.center_prior * 4 * prior)

    # ---------------------------------------------------------- 人脸（可选）
    def _face_center(self, path: str) -> Optional[Tuple[float, float]]:
        if not self.use_face or self._cv2_failed:
            return None
        try:
            import cv2
        except ImportError:
            self._cv2_failed = True
            return None
        try:
            img = cv2.imread(path)
            if img is None:
                return None
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            faces = cascade.detectMultiScale(gray, 1.3, 5, minSize=(24, 24))
            if len(faces) == 0:
                return None
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            h, w = gray.shape
            return ((x + fw / 2) / w, (y + fh / 2) / h)
        except Exception:
            self._cv2_failed = True
            return None

    # ---------------------------------------------------------- 主流程
    def track(
        self,
        video_path: str,
        win_w_frac: float = 0.5625,
        win_h_frac: float = 1.0,
        tmp_dir: Optional[str] = None,
        max_samples: int = 240,
    ) -> Optional[SubjectTrack]:
        """追踪主体轨迹。

        Args:
            video_path: 视频
            win_w_frac: 裁切窗口宽度占画面比例（9:16 从 16:9 裁 -> 0.5625）
            win_h_frac: 裁切窗口高度占画面比例（1.0 表示满高）
            tmp_dir: 抽帧临时目录
            max_samples: 采样上限，超长视频自动降采样率
        """
        src_w, src_h, duration = probe_video(video_path)
        if src_w <= 0 or src_h <= 0:
            return None

        fps = self.sample_fps
        if duration > 0 and duration * fps > max_samples:
            fps = max(0.5, max_samples / duration)

        tmp_dir = tmp_dir or str(Path("cache/track") / Path(video_path).stem)
        frames = self._extract(video_path, tmp_dir, fps)
        if not frames:
            return None

        times_all = [i / fps for i in range(len(frames))]

        energy_maps = []
        valid_idx: List[int] = []
        prev_gray = None
        for i, (_t, p) in enumerate(frames):
            gray, rgb = self._load_gray_rgb(p)
            if gray is None:
                continue
            e = self._saliency(gray, rgb)
            if prev_gray is not None and prev_gray.shape == gray.shape:
                motion = np.abs(gray - prev_gray)
                motion = self._box_blur2d(motion, 5)
                me, mm = float(e.mean()), float(motion.mean())
                if me > 1e-9 and mm > 1e-9:
                    e = (1 - self.motion_weight) * (e / me) + self.motion_weight * (motion / mm)
            energy_maps.append(e)
            valid_idx.append(i)
            prev_gray = gray

        if not energy_maps:
            return None

        xs: List[float] = []
        ys: List[float] = []
        confs: List[float] = []
        for i, e in enumerate(energy_maps):
            col = e.sum(axis=0)
            row = e.sum(axis=1)
            col = _moving_average(col, max(1, len(col) // 40))
            row = _moving_average(row, max(1, len(row) // 40))
            cx = _best_window(col, win_w_frac)
            cy = _best_window(row, win_h_frac)
            # 人脸优先
            fc = self._face_center(frames[valid_idx[i]][1]) if i < len(valid_idx) else None
            conf = 1.0
            if fc is not None:
                fcx = min(1.0 - win_w_frac / 2, max(win_w_frac / 2, fc[0]))
                cx = 0.7 * fcx + 0.3 * cx
                conf = 1.5
            xs.append(cx)
            ys.append(cy)
            confs.append(conf)

        track = SubjectTrack(
            times=[round(times_all[i], 3) for i in valid_idx],
            cx=_smooth_series(xs, self.smooth_alpha, 3),
            cy=_smooth_series(ys, self.smooth_alpha, 3),
            conf=confs,
            width=src_w, height=src_h, fps=fps,
        )
        return track


def track_subject(video_path: str, **kwargs) -> Optional[SubjectTrack]:
    """便捷函数：追踪主体轨迹。"""
    return SubjectTracker(**{k: v for k, v in kwargs.items()
                             if k in ("sample_fps", "prox_width", "smooth_alpha",
                                      "motion_weight", "center_prior", "use_face")}
                          ).track(video_path, **{
                              k: v for k, v in kwargs.items()
                              if k in ("win_w_frac", "win_h_frac", "tmp_dir", "max_samples")})
