"""
CLI Tool（旧版 Typer CLI）

已不是主入口。新工作请使用仓库根目录的 `pipeline.py`：
    python pipeline.py --movies a.mp4 --bgm bgm.mp3
    python pipeline.py --webui
旧命令保留用于兼容，后续版本会移除。
"""

import typer
from pathlib import Path
from typing import List, Optional
import json
import sys

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "packages"))

from video_understanding.src.shot_detector import ShotDetector
from beat_engine.src.beat_detector import BeatDetector
from beat_engine.src.beat_sync_engine import BeatSyncEngine
from video_understanding.src.highlight_scorer import HighlightScorer
from timeline_engine.src.timeline_planner import TimelinePlanner
from montage_engine.src.video_composer import VideoComposer
from render_engine.src.ffmpeg_executor import FFmpegExecutor

app = typer.Typer(
    name="ai-montage",
    help="AI自动影视混剪Agent CLI"
)

@app.command()
def detect_shots(
    video_path: str = typer.Argument(..., help="视频文件路径"),
    output_dir: str = typer.Option("./shots", help="输出目录"),
    method: str = typer.Option("content", help="检测方法"),
    threshold: float = typer.Option(27.0, help="检测阈值")
):
    """检测视频镜头"""

    typer.echo(f"检测视频镜头: {video_path}")

    detector = ShotDetector(output_dir)
    shots = detector.detect_shots(video_path, method, threshold)

    typer.echo(f"检测到 {len(shots)} 个镜头")

    # 保存结果
    output_file = Path(output_dir) / "shots.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump([s.to_dict() for s in shots], f, indent=2, ensure_ascii=False)

    typer.echo(f"结果已保存到: {output_file}")

@app.command()
def analyze_beats(
    audio_path: str = typer.Argument(..., help="音频文件路径"),
    output_file: str = typer.Option("beats.json", help="输出文件")
):
    """分析BGM节拍"""

    typer.echo(f"分析BGM节拍: {audio_path}")

    detector = BeatDetector()
    result = detector.analyze(audio_path)

    typer.echo(f"检测到 {len(result['beats'])} 个节拍")
    typer.echo(f"BPM: {result['tempo']:.1f}")

    # 保存结果
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    typer.echo(f"结果已保存到: {output_file}")

@app.command()
def score_highlights(
    shots_file: str = typer.Argument(..., help="镜头数据文件"),
    output_file: str = typer.Option("highlights.json", help="输出文件")
):
    """评分高光镜头"""

    typer.echo(f"评分高光镜头: {shots_file}")

    # 加载镜头数据
    with open(shots_file, 'r', encoding='utf-8') as f:
        shots_data = json.load(f)

    # 评分
    scorer = HighlightScorer()
    scores = scorer.score_shots(shots_data)

    # 归一化分数
    scores = scorer.normalize_scores(scores)

    # 获取高光镜头
    highlights = scorer.get_highlight_shots(scores, top_k=20)

    typer.echo(f"找到 {len(highlights)} 个高光镜头")

    # 保存结果
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump([s.to_dict() for s in scores], f, indent=2, ensure_ascii=False)

    typer.echo(f"结果已保存到: {output_file}")

@app.command()
def sync_beats(
    shots_file: str = typer.Argument(..., help="镜头数据文件"),
    beats_file: str = typer.Argument(..., help="节拍数据文件"),
    output_file: str = typer.Option("timeline.json", help="输出文件"),
    style: str = typer.Option("dynamic", help="同步风格")
):
    """卡点同步"""

    typer.echo(f"卡点同步: {shots_file} + {beats_file}")

    # 加载数据
    with open(shots_file, 'r', encoding='utf-8') as f:
        shots = json.load(f)

    with open(beats_file, 'r', encoding='utf-8') as f:
        beats_data = json.load(f)

    # 同步
    engine = BeatSyncEngine()
    timeline = engine.sync(
        shots,
        beats_data["beats"],
        beats_data.get("climax"),
        style
    )

    typer.echo(f"生成 {len(timeline)} 个时间线条目")

    # 导出时间线
    engine.export_timeline(timeline, output_file)

    typer.echo(f"时间线已保存到: {output_file}")

@app.command()
def plan_timeline(
    shots_file: str = typer.Argument(..., help="镜头数据文件"),
    output_file: str = typer.Option("planned_timeline.json", help="输出文件"),
    style: str = typer.Option("dynamic", help="规划风格"),
    target_duration: Optional[float] = typer.Option(None, help="目标时长(秒)")
):
    """规划时间轴"""

    typer.echo(f"规划时间轴: {shots_file}")

    # 加载镜头数据
    with open(shots_file, 'r', encoding='utf-8') as f:
        shots = json.load(f)

    # 规划
    planner = TimelinePlanner()
    result = planner.plan(shots, style, target_duration)

    typer.echo(f"规划完成")
    typer.echo(f"- 镜头数量: {len(result['shots'])}")
    typer.echo(f"- 风格: {result['style']}")

    # 保存结果
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    typer.echo(f"规划结果已保存到: {output_file}")

@app.command()
def compose(
    timeline_file: str = typer.Argument(..., help="时间线文件"),
    output_path: str = typer.Option("output.mp4", help="输出文件"),
    bgm_path: Optional[str] = typer.Option(None, help="BGM文件路径")
):
    """合成视频"""

    typer.echo(f"合成视频: {timeline_file}")

    # 加载时间线
    with open(timeline_file, 'r', encoding='utf-8') as f:
        timeline_data = json.load(f)

    timeline = timeline_data.get("shots", timeline_data.get("entries", []))

    # 合成
    composer = VideoComposer()
    result = composer.compose(
        timeline,
        bgm_path=bgm_path,
        output_path=output_path
    )

    typer.echo(f"视频已生成: {result}")

@app.command()
def full_pipeline(
    video_paths: List[str] = typer.Argument(..., help="视频文件路径列表"),
    bgm_path: str = typer.Option(..., help="BGM文件路径"),
    output_path: str = typer.Option("montage.mp4", help="输出文件"),
    style: str = typer.Option("dynamic", help="风格")
):
    """完整混剪流程"""

    typer.echo("=" * 50)
    typer.echo("AI自动影视混剪Agent")
    typer.echo("=" * 50)

    # 1. 检测镜头
    typer.echo("\n[1/6] 检测镜头...")
    shot_detector = ShotDetector("./temp/shots")
    all_shots = []

    for video_path in video_paths:
        shots = shot_detector.detect_shots(video_path)
        all_shots.extend(shots)

    typer.echo(f"检测到 {len(all_shots)} 个镜头")

    # 2. 分析BGM
    typer.echo("\n[2/6] 分析BGM...")
    beat_detector = BeatDetector()
    beats_result = beat_detector.analyze(bgm_path)

    typer.echo(f"BPM: {beats_result['tempo']:.1f}")
    typer.echo(f"检测到 {len(beats_result['beats'])} 个节拍")

    # 3. 评分高光
    typer.echo("\n[3/6] 评分高光...")
    scorer = HighlightScorer()
    shots_data = [s.to_dict() for s in all_shots]
    scores = scorer.score_shots(shots_data)
    scores = scorer.normalize_scores(scores)

    # 4. 卡点同步
    typer.echo("\n[4/6] 卡点同步...")
    sync_engine = BeatSyncEngine()
    timeline = sync_engine.sync(
        [s.to_dict() for s in scores],
        beats_result["beats"],
        beats_result.get("climax"),
        style
    )

    # 5. 规划时间轴
    typer.echo("\n[5/6] 规划时间轴...")
    planner = TimelinePlanner()
    planned = planner.plan(
        [entry.to_dict() for entry in timeline],
        style
    )

    # 6. 合成视频
    typer.echo("\n[6/6] 合成视频...")
    composer = VideoComposer()
    result = composer.compose(
        planned["shots"],
        bgm_path=bgm_path,
        output_path=output_path
    )

    typer.echo("\n" + "=" * 50)
    typer.echo("混剪完成!")
    typer.echo(f"输出文件: {result}")
    typer.echo("=" * 50)

@app.command()
def version():
    """显示版本信息"""
    typer.echo("AI Montage Agent v0.1.0")

if __name__ == "__main__":
    app()
