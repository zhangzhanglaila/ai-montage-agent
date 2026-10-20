"""混剪主流程：镜头检测 → 评分 → 卡点 → 渲染 → 后处理。"""

import json
import os
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from packages.core_types.models import (
    Beat, BeatAnalysis, HighlightScore, MotionData, MusicSegment, Shot, TimelineEntry,
)

from .beat import BeatAnalyzer
from .helpers import _probe_media_duration
from .highlight import HighlightScorer
from .renderer import VideoRenderer
from .shot import MotionAnalyzer, ShotDetector
from .sync import BeatSyncEngine


# 画面级去重指纹缓存：key = (路径, mtime_ns, 大小) -> 8fps/16x16 灰度指纹序列
_VISUAL_SIG_CACHE: Dict[Any, Any] = {}


class MontagePipeline:
    """混剪 Pipeline - 真正的端到端流程"""

    def __init__(self, cache_dir: str = "cache", output_dir: str = "output"):
        self.cache_dir = cache_dir
        self.output_dir = output_dir

        self.shot_detector = ShotDetector(f"{cache_dir}/shots")
        self.motion_analyzer = MotionAnalyzer()
        self.beat_analyzer = BeatAnalyzer()
        self.scorer = HighlightScorer()
        self.sync_engine = BeatSyncEngine()
        self.renderer = VideoRenderer(output_dir)

        # 时间线数据（供导出使用）
        self._last_timeline = []
        self._last_bgm_path = ""
        self._last_bgm_offset = 0.0
        self._last_total_duration = 0.0

    # ---- 自动去黑场 ----
    @staticmethod
    def _has_audio(path: str) -> bool:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        return bool((r.stdout or "").strip())

    @staticmethod
    def _black_ranges(path: str, min_duration: float = 0.04,
                      pix_th: float = 0.10):
        """返回黑场区间 [(start, end), ...]，已合并相邻区间。"""
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path), "-vf",
             f"blackdetect=d={min_duration}:pix_th={pix_th}",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, errors="ignore",
        )
        ranges = []
        for line in (r.stderr or "").splitlines():
            m = re.search(r"black_start:([\d.]+)\s+black_end:([\d.]+)", line)
            if m:
                ranges.append((float(m.group(1)), float(m.group(2))))
        merged = []
        for a, b in ranges:
            if merged and a <= merged[-1][1] + 0.02:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def clean_black_source(self, path: str, margin: float = 0.12,
                           verbose: bool = True) -> str:
        """检测并删除源视频里的黑场，返回干净文件路径。

        用户直接上传带黑场过渡的 AMV 时，ShotDetector 会把黑帧一起切进镜头；
        成片就会出现黑闪。这里先把黑段（前后各留 margin）删掉，再重编码拼接。
        没有黑场时原样返回，不产生额外转码。
        """
        src = Path(path)
        if not src.exists():
            return str(src)
        total = _probe_media_duration(str(src))
        if total <= 0:
            return str(src)
        blacks = self._black_ranges(str(src))
        if not blacks:
            return str(src)

        # 构造保留区间（黑段前后各扣 margin，避免边界残黑）
        keep = []
        cur = 0.0
        for a, b in blacks:
            if a - cur > 0.20:
                keep.append((cur, max(cur, a - margin)))
            cur = max(cur, b + margin)
        if total - cur > 0.20:
            keep.append((cur, total))
        if not keep:
            return str(src)

        out_dir = Path(self.cache_dir) / "clean"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{src.stem}_clean_{int(margin * 1000)}.mp4"
        if out.exists() and out.stat().st_size > 1000:
            if not self._black_ranges(str(out)):
                return str(out)

        has_audio = self._has_audio(str(src))
        parts = []
        for i, (a, b) in enumerate(keep):
            p = out_dir / f"_part_{src.stem}_{i}.mp4"
            cmd = ["ffmpeg", "-y", "-v", "error",
                   "-ss", f"{a:.3f}", "-i", str(src), "-t", f"{b - a:.3f}",
                   "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                   "-pix_fmt", "yuv420p"]
            if has_audio:
                cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
            else:
                cmd += ["-an"]
            subprocess.run(cmd + [str(p)], capture_output=True, check=True)
            parts.append(p)

        def _concat(parts, with_audio: bool) -> bool:
            ins = []
            for p in parts:
                ins += ["-i", str(p)]
            if with_audio:
                fc = "".join(f"[{i}:v][{i}:a]" for i in range(len(parts)))
                fc += f"concat=n={len(parts)}:v=1:a=1[v][a]"
                maps = ["-map", "[v]", "-map", "[a]",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
            else:
                fc = "".join(f"[{i}:v]" for i in range(len(parts)))
                fc += f"concat=n={len(parts)}:v=1:a=0[v]"
                maps = ["-map", "[v]"]
            cmd = ["ffmpeg", "-y", "-v", "error", *ins,
                   "-filter_complex", fc, *maps,
                   "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                   "-pix_fmt", "yuv420p", str(out)]
            return subprocess.run(cmd, capture_output=True).returncode == 0

        ok = _concat(parts, has_audio)
        if not ok and has_audio:
            # 某段没有音轨时，退化成纯视频，保证清理成功
            for p in parts:
                p.unlink(missing_ok=True)
            parts = []
            for i, (a, b) in enumerate(keep):
                p = out_dir / f"_part_{src.stem}_{i}.mp4"
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}",
                     "-i", str(src), "-t", f"{b - a:.3f}", "-an",
                     "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                     "-pix_fmt", "yuv420p", str(p)],
                    capture_output=True, check=True,
                )
                parts.append(p)
            ok = _concat(parts, False)
        for p in parts:
            p.unlink(missing_ok=True)
        if not ok or not out.exists():
            return str(src)

        if verbose:
            clean_dur = _probe_media_duration(str(out))
            print(f"  去黑场: {src.name} 黑段 {len(blacks)} 个 "
                  f"{total:.1f}s -> {clean_dur:.1f}s")

        # 边界仍有一两帧残黑时，加大 margin 重试一次
        if self._black_ranges(str(out)) and margin < 0.30:
            return self.clean_black_source(str(src), margin=margin + 0.10,
                                           verbose=verbose)
        return str(out)

    @staticmethod
    def _interval_gap(a: Shot, b: Shot) -> float:
        """两个镜头在源时间轴上相隔多少秒（源区间重叠时为负数）"""
        return max(a.start_time, b.start_time) - min(a.end_time, b.end_time)

    # ---- 画面级去重（指纹参数）----
    # 分辨率取 16x16 是实测选出来的：8x8 太粗，细高频纹理（如密棋盘）降采样后会
    # 平均成同一种灰而误判（实测合成素材非重复对 MSE 只有 9.9）；16x16 下同一组
    # 素材升到 1658。真实素材实测：重复对 MSE 0~37，最接近的非重复对 1319 → 阈值 200
    # 落在两者几何中点附近，上下各留 5 倍余量。
    VISUAL_SIG_FPS = 8      # 指纹采样率
    VISUAL_SIG_SIZE = 16    # 每帧缩到 16x16 灰度
    VISUAL_WIN = 4          # 比对窗口 0.5s
    VISUAL_DEFAULT_THRESHOLD = 200.0

    @classmethod
    def _visual_signature(cls, path: str) -> np.ndarray:
        """把镜头抽成 8fps / 16x16 灰度的指纹序列（每帧 256 维）。

        用于**画面级**去重：素材片自带的片头回顾、闪回会让同一画面在源里出现两次，
        此时两个镜头的**源区间毫无重叠**，区间去重完全抓不到，只能比对画面内容。
        结果按 (路径, mtime, 大小) 缓存，重复调用不重复解码。
        """
        try:
            st = os.stat(path)
            key = (str(path), int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            key = (str(path), 0, 0)
        cached = _VISUAL_SIG_CACHE.get(key)
        if cached is not None:
            return cached

        size = cls.VISUAL_SIG_SIZE
        cmd = [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-vf", f"fps={cls.VISUAL_SIG_FPS},scale={size}:{size}",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ]
        out = subprocess.run(cmd, capture_output=True).stdout
        px = size * size
        a = np.frombuffer(out, dtype=np.uint8).astype(np.float32)
        n = a.size // px
        sig = a[:n * px].reshape(n, px) if n else np.zeros((0, px), np.float32)
        _VISUAL_SIG_CACHE[key] = sig
        return sig

    @classmethod
    def _visual_shared_window_mse(cls, a: np.ndarray, b: np.ndarray) -> float:
        """两段指纹**最相似的 0.5s 窗口**的 MSE；无法比较时返回 inf。

        用"共享某个子窗口"而不是"整段前缀比对"：源里的重复区间往往只覆盖镜头的一部分
        （实测源 9.25-10.38s 重复，而选中的镜头是 9.27-10.64s，尾部 0.26s 并不重复），
        整段比对会被不重复的尾部拉高误差而漏判。
        近纯色/黑场窗口直接跳过 —— 它们会和任何黑场"匹配"（MSE 0.0 的假阳性）。
        """
        win = cls.VISUAL_WIN
        if a.shape[0] < win or b.shape[0] < win:
            return float("inf")
        Wa = np.lib.stride_tricks.sliding_window_view(a, win, axis=0)
        Wb = np.lib.stride_tricks.sliding_window_view(b, win, axis=0)
        best = float("inf")
        step = max(1, win // 2)
        for i in range(0, Wa.shape[0], step):
            ai = Wa[i]
            if float(ai.std()) < 2.0:
                continue
            best = min(best, float(((Wb - ai) ** 2).mean(axis=(1, 2)).min()))
        return best

    def drop_visual_duplicates(
        self,
        shots: List[Shot],
        threshold: float = None,
        verbose: bool = True,
    ) -> List[Shot]:
        """丢掉"画面内容与已选镜头重复"的镜头（跨源、跨源时间点都管）。

        动机（真实事故）：素材片自带片头回顾。实测该片源里有 3 处、合计 4.0s
        （占源 9.4%）的画面重复出现，例如源 9.25-10.38s == 38.88-40.00s（MSE 0.0）。
        选中 9.27s 与 38.90s 两个镜头时，它们的源区间毫无重叠、区间去重全部放行，
        但成片里就是同一段画面出现两次 —— 用户看到的就是"一个片段用了 4 次"。
        这里按画面内容比对，同一画面只保留高光分最高的一个。

        Args:
            shots: 候选镜头
            threshold: 指纹 MSE 阈值（16x16 灰度尺度，默认 200）；同一画面实测 0~37，
                不同画面最接近的一对实测 1319。None 用默认值，<=0 关闭。
        """
        if threshold is None:
            threshold = self.VISUAL_DEFAULT_THRESHOLD
        if not shots or float(threshold) <= 0:
            return list(shots)

        kept: List[Shot] = []
        sigs: List[np.ndarray] = []
        dropped = []
        for shot in sorted(shots, key=lambda s: (-s.highlight_score, s.start_time)):
            path = shot.file_path or ""
            if not path or not Path(path).exists():
                kept.append(shot)
                sigs.append(np.zeros((0, self.VISUAL_SIG_SIZE ** 2), np.float32))
                continue
            sig = self._visual_signature(path)
            dup_of = None
            for k, ksig in zip(kept, sigs):
                m = self._visual_shared_window_mse(sig, ksig)
                if m < threshold:
                    dup_of = (k, m)
                    break
            if dup_of is None:
                kept.append(shot)
                sigs.append(sig)
            else:
                dropped.append((shot, dup_of[0], dup_of[1]))

        if verbose:
            if dropped:
                print(f"  画面级去重：丢掉 {len(dropped)} 个与已选镜头画面重复的镜头")
                for s, k, m in dropped:
                    print(f"    shot_{s.shot_id:05d}(源{s.start_time:.2f}s) 与 "
                          f"shot_{k.shot_id:05d}(源{k.start_time:.2f}s) 画面重复"
                          f"（MSE {m:.1f}）")
            else:
                print("  画面级去重：未发现画面重复的镜头")
        return kept

    def select_highlight_shots(
        self,
        all_shots: List[Shot],
        max_per_video: int = 15,
        min_shot_gap: float = 1.0,
        max_total: int = 50,
        visual_dedup_threshold: float = None,
        verbose: bool = True,
    ) -> List[Shot]:
        """按源分组挑高光镜头，并保证同一源内入选镜头的**源区间互不相接**。

        原实现（已修）是 `min_gap = 20; if abs(shot.shot_id - last_id) >= min_gap`，
        有三个毛病，导致成片里出现"重复镜头"：
          1) 只跟「上一个入选者」比，不跟**所有已选镜头**比。而循环是按分数降序走的，
             于是 shot2[1.00-2.00] 与 shot4[2.13-3.00] 这种源上紧挨着的镜头，只要各自
             离"上一个"够远就都能入选 —— 实测 15 个镜头里 9 对源起点差 <2s，
             id71/75/77 三个还全挤在 32.87~36.37 这 3.5s 窗口里。
          2) 用「起点差」也不够：id61[26.40-28.43] 与 id62[28.43-29.63] 起点虽差 2.03s，
             但两段源区间**严丝合缝相接**，成片里就是同一段连续画面出现两次。
             所以这里比较的是**区间空隙** `max(起点)-min(终点)`，而不是起点差。
          3) shot_id 差值本身不可靠：shot_id = video_index*10000 + i，而 ShotDetector.detect()
             会 continue 掉 <0.1s 的短场景，id 并不连续，跨源比较更是毫无意义。

        Args:
            all_shots: 所有检测到的镜头
            max_per_video: 每个源最多取多少个
            min_shot_gap: 同一源内任两个入选镜头源区间之间要空出的秒数；源很长时会按
                源跨度自适应放大（`span / (max_per_video * 2.5)`）。设为 0 关闭该约束。
            max_total: 交替排列后最终截取的镜头总数上限
            visual_dedup_threshold: 画面级去重的指纹 MSE 阈值（<=0 关闭）
            verbose: 是否打印每个源的选取情况
        """
        # 画面级去重（跨源、跨源时间点）：必须放在区间贪心**之前**。
        # 素材片自带的片头回顾/闪回会让同一画面在源里出现两次，此时区间空隙约束
        # 全部放行（区间毫无重叠），只能按画面内容剔重；先剔再贪心，被剔掉的镜头
        # 会由"下一个最优"补位，不会白白少一个镜头。
        if visual_dedup_threshold is None:
            visual_dedup_threshold = self.VISUAL_DEFAULT_THRESHOLD
        if all_shots and float(visual_dedup_threshold) > 0:
            all_shots = self.drop_visual_duplicates(
                list(all_shots), threshold=float(visual_dedup_threshold), verbose=verbose)

        video_shots: Dict[str, List[Shot]] = {}
        for shot in all_shots:
            key = shot.source_video or "unknown"
            video_shots.setdefault(key, []).append(shot)

        # 每个源：按分数降序贪心，要求与**所有**已选镜头都拉开 min_sep
        per_video_lists = []
        for key, shots in video_shots.items():
            span = max(s.end_time for s in shots) - min(s.start_time for s in shots)
            if min_shot_gap and float(min_shot_gap) > 0:
                # 2.5 是留给贪心的松弛量，保证一般还能选到接近 max_per_video 个
                min_sep = max(float(min_shot_gap), span / (max_per_video * 2.5))
            else:
                min_sep = 0.0  # 显式关闭该约束（回到"尽量多选"，可能出现重复镜头）
            sorted_group = sorted(shots, key=lambda s: (-s.highlight_score, s.start_time))
            selected: List[Shot] = []
            for shot in sorted_group:
                if len(selected) >= max_per_video:
                    break
                if all(self._interval_gap(shot, s) >= min_sep for s in selected):
                    selected.append(shot)
            if verbose:
                print(f"  {Path(key).name}: 取 {len(selected)} 个镜头"
                      f"（源跨度 {span:.1f}s，最小源区间间隔 {min_sep:.2f}s）")
            per_video_lists.append(selected)

        # 交替排列不同源的镜头（避免连续同源）
        balanced_shots: List[Shot] = []
        max_len = max((len(lst) for lst in per_video_lists), default=0)
        for i in range(max_len):
            for lst in per_video_lists:
                if i < len(lst):
                    balanced_shots.append(lst[i])

        # 截取上限（保持交替排列，不按分数重新排序）
        max_shots = min(max_total, len(balanced_shots))
        if verbose:
            print(f"  选择 Top {max_shots} 高光镜头（共 {len(all_shots)} 个，"
                  f"来自 {len(video_shots)} 个视频）")
        return balanced_shots[:max_shots]

    def run(
        self,
        video_paths: List[str],
        bgm_path: str,
        style: str = "dynamic",
        output_name: str = "final.mp4",
        threshold: float = 0.2,
        color_preset: str = None,
        enable_reframe: bool = False,
        reframe_aspect: float = 9 / 16,
        reframe_method: str = "subject",
        enable_ducking: bool = False,
        duck_level_db: float = -12.0,
        enable_harmonize: bool = False,
        harmonize_strength: float = 0.5,
        transition_pattern: dict = None,
        enable_sfx: bool = False,
        narration_audio: str = None,
        narration_duck: bool = True,
        min_shot_gap: float = 1.0,
        visual_dedup_threshold: float = None,
        max_per_video: int = 15,
        clean_black: bool = True,
        target_duration: Optional[float] = None,
        mute_source_audio: bool = False,
    ) -> str:
        """
        运行完整混剪流程

        Args:
            video_paths: 视频文件路径列表
            bgm_path: BGM 文件路径
            style: 风格 (dynamic, calm, intense)
            output_name: 输出文件名
            threshold: 镜头检测灵敏度 (0.01~1.0，越小切得越细)
            color_preset: 颜色分级预设 (如 cinematic, vibrant, vintage 等)
            enable_reframe: 是否启用自动竖屏裁切
            reframe_aspect: 竖屏裁切目标宽高比 (默认 9:16)
            enable_ducking: 是否启用对话闪避
            duck_level_db: 闪避降低分贝数 (默认 -12dB)
            enable_harmonize: 是否启用色彩协调
            harmonize_strength: 色彩协调强度 (0.0~1.0)
            transition_pattern: 转场模板
            enable_sfx: 是否启用音效卡点（按强拍/剪辑点叠加音效）
            narration_audio: 旁白音频路径（有则叠加到成片并自动闪避 BGM）
            narration_duck: 旁白时段是否自动压低 BGM
            min_shot_gap: 同一源内任两个入选镜头的**源区间之间**要空出的秒数（默认 1.0），
                用于避免近邻镜头造成的「重复镜头」观感；源很长时会按源跨度自适应放大。
                调大可进一步减少重复（本片实测：1.0→13 镜头、2.5→12、3.0→10），
                设 0 可关闭该约束（回到「尽量多选」的旧行为，可能出现重复镜头）。
            visual_dedup_threshold: 画面级去重的指纹 MSE 阈值（<=0 关闭）。素材片自带
                片头回顾/闪回时，同一画面在源里出现两次且源区间毫无重叠，区间约束抓不到；
                默认 200（16x16 灰度尺度，同一画面实测 0~37，不同画面最接近的一对 1319）。
            max_per_video: 每个源视频最多选多少个高光镜头（默认 15）。
                想生成更长的混剪可调大，例如 25~30。
            clean_black: 自动检测并删除源视频里的黑场（默认 True）。
                直接上传带黑场过渡的 AMV 时，避免成片出现黑闪。
            target_duration: 目标成片时长（秒）。给定时会在 5~50 个镜头、
                0~1s 间隔之间自动挑最接近目标时长的组合。
            mute_source_audio: 是否静音原片音频（只保留 BGM/音效）。

        Returns:
            输出文件路径
        """
        print("=" * 50)
        print("AI Montage Agent - 开始混剪")
        print("=" * 50)

        # Step 1: 检测镜头
        print(f"\n[1/6] 检测镜头 (阈值: {threshold})...")
        if not video_paths:
            raise ValueError("没有可用的视频文件，请检查搜索关键词或上传文件")

        # 自动去黑场：先把源里的黑段删掉，避免黑帧被切进镜头
        if clean_black:
            cleaned = []
            for vp in video_paths:
                try:
                    cleaned.append(self.clean_black_source(vp))
                except Exception as e:
                    print(f"  去黑场失败（跳过）: {e}")
                    cleaned.append(vp)
            video_paths = cleaned

        all_shots = []
        for video_idx, video_path in enumerate(video_paths):
            print(f"  处理视频: {Path(video_path).name}")
            shots = self.shot_detector.detect(video_path, threshold=threshold, video_index=video_idx)
            all_shots.extend(shots)

        if not all_shots:
            raise ValueError("没有检测到任何镜头，视频文件可能损坏或格式不支持")

        # Step 2: 分析运动
        print("\n[2/6] 分析运动...")
        for shot in all_shots:
            motion = self.motion_analyzer.analyze(shot.file_path)
            shot.motion_score = motion.magnitude

        # Step 3: 评分高光
        print("\n[3/6] 评分高光...")
        for shot in all_shots:
            motion = MotionData(magnitude=shot.motion_score)
            shot.highlight_score = self.scorer.score(shot, motion)

        # Step 4: 分析 BGM
        print("\n[4/6] 分析 BGM...")
        beat_analysis = self.beat_analyzer.analyze(bgm_path)

        # 关键：把 BGM 的第一个节拍对齐到视频 0s。
        # 原来时间线从 "第一个检测到的节拍"（这首歌是 3.18s）开始，而渲染器
        # 从 0s 顺序拼接 → 所有画面比音乐早 3.18s；若在开头补黑场又会违反
        # "不要黑场" 的要求。这里直接裁掉前奏，并同步平移所有节拍/能量/高潮段。
        bgm_offset = 0.0
        if beat_analysis.beats:
            first_beat = min(float(b.time) for b in beat_analysis.beats)
            if first_beat > 0.05:
                bgm_offset = first_beat
                for b in beat_analysis.beats:
                    b.time = max(0.0, float(b.time) - bgm_offset)
                beat_analysis.energy_curve = [
                    {"time": max(0.0, float(e["time"]) - bgm_offset),
                     "energy": e["energy"]}
                    for e in beat_analysis.energy_curve
                ]
                beat_analysis.drops = [
                    {"start": max(0.0, float(d["start"]) - bgm_offset),
                     "end": max(0.0, float(d["end"]) - bgm_offset)}
                    for d in beat_analysis.drops
                ]
                print(f"  BGM 前奏 {bgm_offset:.2f}s 已裁掉（开头无黑场，且画面从第一拍开始）")

        # Step 5: 卡点同步
        print("\n[5/6] 卡点同步...")
        # 按 source_video 分组，每视频最多取 top N 个镜头（带源区间间隔约束）
        top_shots = self.select_highlight_shots(
            all_shots, max_per_video=max_per_video, min_shot_gap=min_shot_gap,
            visual_dedup_threshold=visual_dedup_threshold)
        if not top_shots:
            raise ValueError("没有可用的高光镜头")
        timeline = self.sync_engine.sync(top_shots, beat_analysis.beats, style, transition_pattern)

        # 目标时长：在镜头数量 / 源区间间隔之间找最接近的组合
        if target_duration and timeline:
            best = timeline
            best_diff = abs(sum(e.duration for e in best) - float(target_duration))
            combos = [(5, 1.0), (10, 1.0), (15, 1.0), (20, 0.6),
                      (30, 0.3), (40, 0.0), (50, 0.0)]
            for mpv, gap in combos:
                if mpv == max_per_video and abs(gap - min_shot_gap) < 1e-6:
                    continue
                try:
                    shots_try = self.select_highlight_shots(
                        all_shots, max_per_video=mpv, min_shot_gap=gap,
                        visual_dedup_threshold=visual_dedup_threshold,
                        verbose=False)
                    tl_try = self.sync_engine.sync(
                        shots_try, beat_analysis.beats, style, transition_pattern)
                except Exception:
                    continue
                if not tl_try:
                    continue
                total = sum(e.duration for e in tl_try)
                diff = abs(total - float(target_duration))
                if diff < best_diff:
                    best, best_diff = tl_try, diff
                if total >= float(target_duration) * 0.95:
                    break
            timeline = best
            print(f"  目标时长 {target_duration:.1f}s -> "
                  f"实际 {sum(e.duration for e in timeline):.2f}s"
                  f"（{len(timeline)} 段）")

        if not timeline:
            raise ValueError("时间线为空")

        # 存储时间线数据（供导出使用）
        self._last_timeline = timeline
        self._last_bgm_path = bgm_path
        self._last_bgm_offset = bgm_offset
        self._last_total_duration = sum(e.duration for e in timeline)

        # 后处理：色彩协调（渲染前对镜头做，避免双重渲染）
        if enable_harmonize:
            print("\n  色彩协调...")
            try:
                from packages.video_enhancement.src.color_harmonizer import harmonize_clips
                shot_paths = [e.shot_path for e in timeline if e.shot_path and Path(e.shot_path).exists()]
                if shot_paths:
                    harmonized = [p.replace(".mp4", "_harmonized.mp4") for p in shot_paths]
                    harmonize_clips(shot_paths, harmonized, strength=harmonize_strength)
                    for i, entry in enumerate(timeline):
                        if entry.shot_path and i < len(harmonized) and Path(harmonized[i]).exists():
                            entry.shot_path = harmonized[i]
            except Exception as e:
                print(f"  色彩协调失败（跳过）: {e}")

        # Step 6: 渲染
        print("\n[6/6] 渲染输出...")
        out_p = Path(output_name)
        if out_p.parent and str(out_p.parent) != ".":
            output_path = str(out_p)
        else:
            output_path = str(Path(self.output_dir) / output_name)
        result = self.renderer.render(
            timeline, bgm_path, output_path,
            bgm_offset=bgm_offset,
            source_volume=0.0 if mute_source_audio else 0.2,
        )

        # 后处理：对话闪避
        if enable_ducking:
            print("\n  对话闪避...")
            try:
                from packages.video_enhancement.src.dialogue_ducking import duck_audio
                ducked_path = output_path.replace(".mp4", "_ducked.mp4")
                duck_audio(result, bgm_path, ducked_path, duck_level_db=duck_level_db)
                if Path(ducked_path).exists():
                    import os
                    os.replace(ducked_path, result)
            except Exception as e:
                print(f"  对话闪避失败（跳过）: {e}")

        # 后处理：颜色分级
        if color_preset and color_preset != "none":
            print(f"\n  颜色分级: {color_preset}...")
            try:
                from packages.video_enhancement.src.color_grading import apply_color_grade
                graded_path = output_path.replace(".mp4", "_graded.mp4")
                apply_color_grade(result, graded_path, preset=color_preset)
                if Path(graded_path).exists():
                    import os
                    os.replace(graded_path, result)
            except Exception as e:
                print(f"  颜色分级失败（跳过）: {e}")

        # 后处理：自动裁切（默认竖屏，目标比例可配）
        if enable_reframe:
            _suffix = _ratio_suffix(reframe_aspect)
            print(f"\n  自动裁切 (目标比例 {_suffix.lstrip('_').replace('x', ':')})...")
            try:
                from packages.video_enhancement.src.auto_reframe import auto_reframe
                reframed_path = output_path.replace(".mp4", f"{_suffix}.mp4")
                auto_reframe(result, reframed_path, target_aspect=reframe_aspect,
                             method=reframe_method)
                if Path(reframed_path).exists():
                    result = reframed_path
            except Exception as e:
                print(f"  竖屏裁切失败（跳过）: {e}")

        # 后处理：音效卡点
        if enable_sfx:
            print("\n  音效卡点...")
            try:
                from packages.sound_engine import SfxEngine
                sfx_engine = SfxEngine()
                # 按**成片实际时长**和**真实切点**选音效；否则长 BGM 的
                # 前 16 个强拍会全挤在开头，超出成片的部分还会被静默截掉。
                rendered_duration = _probe_media_duration(result) or self._last_total_duration
                cut_times = [e.start_time for e in timeline[1:]]
                cues = sfx_engine.plan(
                    beat_analysis.beats, style=style,
                    duration=rendered_duration, cut_times=cut_times,
                )
                if cues:
                    sfx_path = output_path.replace(".mp4", "_sfx.mp4")
                    sfx_engine.mix(result, cues, sfx_path)
                    if Path(sfx_path).exists():
                        result = sfx_path
                    print(f"  已叠加 {len(cues)} 个音效（均落在成片 {rendered_duration:.2f}s 内）")
                else:
                    print("  未规划出音效点（跳过）")
            except Exception as e:
                print(f"  音效卡点失败（跳过）: {e}")

        # 后处理：配音旁白（叠加人声轨，旁白时段自动闪避 BGM）
        if narration_audio:
            print("\n  叠加配音旁白...")
            try:
                from packages.voice_engine import mix_narration
                narr_path = output_path.replace(".mp4", "_voiced.mp4")
                mix_narration(result, narration_audio, narr_path, duck=narration_duck)
                if Path(narr_path).exists():
                    result = narr_path
                print("  旁白叠加完成")
            except Exception as e:
                print(f"  配音叠加失败（跳过）: {e}")

        print("\n" + "=" * 50)
        print("混剪完成!")
        print(f"输出文件: {result}")
        print(f"镜头数量: {len(all_shots)}")
        print(f"节拍数量: {len(beat_analysis.beats)}")
        print(f"BPM: {beat_analysis.tempo:.1f}")
        print("=" * 50)

        return result


