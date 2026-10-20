"""命令行入口（原 pipeline.py 的 main 与后处理辅助函数）。"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .helpers import (
    _parse_ratio, _print_task_detail, _print_task_table,
    _probe_media_duration, _ratio_suffix,
)
from .paths import OUTPUT_DIR
from .pipeline import MontagePipeline


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="AI Montage Agent")
    parser.add_argument("--movies", nargs="+", help="本地视频文件路径")
    parser.add_argument("--query", type=str, help="搜索关键词，自动下载素材（与 --movies 二选一）")
    parser.add_argument("--source", type=str, default="playphrase",
                        choices=["playphrase", "quodb", "bilibili",
                                 "youtube", "dailymotion", "douyin", "ixigua", "acfun", "vimeo",
                                 "yarn", "zhaotaici"],
                        help="素材来源（默认 playphrase）")
    parser.add_argument("--clip-limit", type=int, default=20, help="最大下载片段数，默认 20（仅 --query 模式）")
    parser.add_argument("--bgm", help="BGM 文件路径（可选，不传则自动从B站搜索下载）")
    parser.add_argument("--bgm-query", help="BGM 搜索关键词（可选，不传则按 --style 风格自动搜索）")
    parser.add_argument("--style", default="dynamic", choices=["dynamic", "calm", "intense"],
                        help="基础风格: dynamic(动感) / calm(舒缓) / intense(高燃)")
    parser.add_argument("--style-preset", type=str, default=None,
                        help="风格预设模板(如 action/wedding/cinematic/gaming 等)，会覆盖 --style 和 --enhance")
    parser.add_argument("--output", default="final.mp4", help="输出文件名")
    parser.add_argument("--threshold", type=float, default=0.2,
                        help="镜头检测灵敏度 0.01~1.0，越小切得越细（默认 0.2，混剪推荐 0.1~0.2）")
    parser.add_argument("--min-shot-gap", type=float, default=1.0,
                        help="同一源内任两个入选镜头的源区间之间要空出的秒数（默认 1.0）；"
                             "调大可减少重复镜头，0 表示关闭该约束")
    parser.add_argument("--visual-dedup-threshold", type=float, default=None,
                        help="画面级去重的指纹 MSE 阈值（默认 200，16x16 灰度尺度）；"
                             "素材片自带片头回顾/闪回时，同一画面在源里出现两次且源区间不重叠，"
                             "只能按画面内容去重；同一画面实测 0~37、不同画面最接近的一对 1319；"
                             "0 表示关闭")
    # 新增功能参数
    parser.add_argument("--prompt", type=str, help="自然语言描述，如 '做一个30秒的漫威高燃混剪'")
    parser.add_argument("--subtitles", type=str, default="none",
                        choices=["none", "tiktok", "youtube", "minimal", "karaoke",
                                 "bold", "cinematic", "bilingual"],
                        help="字幕风格（默认 none；karaoke 为逐字高亮，需 whisper）")
    parser.add_argument("--subtitle-model", type=str, default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="字幕 Whisper 模型规格（默认 base，越大越准越慢）")
    parser.add_argument("--translate", type=str, default=None,
                        help="字幕翻译目标语言（如 en/ja/ko）；给定时输出双语字幕")
    parser.add_argument("--translate-backend", type=str, default="auto",
                        choices=["auto", "llm", "deep"],
                        help="翻译后端: auto(LLM优先)/llm/deep(需 deep-translator)")
    parser.add_argument("--subtitle-lang", type=str, default=None,
                        help="字幕源语言（zh/en/ja...，默认自动检测）")
    parser.add_argument("--enhance", nargs="*", default=[],
                        help="视频增强选项: stabilize denoise color-grade auto-reframe")
    parser.add_argument("--reframe-aspect", type=str, default="16:9",
                        help="auto-reframe 目标比例，如 16:9 / 9:16 / 3:4 / 1:1（默认 16:9，即不默认出竖屏）")
    parser.add_argument("--reframe-method", type=str, default="subject",
                        choices=["subject", "center"],
                        help="auto-reframe 裁切方式：subject 主体跟随（默认）/ center 静态居中")
    parser.add_argument("--export-timeline", type=str,
                        choices=["edl", "csv", "json", "xml", "otio"],
                        help="导出时间轴格式")
    parser.add_argument("--sfx", action="store_true",
                        help="按节拍自动叠加音效（强拍 impact / 剪辑点 whoosh）")
    parser.add_argument("--narrate", type=str,
                        help="配音旁白主题（LLM 自动写解说词并合成语音）")
    parser.add_argument("--script", type=str,
                        help="自定义旁白脚本文件（每行一句，优先于 --narrate）")
    parser.add_argument("--narrate-sec", type=int, default=30,
                        help="旁白目标时长（秒，仅 --narrate 模式）")
    parser.add_argument("--voice", type=str, default=None,
                        help="TTS 音色（如 zh-CN-YunxiNeural，默认女声晓晓）")
    parser.add_argument("--no-duck", action="store_true",
                        help="旁白时段不自动压低 BGM")
    parser.add_argument("--cover", action="store_true",
                        help="自动生成封面图（挑最佳帧 + 叠加标题）")
    parser.add_argument("--cover-title", type=str, default=None,
                        help="封面主标题（默认取 --prompt 或输出文件名）")
    parser.add_argument("--cover-subtitle", type=str, default="",
                        help="封面副标题")
    parser.add_argument("--cover-size", type=str, default="16:9",
                        choices=["16:9", "9:16", "1:1", "4:3", "3:4"],
                        help="封面比例，默认 16:9")
    parser.add_argument("--cover-style", type=str, default="bold",
                        choices=["bold", "minimal", "cinematic"],
                        help="封面文字版式，默认 bold")
    parser.add_argument("--speed-curve", type=str, default=None,
                        choices=["rush", "slowmo", "hero", "punch"],
                        help="变速曲线预设: rush 渐快冲刺 / slowmo 冲入慢放 / hero 慢快慢 / punch 脉冲")
    parser.add_argument("--speed", type=float, default=None,
                        help="整段等比变速（如 2.0 双速、0.5 半速），优先于 --speed-curve")
    parser.add_argument("--speed-steps", type=int, default=10,
                        help="变速曲线分段数（越多越平滑，默认 10）")
    parser.add_argument("--audio-mix", type=str, default=None, metavar="SPEC.json",
                        help="多轨混音计划(JSON)：人声/BGM/音效独立增益+自动闪避；"
                             "spec 内不写 video 时自动以本次成片为底")
    parser.add_argument("--webui", action="store_true", help="启动 WebUI 界面")
    parser.add_argument("--index-shots", type=str, nargs="?", const="var/cache/index/shot_index.json",
                        default=None, metavar="PATH",
                        help="为 --movies 建立语义镜头索引并保存（默认 var/cache/index/shot_index.json）")
    parser.add_argument("--query-shots", type=str, default=None,
                        help="语义检索镜头，如 '明亮的画面'（需先建索引；配合 --index-shots 指定索引路径）")
    parser.add_argument("--index-top-k", type=int, default=10, help="检索返回条数（默认 10）")
    parser.add_argument("--index-export", type=str, default=None,
                        help="把检索命中的镜头切片导出到指定目录")
    parser.add_argument("--embedder", type=str, default="auto",
                        choices=["auto", "clip", "heuristic"],
                        help="镜头编码器：auto(优先CLIP)/clip/heuristic(离线)")

    # ---- 任务队列管理（持久化任务表，不跑混剪）----
    parser.add_argument("--task-db", type=str, default=None, metavar="PATH",
                        help="任务库路径（默认 var/var/cache/tasks.db 或环境变量 MONTAGE_TASK_DB）")
    parser.add_argument("--task-list", action="store_true", help="列出持久化任务")
    parser.add_argument("--task-status", type=str, default=None,
                        choices=["pending", "queued", "running", "done", "error", "canceled"],
                        help="配合 --task-list：只显示某状态的任务")
    parser.add_argument("--task-show", type=str, default=None, metavar="ID",
                        help="查看单个任务详情")
    parser.add_argument("--task-retry", type=str, default=None, metavar="ID",
                        help="手动重试任务（清零尝试次数并重新入队）")
    parser.add_argument("--task-cancel", type=str, default=None, metavar="ID",
                        help="取消任务")
    parser.add_argument("--task-resume", action="store_true",
                        help="把卡在 running 的超时任务重新入队（进程中断后断点续跑）")
    parser.add_argument("--task-stale", type=float, default=600.0,
                        help="--task-resume 的超时判定秒数（默认 600）")

    args = parser.parse_args()

    # 风格预设模板处理
    if args.style_preset:
        from packages.video_enhancement.src.style_templates import get_pipeline_params
        preset_params = get_pipeline_params(args.style_preset)
        args.style = preset_params.get("style", args.style)
        if preset_params.get("stabilize") and "stabilize" not in args.enhance:
            args.enhance.append("stabilize")
        if preset_params.get("color_preset"):
            args._color_preset = preset_params["color_preset"]
            if "color-grade" not in args.enhance:
                args.enhance.append("color-grade")
        if preset_params.get("enable_harmonize"):
            args._enable_harmonize = True
        if preset_params.get("enable_ducking"):
            args._enable_ducking = True
        if preset_params.get("enable_reframe"):
            args._enable_reframe = True
        print(f"  风格预设: {args.style_preset} -> style={args.style}, color={getattr(args, '_color_preset', 'default')}")

    # WebUI 模式
    if args.webui:
        from packages.webui import start_webui
        print("启动 WebUI: http://localhost:8000")
        start_webui()
        return

    # 任务队列管理模式（只读/操作持久化任务表，不跑混剪）
    if any([args.task_list, args.task_show, args.task_retry,
            args.task_cancel, args.task_resume]):
        from packages.task_queue import get_store
        store = get_store(args.task_db)
        print(f"任务库: {store.db_path}")

        if args.task_show:
            task = store.get(args.task_show)
            if task is None:
                print(f"任务不存在: {args.task_show}")
                return 1
            _print_task_detail(task)
            return 0

        if args.task_retry:
            if store.get(args.task_retry) is None:
                print(f"任务不存在: {args.task_retry}")
                return 1
            store.retry(args.task_retry)
            print(f"已重新入队: {args.task_retry}（WebUI 的 worker 启动后会自动执行）")
            return 0

        if args.task_cancel:
            if store.get(args.task_cancel) is None:
                print(f"任务不存在: {args.task_cancel}")
                return 1
            store.cancel(args.task_cancel)
            print(f"已取消: {args.task_cancel}")
            return 0

        if args.task_resume:
            info = store.resume_stale(stale_seconds=args.task_stale)
            print(f"续跑: 重新入队 {info['resumed']} 个，"
                  f"重试耗尽转失败 {info['exhausted']} 个")
            return 0

        rows = store.list(status=args.task_status, limit=50)
        _print_task_table(rows)
        stats = store.stats()
        print("  统计: " + "  ".join(f"{k}={v}" for k, v in stats.items()))
        return 0

    # 镜头索引构建模式
    if args.index_shots:
        if not args.movies:
            parser.error("--index-shots 需要配合 --movies 指定素材")
        from packages.shot_index import get_embedder, ShotIndex
        for movie in args.movies:
            if not Path(movie).exists():
                print(f"错误: 视频文件不存在: {movie}")
                return
        emb = get_embedder(prefer=args.embedder)
        print(f"镜头编码器: {emb.name} (dim={emb.dim})")
        index = ShotIndex(embedder=emb)
        for movie in args.movies:
            print(f"\n索引: {movie}")
            index.build(movie)
        index.save(args.index_shots)
        print(f"\n索引完成：{len(index)} 个镜头 -> {args.index_shots}")
        return

    # 镜头语义检索模式
    if args.query_shots:
        from packages.shot_index import get_embedder, ShotIndex, ShotRetriever, format_hits
        index_path = args.index_shots or "var/cache/index/shot_index.json"
        if not Path(index_path).exists():
            print(f"索引不存在: {index_path}\n请先用 --movies xxx --index-shots {index_path} 建索引")
            return
        emb = get_embedder(prefer=args.embedder)
        index = ShotIndex.load(index_path, embedder=emb)
        retriever = ShotRetriever(index)
        print(f"\n索引: {index_path}（{len(index)} 镜头，编码器 {index.name}）")
        print(f"查询: 「{args.query_shots}」")
        hits = retriever.search(query=args.query_shots, top_k=args.index_top_k)
        print(format_hits(hits, show_frame=True))
        if args.index_export and hits:
            retriever.export_clips(hits, args.index_export)
        return

    # 校验参数：--movies 和 --query 二选一
    if not args.movies and not args.query:
        parser.error("请指定 --movies（本地视频）或 --query（搜索素材）")
    if args.movies and args.query:
        parser.error("--movies 和 --query 不能同时使用")

    # 获取视频路径
    if args.query:
        video_paths = _crawl_videos(args.query, args.source, args.clip_limit, parser)
        if not video_paths:
            return
    else:
        video_paths = args.movies
        for movie in video_paths:
            if not Path(movie).exists():
                print(f"错误: 视频文件不存在: {movie}")
                return

    # BGM 处理：本地文件 → 指定搜索 → 按风格自动搜索 → 默认 BGM
    bgm_path = args.bgm
    if args.bgm_query:
        # 用户指定了搜索关键词
        from packages.video_crawler.src.bgm_crawler import BgmCrawler
        bgm_crawler = BgmCrawler()
        bgm_paths = bgm_crawler.search_and_download(args.bgm_query, max_clips=1)
        if bgm_paths:
            bgm_path = bgm_paths[0]
            print(f"  使用 BGM: {bgm_path}")
        else:
            print(f"  BGM 搜索未找到结果，尝试按风格搜索...")
            bgm_path = None
    elif bgm_path and not Path(bgm_path).exists():
        print(f"  BGM 文件不存在: {bgm_path}，尝试自动搜索...")
        bgm_path = None

    # 没有 BGM → 按风格自动从 B站搜索真实音乐
    if not bgm_path:
        from packages.video_crawler.src.bgm_crawler import BgmCrawler
        bgm_crawler = BgmCrawler()
        print(f"  自动搜索 {args.style} 风格 BGM...")
        bgm_paths = bgm_crawler.search_and_download_by_style(args.style, max_clips=1)
        if bgm_paths:
            bgm_path = bgm_paths[0]
            print(f"  使用 BGM: {bgm_path}")
        else:
            # 最终兜底：使用合成默认 BGM
            from packages.video_crawler.src.default_bgm import get_default_bgm_path
            bgm_path = get_default_bgm_path(style=args.style)
            print(f"  搜索失败，使用合成默认 BGM ({args.style}): {bgm_path}")

    # LLM 自然语言控制
    if args.prompt:
        from packages.ai_director import CreativeDirector
        director = CreativeDirector()
        instructions = director.interpret_prompt(args.prompt)
        if instructions:
            # 从 LLM 指令中提取参数
            style_map = {"intense": "intense", "calm": "calm", "dynamic": "dynamic"}
            llm_speed = instructions.get("pacing", {}).get("speed", "dynamic")
            args.style = style_map.get(llm_speed, args.style)
            print(f"  AI 导演建议风格: {args.style}")

            # 自动设置调色
            effects = instructions.get("effects", {})
            color_preset = effects.get("color_grading", "")
            if color_preset and color_preset != "neutral":
                if "color-grade" not in args.enhance:
                    args.enhance.append("color-grade")
                print(f"  AI 导演建议调色: {color_preset}")

            # 自动设置字幕
            if args.subtitles == "none" and instructions.get("subtitles"):
                args.subtitles = instructions["subtitles"]

            # 自动设置目标时长
            target_dur = instructions.get("constraints", {}).get("target_duration_sec")
            if target_dur:
                print(f"  AI 导演建议时长: {target_dur}s")

    # 配音旁白：生成脚本 → TTS 合成
    narration_audio = None
    if args.narrate or args.script:
        from packages.voice_engine import ScriptWriter, TtsEngine

        writer = ScriptWriter()
        if args.script:
            lines = writer.from_file(args.script)
        else:
            lines = writer.write(args.narrate, target_sec=args.narrate_sec)
        script_text = ScriptWriter.to_text(lines)
        print(f"\n  旁白脚本 {len(lines)} 句，预计 {ScriptWriter.total_duration(lines)}s")
        for ln in lines:
            print(f"    · {ln.text}")

        if TtsEngine.available():
            try:
                tts = TtsEngine(voice=args.voice)
                narration_path = str(OUTPUT_DIR / "narration.mp3")
                tts.synthesize(script_text, narration_path)
                narration_audio = narration_path
                print(f"  旁白音频: {narration_path}（音色: {tts.voice}）")
            except Exception as e:
                print(f"  配音合成失败（跳过配音）: {e}")
        else:
            print("  未安装 edge-tts，跳过配音（pip install edge-tts）")

    # 运行 pipeline
    pipeline = MontagePipeline()
    color_preset = getattr(args, '_color_preset', None)
    enable_harmonize = getattr(args, '_enable_harmonize', False)
    enable_ducking = getattr(args, '_enable_ducking', False)
    enable_reframe = getattr(args, '_enable_reframe', False)
    reframe_aspect = _parse_ratio(getattr(args, "reframe_aspect", "16:9"), default=16 / 9)
    reframe_method = getattr(args, "reframe_method", "subject")

    result_path = pipeline.run(
        video_paths, bgm_path, args.style, args.output,
        threshold=args.threshold,
        color_preset=color_preset if color_preset and "color-grade" in args.enhance else None,
        enable_harmonize=enable_harmonize,
        enable_ducking=enable_ducking,
        enable_reframe=enable_reframe,
        reframe_aspect=reframe_aspect,
        reframe_method=reframe_method,
        enable_sfx=args.sfx,
        narration_audio=narration_audio,
        narration_duck=not args.no_duck,
        min_shot_gap=args.min_shot_gap,
        visual_dedup_threshold=args.visual_dedup_threshold,
    )

    # 后处理：视频增强（只处理 pipeline.run() 未处理的项目）
    post_enhance = [e for e in args.enhance if e != "color-grade" or not color_preset]
    if post_enhance:
        _apply_enhancement(result_path, post_enhance, color_preset,
                           reframe_aspect=reframe_aspect, reframe_method=reframe_method)

    # 后处理：字幕压制（--translate 时输出双语字幕）
    sub_style = args.subtitles
    if args.translate and sub_style == "none":
        sub_style = "bilingual"
    if sub_style != "none":
        _apply_subtitles(
            result_path, sub_style, args.subtitle_model,
            translate=args.translate,
            source_lang=args.subtitle_lang or "auto",
            translate_backend=args.translate_backend,
        )

    # 后处理：变速曲线
    if args.speed is not None or args.speed_curve:
        try:
            from packages.montage_engine import SpeedCurve, apply_speed_curve, probe_duration

            dur = probe_duration(result_path)
            if args.speed is not None:
                curve = SpeedCurve.constant(dur, args.speed)
                label = f"整段 {args.speed}x"
            else:
                curve = SpeedCurve.from_preset(args.speed_curve, dur, steps=args.speed_steps)
                label = f"曲线 {args.speed_curve}"
            print(f"\n  应用变速（{label}）...")
            print(f"  {curve.describe()}")
            sped_path = result_path.replace(".mp4", "_speed.mp4")
            apply_speed_curve(result_path, sped_path, curve)
            if Path(sped_path).exists():
                result_path = sped_path
        except Exception as e:
            print(f"  变速失败（跳过）: {e}")

    # 后处理：多轨混音（人声/BGM/音效）
    if args.audio_mix:
        try:
            from packages.audio_mixer import AudioMixer, AudioTrack, mix_tracks_from_spec

            spec_path = Path(args.audio_mix)
            if not spec_path.exists():
                print(f"  混音计划不存在: {args.audio_mix}")
            else:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                spec.setdefault("video", result_path)
                # 先把 spec 里没有的时长补成视频时长，避免 loop 轨无限长
                if not spec.get("duration"):
                    try:
                        spec["duration"] = _probe_media_duration(result_path)
                    except Exception:
                        pass
                mixer = AudioMixer(
                    sample_rate=int(spec.get("sample_rate", 44100)),
                    channels=int(spec.get("channels", 2)),
                    duck_db=float(spec.get("duck_db", -12.0)),
                )
                tracks = [AudioTrack.from_dict(d) for d in spec.get("tracks", [])]
                print(f"\n  多轨混音（{len(tracks)} 轨）...")
                print(mixer.describe(tracks))
                mixed_path = result_path.replace(".mp4", "_mixed.mp4")
                mix_tracks_from_spec({**spec, "video": result_path}, mixed_path)
                if Path(mixed_path).exists():
                    import os
                    os.replace(mixed_path, result_path)
                    print("  混音完成!")
        except Exception as e:
            print(f"  多轨混音失败（跳过）: {e}")

    # 后处理：封面生成
    if args.cover:
        from packages.cover_engine import generate_cover

        cover_title = args.cover_title or args.prompt or Path(args.output).stem
        cover_out = str(Path(args.output).with_suffix("")) + f"_cover.png"
        print("\n  生成封面...")
        generate_cover(
            result_path,
            title=cover_title,
            subtitle=args.cover_subtitle,
            size=args.cover_size,
            style=args.cover_style,
            out_path=cover_out,
        )

    # 导出时间轴
    if args.export_timeline:
        _export_timeline(pipeline, args.export_timeline)


def _apply_enhancement(video_path: str, enhance_options: list, color_preset: str = None,
                       reframe_aspect: float = 16 / 9, reframe_method: str = "subject"):
    """对输出视频应用增强"""
    from packages.video_enhancement import enhance_video, stabilize_video, apply_color_grade

    temp_path = video_path + ".enhanced.mp4"
    enhanced = False

    if "stabilize" in enhance_options:
        print("\n  应用防抖...")
        stabilize_video(video_path, temp_path)
        import shutil
        shutil.move(temp_path, video_path)
        enhanced = True

    if "denoise" in enhance_options:
        print("  应用降噪...")
        enhance_video(video_path, temp_path, denoise=True)
        import shutil
        shutil.move(temp_path, video_path)
        enhanced = True

    if "color-grade" in enhance_options:
        preset = color_preset or "cinematic"
        print(f"  应用调色: {preset}")
        apply_color_grade(video_path, temp_path, preset=preset)
        import shutil
        shutil.move(temp_path, video_path)
        enhanced = True

    if "auto-reframe" in enhance_options:
        _label = _ratio_suffix(reframe_aspect).lstrip("_").replace("x", ":")
        print(f"  应用自动裁切 (目标 {_label} / {reframe_method})...")
        from packages.video_enhancement import auto_reframe
        auto_reframe(video_path, temp_path,
                     target_aspect=reframe_aspect, method=reframe_method)
        if Path(temp_path).exists():
            import shutil
            shutil.move(temp_path, video_path)
            enhanced = True

    if enhanced:
        print("  增强完成!")


def _apply_subtitles(
    video_path: str, style: str, model_size: str = "base",
    translate: str = None, source_lang: str = "auto",
    translate_backend: str = "auto",
):
    """对输出视频应用字幕（转录 + 可选翻译 + 压制）

    karaoke 等词级风格由 transcribe_and_burn 自动改用 JSON 中间格式，
    以保留逐字时间戳；bilingual / --translate 走 ASS 双行排版。
    """
    try:
        from packages.subtitle_engine import transcribe_and_burn

        extra = f", 翻译->{translate}" if translate else ""
        print(f"\n  生成字幕 (风格: {style}, 模型: {model_size}{extra})...")

        out_path = video_path.replace(".mp4", "_subtitled.mp4")
        transcribe_and_burn(
            video_path, style=style, output_path=out_path, model_size=model_size,
            translate=translate, source_lang=source_lang,
            translate_backend=translate_backend,
        )

        # 替换原文件
        import shutil
        shutil.move(out_path, video_path)
        print(f"  字幕烧录完成!")
    except ImportError as e:
        print(f"  字幕功能需要安装 whisper: pip install openai-whisper")
        print(f"  错误: {e}")
    except Exception as e:
        print(f"  字幕处理失败: {e}")


def _export_timeline(pipeline, format: str):
    """导出时间轴"""
    # OTIO 使用专用导出器
    if format == "otio":
        from packages.timeline_export.src.otio_exporter import export_otio_from_pipeline
        output_path = f"{pipeline.output_dir}/timeline.otio"
        export_otio_from_pipeline(pipeline, output_path)
        return

    from packages.timeline_export import TimelineExporter, Timeline, Clip

    # 从 pipeline 中收集的时间线数据构建 Timeline 对象
    timeline_entries = getattr(pipeline, '_last_timeline', [])
    bgm_path = getattr(pipeline, '_last_bgm_path', '')
    total_duration = getattr(pipeline, '_last_total_duration', 0.0)

    if not timeline_entries:
        print(f"\n  导出时间轴: 无时间线数据（需要先运行 pipeline）")
        return

    # 转换为 Clip 对象
    clips = []
    for entry in timeline_entries:
        clips.append(Clip(
            source_path=getattr(entry, 'shot_path', ''),
            start_time=getattr(entry, 'start_time', 0.0),
            duration=getattr(entry, 'duration', 0.0),
            timeline_start=getattr(entry, 'start_time', 0.0),
        ))

    timeline = Timeline(
        clips=clips,
        audio_path=bgm_path,
        total_duration=total_duration,
    )

    exporter = TimelineExporter(output_dir=pipeline.output_dir)
    exported = exporter.export(timeline, formats=[format])

    for fmt, path in exported.items():
        print(f"  导出 {fmt.upper()}: {path}")


def _crawl_videos(keyword: str, source: str, clip_limit: int, parser) -> list:
    """根据来源爬取视频"""
    if source == "bilibili":
        from packages.video_crawler.src.bilibili_crawler import BilibiliCrawler
        crawler = BilibiliCrawler()
    elif source in ("youtube", "dailymotion", "douyin", "ixigua", "acfun", "vimeo"):
        from packages.video_crawler.src.ytdlp_crawler import create_crawler
        try:
            crawler = create_crawler(source)
        except ValueError as e:
            parser.error(str(e))
    elif source in ("yarn", "playphrase", "quodb", "zhaotaici"):
        from packages.video_crawler.src.quote_crawler import create_quote_crawler
        try:
            crawler = create_quote_crawler(source)
        except ValueError as e:
            parser.error(str(e))
    else:
        parser.error(f"不支持的素材来源: {source}")

    paths = crawler.search_and_download(keyword, max_clips=clip_limit)
    if not paths:
        print("错误: 未下载到任何视频")
    return paths


if __name__ == "__main__":
    main()
