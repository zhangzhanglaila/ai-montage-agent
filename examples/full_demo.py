"""
AI Montage Agent 完整功能演示

本示例展示所有新整合的功能：
1. 视频增强（防抖/降噪/调色）
2. 字幕自动生成与压制
3. LLM 自然语言控制
4. 时间轴导出
5. WebUI
"""

import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))


def demo_video_enhancement():
    """演示视频增强功能"""
    print("=" * 50)
    print("1. 视频增强演示")
    print("=" * 50)

    from packages.video_enhancement import (
        DenoiseConfig, SharpenConfig, FilmGrainConfig,
        enhance_video, stabilize_video,
        ColorGradeConfig, apply_color_grade, ColorGradePreset,
    )

    # 降噪
    print("\n  降噪配置:")
    cfg = DenoiseConfig(temporal_strength=0.5, spatial_strength=0.3)
    print(f"    FFmpeg 滤镜: {cfg.to_ffmpeg_filter()}")

    # 锐化
    print("\n  锐化配置:")
    cfg = SharpenConfig(amount=0.4, radius=1.5)
    print(f"    FFmpeg 滤镜: {cfg.to_ffmpeg_filter()}")

    # 调色预设
    print("\n  可用调色预设:")
    for preset in ColorGradePreset:
        cfg = ColorGradeConfig(preset=preset.value)
        print(f"    {preset.value:20s} → {cfg.preset}")

    # 胶片颗粒
    print("\n  胶片颗粒配置:")
    cfg = FilmGrainConfig(grain_type="35mm", intensity=0.3)
    print(f"    FFmpeg 滤镜: {cfg.to_ffmpeg_filter()}")

    # 防抖预设
    from packages.video_enhancement import PROFILES
    print("\n  防抖预设:")
    for name, prof in PROFILES.items():
        print(f"    {name:20s} → shakiness={prof.shakiness}, smoothing={prof.smoothing}")


def demo_subtitle_engine():
    """演示字幕功能"""
    print("\n" + "=" * 50)
    print("2. 字幕引擎演示")
    print("=" * 50)

    from packages.subtitle_engine import (
        CaptionStyle, CaptionBurner, burn_captions,
        Transcriber,
    )

    # 字幕风格
    print("\n  可用字幕风格:")
    for style in CaptionStyle:
        print(f"    {style.value}")

    # 创建 burner
    burner = CaptionBurner(style=CaptionStyle.TIKTOK)
    print(f"\n  TikTok 风格配置:")
    print(f"    字号: {burner.config.fontsize}")
    print(f"    颜色: {burner.config.fontcolor}")
    print(f"    位置: {burner.config.y_expr}")

    # 转录器
    print("\n  转录器:")
    print("    后端: whisper / faster_whisper")
    print("    模型: tiny/base/small/medium/large")
    print("    输出: srt / vtt / json")


def demo_ai_director():
    """演示 LLM 自然语言控制"""
    print("\n" + "=" * 50)
    print("3. AI 导演演示")
    print("=" * 50)

    from packages.ai_director import CreativeDirector

    director = CreativeDirector()

    # 测试启发式检测
    test_prompts = [
        "做一个30秒的漫威高燃混剪",
        "平静的纪录片风格",
        "复古怀旧的视频",
        "好莱坞大片风格",
    ]

    print("\n  启发式风格检测:")
    for prompt in test_prompts:
        # 使用默认指令（不需要 LLM）
        instructions = director.get_default_instructions(prompt)
        color = instructions["effects"]["color_grading"]
        speed = instructions["pacing"]["speed"]
        print(f"    '{prompt}'")
        print(f"      → 节奏: {speed}, 调色: {color}")

    # LLM 模式（需要 Ollama 或 OpenAI API）
    print("\n  LLM 模式（需要配置 API）:")
    print("    设置 OPENAI_API_BASE=http://localhost:11434/v1")
    print("    设置 LLM_MODEL=qwen2.5:7b")
    print("    然后调用: director.interpret_prompt(prompt)")


def demo_timeline_export():
    """演示时间轴导出"""
    print("\n" + "=" * 50)
    print("4. 时间轴导出演示")
    print("=" * 50)

    from packages.timeline_export import Clip, Timeline, TimelineExporter

    # 创建示例时间轴
    clips = [
        Clip(source_path="shot_001.mp4", start_time=0.0, duration=2.5, timeline_start=0.0),
        Clip(source_path="shot_002.mp4", start_time=0.0, duration=1.8, timeline_start=2.5),
        Clip(source_path="shot_003.mp4", start_time=0.0, duration=3.0, timeline_start=4.3),
    ]

    timeline = Timeline(
        clips=clips,
        audio_path="bgm.mp3",
        total_duration=7.3,
        fps=30.0,
        resolution=(1920, 1080),
        project_name="demo_montage",
    )

    print(f"\n  时间轴信息:")
    print(f"    片段数: {len(timeline.clips)}")
    print(f"    总时长: {timeline.total_duration}s")
    print(f"    帧率: {timeline.fps}")
    print(f"    分辨率: {timeline.resolution}")

    print("\n  支持的导出格式:")
    print("    edl  - CMX 3600 EDL（所有NLE通用）")
    print("    xml  - FCP XML v7（DaVinci/Premiere）")
    print("    csv  - 电子表格")
    print("    json - 完整元数据")

    # 导出示例（不需要实际文件）
    exporter = TimelineExporter(output_dir="var/output/demo_export")
    tc = exporter._seconds_to_timecode(65.5, 30.0)
    print(f"\n  时间码转换示例: 65.5秒 → {tc}")


def demo_pipeline_cli():
    """演示 Pipeline CLI 用法"""
    print("\n" + "=" * 50)
    print("5. Pipeline CLI 用法")
    print("=" * 50)

    print("""
    # 基础混剪
    python pipeline.py --movies video.mp4 --bgm bgm.mp3

    # 关键词搜索 + 混剪
    python pipeline.py --query "漫威混剪" --bgm bgm.mp3 --style intense

    # 台词搜索（需要 Playwright）
    python pipeline.py --query "i love you" --source yarn --bgm bgm.mp3
    python pipeline.py --query "i love you" --source playphrase --bgm bgm.mp3
    python pipeline.py --query "我爱你" --source zhaotaici --bgm bgm.mp3

    # LLM 自然语言控制
    python pipeline.py --movies video.mp4 --bgm bgm.mp3 \\
        --prompt "做一个30秒的高燃混剪，橙青调色"

    # 视频增强
    python pipeline.py --movies video.mp4 --bgm bgm.mp3 \\
        --enhance stabilize denoise color-grade

    # 字幕压制
    python pipeline.py --movies video.mp4 --bgm bgm.mp3 \\
        --subtitles tiktok

    # 导出时间轴
    python pipeline.py --movies video.mp4 --bgm bgm.mp3 \\
        --export-timeline edl

    # 启动 WebUI
    python pipeline.py --webui
    """)


def demo_webui():
    """演示 WebUI"""
    print("=" * 50)
    print("6. WebUI 演示")
    print("=" * 50)

    print("""
    启动方式:
      python pipeline.py --webui

    或直接运行:
      python -m packages.webui.src.app

    功能:
      - 关键词输入 → 选源 → 上传BGM → 选风格 → 提交
      - 本地视频上传
      - SSE 实时进度推送
      - 完成后下载

    访问: http://localhost:8000
    """)


if __name__ == "__main__":
    print("AI Montage Agent - 完整功能演示\n")

    demo_video_enhancement()
    demo_subtitle_engine()
    demo_ai_director()
    demo_timeline_export()
    demo_pipeline_cli()
    demo_webui()

    print("\n" + "=" * 50)
    print("所有功能演示完成!")
    print("=" * 50)
