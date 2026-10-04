"""
WebUI 模块 - FastAPI + HTML 前端

功能：
- 关键词输入 → 选源 → 上传BGM → 选风格/质量/比例 → 提交 → 进度 → 下载
- 支持本地视频上传和在线搜索
- SSE 实时进度推送
"""

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="AI Montage Agent", version="1.0.0")

# 任务存储
_tasks = {}

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("output")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def _run_montage_task(task_id: str, video_paths: list, bgm_path: str, bgm_query: str,
                      style: str, output_name: str, query: str, source: str, clip_limit: int,
                      color_preset: str = None, stabilize: bool = False,
                      aspect_ratio: str = "16/9", enable_subtitles: bool = False,
                      subtitle_style: str = "tiktok", subtitle_lang: str = "auto",
                      translate_lang: str = "",
                      color_grade: str = "none", enable_ducking: bool = False,
                      enable_harmonize: bool = False, enhance_options: list = None,
                      transition_pattern_id: str = "auto"):
    """后台运行完整流程：搜索下载 → pipeline → 后处理"""
    import sys
    import traceback
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from pipeline import MontagePipeline

    task = _tasks[task_id]
    try:
        # ===== 阶段 1：BGM 搜索下载 =====
        if bgm_query:
            task["status"] = "running"
            task["progress"] = 3
            task["message"] = f"正在搜索 BGM: {bgm_query}..."
            from packages.video_crawler.src.bgm_crawler import BgmCrawler
            bgm_crawler = BgmCrawler()
            bgm_paths = bgm_crawler.search_and_download(bgm_query, max_clips=1)
            if not bgm_paths:
                task["status"] = "error"
                task["message"] = f"未找到 BGM「{bgm_query}」，请换个关键词试试"
                return
            bgm_path = bgm_paths[0]
            task["progress"] = 8
            task["message"] = "BGM 下载完成 ✓"
        elif not bgm_path:
            task["status"] = "error"
            task["message"] = "请提供 BGM 文件或搜索关键词"
            return

        # ===== 阶段 2：视频素材搜索下载 =====
        if query:
            task["status"] = "running"
            task["progress"] = 10
            task["message"] = f"正在搜索素材: {query}（来源: {source}）..."

            try:
                if source in ("playphrase", "quodb"):
                    from packages.video_crawler.src.quote_crawler import create_quote_crawler
                    crawler = create_quote_crawler(source)
                    if source == "playphrase":
                        task["message"] = "正在启动浏览器搜索 PlayPhrase...首次可能较慢"
                    else:
                        task["message"] = "正在搜索 QuoDB 台词库..."
                elif source == "bilibili":
                    from packages.video_crawler.src.bilibili_crawler import BilibiliCrawler
                    crawler = BilibiliCrawler()
                    task["message"] = "正在搜索 B站视频..."
                elif source == "youtube":
                    from packages.video_crawler.src.ytdlp_crawler import YtdlpCrawler
                    crawler = YtdlpCrawler("youtube")
                    task["message"] = "正在搜索 YouTube（需要代理）..."
                elif source == "dailymotion":
                    from packages.video_crawler.src.ytdlp_crawler import YtdlpCrawler
                    crawler = YtdlpCrawler("dailymotion")
                    task["message"] = "正在搜索 Dailymotion..."
                else:
                    from packages.video_crawler.src.bilibili_crawler import BilibiliCrawler
                    crawler = BilibiliCrawler()
                    task["message"] = f"正在搜索 {source}..."

                task["progress"] = 12
                video_paths = crawler.search_and_download(query, max_clips=clip_limit)

                if not video_paths:
                    task["status"] = "error"
                    task["message"] = f"搜索「{query}」未找到结果，请检查关键词或换一个来源试试"
                    return

                task["progress"] = 25
                task["message"] = f"素材下载完成，共 {len(video_paths)} 个片段 ✓"

            except Exception as e:
                task["status"] = "error"
                task["message"] = f"素材搜索失败: {str(e)}"
                print(f"[Crawler Error] task={task_id}, source={source}")
                traceback.print_exc()
                return

        if not video_paths:
            task["status"] = "error"
            task["message"] = "没有可用的视频文件，请检查搜索关键词"
            return

        # ===== 阶段 3：AI 混剪 pipeline =====
        task["status"] = "running"
        task["progress"] = 30
        task["message"] = f"正在分析 {len(video_paths)} 个素材，检测镜头与节拍..."

        # 解析输出比例
        enable_reframe = (aspect_ratio != "16/9")
        reframe_map = {"9/16": 9/16, "1/1": 1/1, "4/5": 4/5, "16/9": 16/9}
        reframe_aspect = reframe_map.get(aspect_ratio, 9/16)

        # 合并色彩分级：高级设置优先，其次用风格模板的
        final_color_grade = color_grade if color_grade and color_grade != "none" else color_preset

        # 加载转场模板
        from packages.montage_engine.src.transition_patterns import get_pattern
        trans_pattern = get_pattern(transition_pattern_id) if transition_pattern_id != "auto" else None

        pipeline = MontagePipeline(cache_dir="cache", output_dir="output")
        result = pipeline.run(
            video_paths, bgm_path, style, output_name,
            threshold=0.2,
            color_preset=final_color_grade if final_color_grade != "none" else None,
            enable_reframe=enable_reframe,
            reframe_aspect=reframe_aspect,
            enable_ducking=enable_ducking,
            enable_harmonize=enable_harmonize,
            transition_pattern=trans_pattern,
        )

        # ===== 阶段 4：后处理 =====
        import os

        # 防抖（来自风格模板）
        if stabilize:
            task["progress"] = 88
            task["message"] = "正在应用视频防抖..."
            try:
                from packages.video_enhancement.src.stabilizer import stabilize_video
                temp_path = result + ".stab.mp4"
                stabilize_video(result, temp_path)
                if os.path.exists(temp_path):
                    os.replace(temp_path, result)
            except Exception as e:
                print(f"[Stabilize] 跳过: {e}")

        # 视频增强（降噪/锐化/胶片颗粒）
        if enhance_options:
            try:
                from packages.video_enhancement.src.enhancer import enhance_video
                task["progress"] = 90
                task["message"] = f"正在增强视频: {', '.join(enhance_options)}..."
                temp_path = result + ".enhanced.mp4"
                enhance_video(result, temp_path, denoise="denoise" in enhance_options,
                              sharpen="sharpen" in enhance_options, film_grain="grain" in enhance_options)
                if os.path.exists(temp_path):
                    os.replace(temp_path, result)
            except Exception as e:
                print(f"[Enhance] 跳过: {e}")

        # 字幕
        if enable_subtitles:
            task["progress"] = 93
            task["message"] = "正在生成字幕（语音识别中，可能较慢）..."
            try:
                from packages.subtitle_engine.src.burn_pipeline import transcribe_and_burn
                lang = None if subtitle_lang == "auto" else subtitle_lang
                translate = translate_lang or None
                if translate:
                    task["message"] = f"正在生成双语字幕（翻译->{translate}）..."
                temp_path = result + ".subtitled.mp4"
                transcribe_and_burn(
                    result, style=subtitle_style, output_path=temp_path, language=lang,
                    translate=translate,
                )
                if os.path.exists(temp_path):
                    os.replace(temp_path, result)
            except Exception as e:
                print(f"[Subtitles] 跳过: {e}")

        task["status"] = "done"
        task["progress"] = 100
        task["message"] = "混剪完成!"
        task["output_path"] = result

    except Exception as e:
        task["status"] = "error"
        task["message"] = f"混剪失败: {str(e)}"
        print(f"[Pipeline Error] task={task_id}")
        traceback.print_exc()


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/styles")
async def list_styles():
    """获取可用风格模板列表"""
    from packages.video_enhancement.src.style_templates import get_style_names
    return get_style_names()


@app.get("/api/transitions")
async def list_transitions():
    """获取可用转场模板列表"""
    from packages.montage_engine.src.transition_patterns import list_patterns
    return list_patterns()


@app.post("/api/upload/video")
async def upload_video(files: list[UploadFile] = File(...)):
    """上传视频文件"""
    saved = []
    for f in files:
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex}_{f.filename}"
        with open(dest, "wb") as fh:
            shutil.copyfileobj(f.file, fh)
        saved.append(str(dest))
    return {"files": saved}


@app.post("/api/upload/bgm")
async def upload_bgm(file: UploadFile = File(...)):
    """上传 BGM 文件"""
    dest = UPLOAD_DIR / f"bgm_{uuid.uuid4().hex}_{file.filename}"
    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return {"path": str(dest)}


@app.post("/api/montage")
async def create_montage(
    background_tasks: BackgroundTasks,
    video_paths: str = Form(...),        # JSON 数组字符串
    bgm_path: Optional[str] = Form(None),
    bgm_query: Optional[str] = Form(None),
    style: str = Form("dynamic"),
    style_preset: Optional[str] = Form(None),
    output_name: str = Form("final.mp4"),
    query: Optional[str] = Form(None),
    source: str = Form("playphrase"),
    clip_limit: int = Form(20),
    # 高级设置
    aspect_ratio: str = Form("16/9"),
    enable_subtitles: str = Form("false"),
    subtitle_style: str = Form("tiktok"),
    subtitle_lang: str = Form("auto"),
    translate_lang: str = Form(""),
    color_grade: str = Form("none"),
    enable_ducking: str = Form("false"),
    enable_harmonize: str = Form("false"),
    enhance_options: str = Form("[]"),
    transition_pattern: str = Form("auto"),
):
    """创建混剪任务 — 立即返回 task_id，搜索下载在后台进行"""
    task_id = uuid.uuid4().hex
    _tasks[task_id] = {
        "id": task_id,
        "status": "pending",
        "progress": 0,
        "message": "任务已创建，正在准备...",
        "output_path": None,
    }

    # 解析本地上传的视频路径
    video_paths_list = json.loads(video_paths) if video_paths else []

    # 风格预设处理
    color_preset = None
    stabilize = False
    if style_preset:
        from packages.video_enhancement.src.style_templates import get_pipeline_params
        preset_params = get_pipeline_params(style_preset)
        style = preset_params.get("style", style)
        color_preset = preset_params.get("color_preset")
        stabilize = preset_params.get("stabilize", False)

    # 解析高级设置
    _enhance = json.loads(enhance_options) if enhance_options else []

    # 立即返回 task_id，所有耗时操作在后台执行
    background_tasks.add_task(
        _run_montage_task, task_id, video_paths_list, bgm_path, bgm_query,
        style, output_name, query, source, clip_limit, color_preset, stabilize,
        aspect_ratio, enable_subtitles == "true", subtitle_style, subtitle_lang,
        translate_lang,
        color_grade, enable_ducking == "true", enable_harmonize == "true", _enhance,
        transition_pattern,
    )

    return {"task_id": task_id}


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    """查询任务状态"""
    if task_id not in _tasks:
        return {"error": "任务不存在"}
    return _tasks[task_id]


@app.get("/api/task/{task_id}/progress")
async def task_progress(task_id: str):
    """SSE 实时进度"""
    async def event_generator():
        while True:
            if task_id not in _tasks:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                break
            task = _tasks[task_id]
            yield f"data: {json.dumps(task)}\n\n"
            if task["status"] in ("done", "error"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/download/{task_id}")
async def download_result(task_id: str):
    """下载结果视频"""
    if task_id not in _tasks:
        return {"error": "任务不存在"}
    task = _tasks[task_id]
    if task["status"] != "done" or not task["output_path"]:
        return {"error": "任务未完成"}
    return FileResponse(task["output_path"], media_type="video/mp4", filename="montage.mp4")


# --------------------------------------------------------------- 预览相关
@app.get("/api/task/{task_id}/proxy")
async def task_proxy(task_id: str, width: int = 480, crf: int = 32,
                     max_duration: Optional[float] = None, refresh: int = 0):
    """低码率代理视频（缓存）。用于前端秒开播放。"""
    if task_id not in _tasks:
        return {"error": "任务不存在"}
    task = _tasks[task_id]
    if task["status"] != "done" or not task["output_path"]:
        return {"error": "任务未完成"}
    try:
        from .preview import get_proxy
        info = get_proxy(task["output_path"], width=width, crf=crf,
                         max_duration=max_duration, force=bool(refresh))
        task.setdefault("proxy", {})["info"] = info
        return {
            "status": "ok",
            "url": f"/api/task/{task_id}/proxy/video?width={width}&crf={crf}",
            **info,
        }
    except Exception as e:
        return {"error": f"代理生成失败: {e}"}


@app.get("/api/task/{task_id}/proxy/video")
async def task_proxy_video(task_id: str, width: int = 480, crf: int = 32,
                           max_duration: Optional[float] = None):
    """直接返回代理视频文件（未生成则现场生成）。"""
    if task_id not in _tasks:
        return {"error": "任务不存在"}
    task = _tasks[task_id]
    if task["status"] != "done" or not task["output_path"]:
        return {"error": "任务未完成"}
    try:
        from .preview import get_proxy
        info = get_proxy(task["output_path"], width=width, crf=crf,
                         max_duration=max_duration)
    except Exception as e:
        return {"error": f"代理生成失败: {e}"}
    return FileResponse(info["path"], media_type="video/mp4", filename="preview.mp4")


@app.post("/api/task/{task_id}/preview")
async def create_param_preview(
    task_id: str,
    start: float = Form(0.0),
    duration: float = Form(6.0),
    color_grade: str = Form("none"),
    subtitle_style: str = Form("none"),
    width: int = Form(480),
    crf: int = Form(32),
):
    """按当前参数快速渲染一小段预览（默认前 6 秒），用于"边调参边看"。"""
    if task_id not in _tasks:
        return {"error": "任务不存在"}
    task = _tasks[task_id]
    if task["status"] != "done" or not task["output_path"]:
        return {"error": "任务未完成"}
    try:
        from .preview import get_param_preview
        # 若任务侧已生成过字幕文件，则预览也带上字幕
        sub_path = None
        if subtitle_style and subtitle_style != "none":
            cand = Path(task["output_path"]).with_suffix(".srt")
            if cand.exists():
                sub_path = str(cand)
        info = get_param_preview(
            task["output_path"], start=start, duration=duration, width=width,
            crf=crf, color_preset=None if color_grade in ("none", "") else color_grade,
            subtitle_path=sub_path,
        )
        previews = task.setdefault("previews", [])
        pid = f"p{len(previews) + 1}"
        previews.append({"id": pid, **info, "params": {
            "start": start, "duration": duration, "color_grade": color_grade,
            "subtitle_style": subtitle_style}})
        return {
            "status": "ok", "preview_id": pid,
            "url": f"/api/task/{task_id}/preview/{pid}",
            **info,
        }
    except Exception as e:
        return {"error": f"预览生成失败: {e}"}


@app.get("/api/task/{task_id}/preview/{preview_id}")
async def get_param_preview_file(task_id: str, preview_id: str):
    """取回参数预览文件。"""
    if task_id not in _tasks:
        return {"error": "任务不存在"}
    for p in _tasks[task_id].get("previews", []):
        if p["id"] == preview_id and Path(p["path"]).exists():
            return FileResponse(p["path"], media_type="video/mp4", filename="preview.mp4")
    return {"error": "预览不存在"}


@app.post("/api/preview/clear")
async def clear_preview_cache():
    """清空预览缓存。"""
    try:
        from .preview import clear_cache
        n = clear_cache()
        return {"status": "ok", "removed": n}
    except Exception as e:
        return {"error": str(e)}


def start_webui(host: str = "0.0.0.0", port: int = 8000):
    """启动 WebUI"""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_webui()
