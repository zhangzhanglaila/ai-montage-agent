"""视频渲染：截取/变速/转场/BGM 混音。"""

import subprocess
from pathlib import Path
from typing import List, Optional

from packages.core_types.models import TimelineEntry

from .helpers import _probe_media_duration, _safe_float
from .paths import OUTPUT_DIR
from .sync import BLACK_UNSAFE_TRANSITIONS, MAX_BEAT_XFADE


class VideoRenderer:
    """视频渲染器"""

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(
        self,
        timeline: List[TimelineEntry],
        bgm_path: str,
        output_path: str,
        bgm_offset: float = 0.0,
        source_volume: float = 0.2,
    ) -> str:
        """渲染最终视频（支持转场效果，且不产生黑场）"""
        print(f"  渲染视频...")

        # 收集有效的镜头（按时间排序）
        valid_entries = [e for e in timeline if e.shot_path and Path(e.shot_path).exists()]
        if not valid_entries:
            raise ValueError("没有有效的镜头文件")

        temp_files = []

        # Step 0: 卡点转场补偿（关键）
        # xfade 的输出 = 前段 + 后段 - 转场时长，会把后面的切点整体提前。
        # 这里给"前一镜头"尾部多准备 d 秒（d = 后一镜头的转场时长），
        # xfade 吃掉 d 后总长不变；新镜头仍然在节拍点开始，转场只发生在
        # 节拍之后 d 秒内。d 被压到 MAX_BEAT_XFADE，避免长冻结。
        for i, entry in enumerate(valid_entries):
            nxt = valid_entries[i + 1] if i + 1 < len(valid_entries) else None
            if nxt is not None and getattr(nxt, "transition_type", "cut") != "cut":
                d = float(getattr(nxt, "transition_duration", 0.0) or 0.0)
                d = max(0.0, min(d, MAX_BEAT_XFADE))
                if d < 0.03:
                    # 太短的转场没有意义，直接硬切，避免无谓的冻结/闪烁
                    nxt.transition_type = "cut"
                    nxt.transition_duration = 0.0

        # Step 1: 截取 + 调整速度（合并为一次 FFmpeg 调用）
        import shutil
        prepared = []
        for idx, entry in enumerate(valid_entries):
            nxt = valid_entries[idx + 1] if idx + 1 < len(valid_entries) else None
            d_out = 0.0
            if nxt is not None and getattr(nxt, "transition_type", "cut") != "cut":
                d_out = float(getattr(nxt, "transition_duration", 0.0) or 0.0)
            clip_path = self.output_dir / f"clip_{idx}.mp4"
            # 多出来的 d_out 会让 _prepare_clip 用 tpad 克隆末帧补足；
            # 0.08s @30fps 约 2~3 帧，只用于转场，不会出现黑场。
            self._prepare_clip(
                entry.shot_path, str(clip_path),
                float(entry.duration) + d_out, entry.speed_factor,
            )
            temp_files.append(clip_path)
            prepared.append(str(clip_path))

        # Step 2: 检查是否有非 cut 转场
        has_transitions = any(
            getattr(e, "transition_type", "cut") != "cut" and getattr(e, "transition_duration", 0) > 0
            for e in valid_entries
        )

        if has_transitions and len(prepared) > 1:
            # 使用 xfade 滤镜应用转场
            concat_video = self._render_with_transitions(prepared, valid_entries)
        else:
            # 简单拼接（无转场或全部是 cut）
            concat_video = self._render_concat(prepared)

        temp_files.append(concat_video)

        # Step 3: BGM 混音（BGM 为主 + 原始音频低音量叠加）
        # 先归一化 BGM 音量（-14 LUFS 标准响度）；bgm_offset 用于裁掉前奏，
        # 让 BGM 的第一个节拍与成片 0s 对齐，而不是在开头补黑场。
        normalized_bgm = self.output_dir / "bgm_normalized.wav"
        self._normalize_bgm(bgm_path, str(normalized_bgm), start=bgm_offset)
        temp_files.append(normalized_bgm)

        # 混音：BGM 100% + 原始音频 source_volume（默认 20%，可静音）
        cmd = [
            "ffmpeg", "-y",
            "-i", str(concat_video),
            "-i", str(normalized_bgm),
            "-filter_complex",
            f"[0:a]volume={max(0.0, float(source_volume))}[orig];"
            f"[1:a]volume=1.0[bgm];"
            f"[orig][bgm]amix=inputs=2:duration=shortest:dropout_transition=2[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # 如果原始视频没有音频轨道，回退到只用 BGM
            print(f"  混音失败（可能无原音轨），使用纯 BGM")
            cmd = [
                "ffmpeg", "-y",
                "-i", str(concat_video),
                "-i", str(normalized_bgm),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                "-movflags", "+faststart",
                output_path
            ]
            subprocess.run(cmd, capture_output=True, check=True)

        # 清理临时文件
        for f in temp_files:
            Path(f).unlink(missing_ok=True)

        print(f"  输出: {output_path}")
        return output_path

    def _render_concat(self, video_paths: List[str]) -> str:
        """简单拼接（无转场）"""
        concat_file = self.output_dir / "concat.txt"
        with open(concat_file, 'w') as f:
            for path in video_paths:
                f.write(f"file '{Path(path).resolve().as_posix()}'\n")

        concat_video = self.output_dir / "concat.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(concat_video)
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        concat_file.unlink(missing_ok=True)
        return str(concat_video)

    def _render_with_transitions(self, video_paths: List[str], entries: List[TimelineEntry]) -> str:
        """渲染视频：cut 用 concat，非 cut 连续段用 xfade"""
        if len(video_paths) < 2:
            return self._render_concat(video_paths)

        durations = [self._get_duration(p) for p in video_paths]
        W, H = 1280, 720

        # 统一分辨率和帧率（**保留音轨**：转场段的 acrossfade 需要它）
        scaled_files = []
        for i, path in enumerate(video_paths):
            scaled = self.output_dir / f"scaled_{i}.mp4"
            cmd = ["ffmpeg", "-y", "-i", path]
            if self._has_audio(path):
                maps = ["-map", "0:v:0", "-map", "0:a:0"]
            else:
                # 源无音轨时补静音轨，保证各段流结构一致（concat -c copy 才不会错位）
                cmd.extend(["-f", "lavfi", "-i",
                            "anullsrc=channel_layout=stereo:sample_rate=44100"])
                maps = ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            cmd.extend([
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30",
                *maps,
                "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                str(scaled)
            ])
            subprocess.run(cmd, capture_output=True, check=True)
            scaled_files.append(str(scaled))

        # 按转场类型分段：连续非 cut 的 clips 用 xfade，cut 处断开
        segments = []
        current_seg = [0]
        for i in range(1, len(video_paths)):
            entry = entries[i] if i < len(entries) else entries[-1]
            trans_type = getattr(entry, "transition_type", "cut")
            trans_dur = getattr(entry, "transition_duration", 0.5)
            if trans_type != "cut" and trans_dur > 0:
                current_seg.append(i)
            else:
                segments.append(current_seg)
                current_seg = [i]
        segments.append(current_seg)

        # 渲染每个 segment
        segment_videos = []
        xfade_temps = []
        for seg in segments:
            if len(seg) == 1:
                segment_videos.append(scaled_files[seg[0]])
            else:
                sv = self._xfade_segment(scaled_files, seg, durations, entries)
                segment_videos.append(sv)
                xfade_temps.append(sv)

        # concat 所有 segments
        if len(segment_videos) == 1:
            result_path = segment_videos[0]
        else:
            result_path = self._render_concat(segment_videos)

        # 清理临时文件（注意：单段 xfade 时 result_path 本身就是临时文件，不能删）
        keep = str(result_path)
        for f in scaled_files + xfade_temps:
            if str(f) != keep:
                Path(f).unlink(missing_ok=True)

        return result_path

    def _xfade_segment(self, scaled_files: List[str], indices: List[int], durations: List[float], entries: List[TimelineEntry]) -> str:
        """对一组连续非-cut clips 应用 xfade 链（**视频 xfade + 音频 acrossfade 同步缩短**）"""
        seg_dur = [durations[i] for i in indices]
        filter_parts = []
        accum_dur = seg_dur[0]      # 视频流累计时长（xfade 语义：输出 = offset + 后段时长）
        accum_audio = seg_dur[0]    # 音频流累计时长（acrossfade 语义：输出 = 前段 + 后段 - d）
        current_label = "[0:v]"
        current_audio = "[0:a]"

        # 注意：这里**不允许**再映射到 fadeblack。
        # 原先把 flash/blur/motion_blur/shake 都映射成 fadeblack，成片里会出现
        # 明显的黑场；用户要求"画面丝滑、不要黑场"，统一改成 fade/dissolve。
        xfade_map = {
            "fade": "fade", "dissolve": "dissolve",
            "wipe": "wipeleft", "flash": "fade",
            "zoom": "fade", "blur": "fade",
            "motion_blur": "fade",
            "wipeleft": "wipeleft", "wiperight": "wiperight",
            "wipeup": "wipeup", "wipedown": "wipedown",
            "slideleft": "slideleft", "slideright": "slideright",
            "slideup": "slideup", "slidedown": "slidedown",
            "smoothleft": "smoothleft", "smoothright": "smoothright",
            "smoothup": "smoothup", "smoothdown": "smoothdown",
            "circleopen": "circleopen", "circleclose": "circleclose",
            # circlecrop/rectcrop 实测转场中间整帧接近全黑（mean≈0），
            # 不能满足"不要黑场"，统一退化成 fade。
            "circlecrop": "fade", "rectcrop": "fade",
            "diagtl": "diagtl", "diagtr": "diagtr",
            "diagbl": "diagbl", "diagbr": "diagbr",
            "vertopen": "vertopen", "vertclose": "vertclose",
            "horzopen": "horzopen", "horzclose": "horzclose",
            "radial": "radial", "pixelize": "pixelize",
            "distance": "distance", "fadeblack": "fade",
            "fadewhite": "fadewhite",
            "hlslice": "hlslice", "hrslice": "hrslice",
            "vuslice": "vuslice", "vdslice": "vdslice",
            "shake": "fade",
        }

        for j in range(1, len(indices)):
            i = indices[j]
            entry = entries[i] if i < len(entries) else entries[-1]
            trans_dur = getattr(entry, "transition_duration", 0.5)
            trans_type = getattr(entry, "transition_type", "fade")
            xfade_type = xfade_map.get(trans_type, "fade")

            # xfade 要求转场时长 < 参与叠合的两段画面长度，否则输出退化（画面丢失）
            trans_dur = max(0.05, min(trans_dur, 0.9 * min(seg_dur[j], accum_dur)))

            # 转场起点 = 上一段（已含 d 秒尾部补偿）末尾 - 转场时长
            # => 起点正好是标称切点：新镜头在节拍点开始，转场只发生在
            #    节拍之后 d 秒内，不会把后面的切点整体提前。
            offset = accum_dur - trans_dur
            if offset < 0:
                offset = max(0.1, accum_dur * 0.5)

            # 音频交叉淡化时长不得 ≥ 任一被叠合的片段长度（acrossfade 会报错）
            fade_d = min(trans_dur, 0.9 * min(seg_dur[j], accum_audio))
            fade_d = max(0.05, fade_d)

            accum_dur = offset + seg_dur[j]
            accum_audio = accum_audio + seg_dur[j] - fade_d

            filter_parts.append(
                f"{current_label}[{j}:v]xfade=transition={xfade_type}:"
                f"duration={trans_dur}:offset={offset:.3f}[out{j}]"
            )
            filter_parts.append(
                f"{current_audio}[{j}:a]acrossfade=d={fade_d:.3f}:c1=tri:c2=tri[a{j}]"
            )
            current_label = f"[out{j}]"
            current_audio = f"[a{j}]"

        # 音频可能因转场误差比画面略长/略短：apad 补尾 + 输出 -t 精确对齐，避免
        # concat demuxer 按流各自拼接时音画逐段漂移
        filter_parts.append(f"{current_audio}apad[aout]")

        seg_video = self.output_dir / f"xfade_seg_{indices[0]}.mp4"
        cmd = ["ffmpeg", "-y"]
        for i in indices:
            cmd.extend(["-i", scaled_files[i]])
        cmd.extend([
            "-filter_complex", ";".join(filter_parts),
            "-map", current_label,
            "-map", "[aout]",
            "-t", f"{accum_dur:.3f}",
            "-c:v", "libx264", "-crf", "23", "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            str(seg_video)
        ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"  xfade segment 失败，回退 concat")
            return self._render_concat([scaled_files[i] for i in indices])
        return str(seg_video)

    def _get_duration(self, video_path: str) -> float:
        """获取**视频流**时长（不是容器时长）

        容器 `format=duration` 是所有流时长的**最大值**。clip 的音频被 `apad`
        补齐到卡点时长后，音频往往比画面长（实测单段画面差 0.03~1.84s），
        这时再拿 format 时长当"画面长度"去算 xfade 的 `offset`，就会算出
        超过真实画面的 offset → xfade 越界、成片视频流被截断。
        所以这里取 `v:0` 的 stream duration，取不到再退回 format。
        """
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            value = float((result.stdout or "").strip())
            if value > 0:
                return value
        except ValueError:
            pass

        # 退回容器时长
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            return float(result.stdout.strip())
        except ValueError:
            print(f"  [警告] 无法获取时长: {video_path}，使用默认值 2.0s")
            return 2.0

    @staticmethod
    def _has_audio(path: str) -> bool:
        """探测输入是否含音轨。

        用途：决定要不要补一条静音轨。**所有 clip 的流结构必须一致**，
        否则 concat demuxer 的 `-c copy` 会因为某段缺音轨而错位/失败。
        """
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return bool((result.stdout or "").strip())

    @staticmethod
    def _atempo_chain(speed: float) -> str:
        """把任意倍速拆成多级 atempo（单级只支持 [0.5, 2.0]，超出必须串联）"""
        speed = max(0.1, float(speed))
        parts: List[str] = []
        s = speed
        while s > 2.0:
            parts.append("atempo=2.0")
            s /= 2.0
        while s < 0.5:
            parts.append("atempo=0.5")
            s /= 0.5
        parts.append(f"atempo={s:.6f}")
        return ",".join(parts)

    def _prepare_clip(self, input_path: str, output_path: str, duration: float, speed: float = 1.0, width: int = 1280, height: int = 720):
        """截取 + 调整速度 + 统一分辨率（合并为一次 FFmpeg 调用，**保留原音轨**）

        原实现两个分支都带 `-an`，把原音轨丢在第一步 → 后续 concat 出的视频没有音轨
        → Step3 混音的 `[0:a]` 不存在、静默回退成「纯 BGM」，原片人声/现场声永远进不了成片。
        现改为：
        - 源有声 → map 原音轨；变速时用 `atempo`（多级串联）同步变速，`apad` 补齐到画面长度；
        - 源无声 → 用 `anullsrc` 补一条静音轨，保证所有 clip 流结构一致。
        """
        has_audio = self._has_audio(input_path)
        cmd = ["ffmpeg", "-y", "-i", input_path]
        if not has_audio:
            cmd.extend(["-f", "lavfi", "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=44100"])

        # 统一分辨率 + 可选变速
        scale_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        if speed != 1.0:
            pts = 1.0 / speed
            vf = f"fps=30,setpts={pts}*PTS,{scale_filter}"
        else:
            vf = f"fps=30,{scale_filter}"

        # 画面长度保护（关键）：
        # 卡点时长 `duration` 只由**歌曲**决定（拍数 × 拍间隔），从不看镜头实际有多少画面；
        # 而 `apad` 只补音频。若不管，这一段就会变成"画面 0.2s + 声音 2.0s"——段内音画不等长，
        # 后果：① concat demuxer 的 `-c copy` 按流各自拼接，音画逐段漂移；
        #        ② xfade 的 `offset` 超过真实画面长度 → 越界，成片视频流被截断
        #          （实测成片只剩 1.53s 画面 / 7.0s 声音）。
        # 这里用 `tpad` 克隆末帧把画面补足到 duration，使每段严格 音长 == 画长 == duration。
        src_v = self._get_duration(input_path)
        eff_v = src_v / speed if speed > 0 else src_v
        pad = duration - eff_v
        if pad > 0.02:
            vf += f",tpad=stop_mode=clone:stop_duration={pad + 0.10:.3f}"

        cmd.extend(["-vf", vf])

        cmd.extend(["-map", "0:v:0"])
        if has_audio:
            cmd.extend(["-map", "0:a:0"])
            af = f"{self._atempo_chain(speed)},apad" if speed != 1.0 else "apad"
            cmd.extend(["-af", af])
        else:
            cmd.extend(["-map", "1:a:0"])

        cmd.extend([
            "-t", str(duration),
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            output_path,
        ])
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=60)
        except subprocess.TimeoutExpired:
            print(f"  [警告] clip 准备超时: {input_path}")

    def _trim_video(self, input_path: str, output_path: str, duration: float):
        """截取视频到指定时长"""
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)

    def _normalize_bgm(self, input_path: str, output_path: str,
                       target_lufs: float = -14.0, start: float = 0.0):
        """
        BGM 音量归一化（两遍 loudnorm）
        目标：-14 LUFS（流媒体标准响度）

        start > 0 时从该时间点开始截取 BGM（用于裁掉前奏，把第一个节拍
        对齐到成片 0s；比"开头补黑场"更符合"无黑场"要求）。
        """
        start = max(0.0, float(start or 0.0))
        seek = ["-ss", f"{start:.3f}"] if start > 0.001 else []

        # 第一遍：分析
        cmd_analyze = [
            "ffmpeg", "-y", *seek, "-i", input_path,
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"
        ]
        result = subprocess.run(cmd_analyze, capture_output=True, text=True)

        # 解析分析结果
        import json as _json
        stderr = result.stderr or ""
        measured_i = -14.0
        measured_tp = -1.5
        measured_lra = 11.0
        measured_thresh = -24.0
        offset = 0.0

        try:
            # 从 stderr 中提取 JSON 块
            json_start = stderr.rfind('{')
            json_end = stderr.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                stats = _json.loads(stderr[json_start:json_end])
                measured_i = _safe_float(stats.get("input_i"), -14.0)
                measured_tp = _safe_float(stats.get("input_tp"), -1.5)
                measured_lra = _safe_float(stats.get("input_lra"), 11.0)
                measured_thresh = _safe_float(stats.get("input_thresh"), -24.0)
                offset = _safe_float(stats.get("target_offset"), 0.0)
        except (ValueError, KeyError, _json.JSONDecodeError):
            pass

        # 静音/极低电平输入兜底：double-pass 需要有限且有效的测量值，
        # 若判定为静音（≤ -70 LUFS）则降级为单遍 loudnorm。
        if measured_i <= -70.0:
            af = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
        else:
            af = (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
                f":measured_I={measured_i}"
                f":measured_TP={measured_tp}"
                f":measured_LRA={measured_lra}"
                f":measured_thresh={measured_thresh}"
                f":offset={offset}"
                f":linear=true"
            )

        # 第二遍：应用归一化；开头做 30ms 淡入，避免裁前奏后出现爆音
        cmd_normalize = [
            "ffmpeg", "-y", *seek, "-i", input_path,
            "-af", af + ",afade=t=in:st=0:d=0.03",
            "-ar", "44100",
            output_path
        ]
        proc = subprocess.run(cmd_normalize, capture_output=True)
        if proc.returncode != 0:
            # 最终兜底：仅重采样到 44.1kHz，不做响度归一化，保证流程不中断
            subprocess.run(
                ["ffmpeg", "-y", *seek, "-i", input_path, "-ar", "44100",
                 "-af", "volume=1.0,afade=t=in:st=0:d=0.03", output_path],
                capture_output=True, check=True,
            )

    def _adjust_speed(self, input_path: str, output_path: str, speed: float):
        """调整视频速度"""
        pts = 1.0 / speed
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", f"setpts={pts}*PTS",
            "-an",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)
