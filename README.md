# AI Montage Agent

<p>
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/FFmpeg-required-green?logo=ffmpeg&logoColor=white" alt="FFmpeg required">
  <img src="https://img.shields.io/badge/WebUI-FastAPI-009688?logo=fastapi&logoColor=white" alt="WebUI FastAPI">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT">
</p>

> **AI 自动影视混剪 Agent** —— 输入视频 + BGM，自动生成卡点混剪。<br>
> **AI-powered auto video-montage agent** — feed it clips + music, get a beat-synced edit.

**语言 / Language:** [简体中文](#简体中文) · [English](#english)

---

# 简体中文

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [参数说明](#参数说明)
- [素材来源](#素材来源)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [许可证](#许可证)

## 功能特性

| 功能 | 说明 | 状态 |
|------|------|------|
| 镜头检测 | FFmpeg scene detect，自动切镜头 | ✅ |
| BGM 节拍分析 | librosa 检测节拍/能量/高潮段 | ✅ |
| 高光评分 | 5 维评分（运动/镜头多样性/表情/运镜/音频） | ✅ |
| 卡点同步 | 镜头与节拍对齐，强弱拍分级 | ✅ |
| 素材搜索 | B站/YouTube/台词搜索等自动下载 | ✅ |
| 风格模板 | 11 种预设风格（Vlog/纪录片/婚礼/游戏等） | ✅ |
| 视频增强 | 防抖/降噪/调色 | ✅ |
| 自动裁剪 | 智能跟踪主体，横屏转竖屏 | ✅ |
| 对白闪避 | BGM 自动避让人声 | ✅ |
| 色彩和谐 | 多片段亮度统一 | ✅ |
| 字幕生成 | Whisper 语音转字幕，5 种风格 | ✅ |
| LLM 控制 | 自然语言描述 → 剪辑指令 | ✅ |
| 时间轴导出 | EDL/CSV/JSON/XML/OTIO | ✅ |
| 丰富转场 | dissolve/zoom/shake/wipe 等 10+ 种 | ✅ |
| WebUI | FastAPI + B站漫画风格前端 | ✅ |

## 快速开始

### 1. 环境要求

- Python 3.10+
- FFmpeg（必须）

### 2. 安装

```bash
# 安装 FFmpeg
# Windows: scoop install ffmpeg / choco install ffmpeg
# Mac:     brew install ffmpeg
# Linux:   sudo apt install ffmpeg

# 安装 Python 依赖
pip install -r requirements.txt
```

### 3. 基础用法

本地视频文件：

```bash
python pipeline.py \
  --movies movie1.mp4 movie2.mp4 \
  --bgm bgm.mp3 \
  --style dynamic \
  --output my_montage.mp4
```

关键词搜索素材（推荐 PlayPhrase，电影原片无水印）：

```bash
python pipeline.py \
  --query "i love you" \
  --bgm bgm.mp3 \
  --style dynamic \
  --output my_montage.mp4
```

B站素材（下载走 playurl API，比 yt-dlp 稳；可能有作者水印）：

```bash
python pipeline.py \
  --query "漫威混剪" \
  --source bilibili \
  --bgm bgm.mp3 \
  --style intense \
  --output my_montage.mp4
```

Remix 别人的混剪：

```bash
python pipeline.py \
  --movies someone_montage.mp4 \
  --bgm new_bgm.mp3 \
  --style intense \
  --output my_remix.mp4
```

### 4. 高级功能

LLM 自然语言控制：

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --prompt "做一个30秒的漫威高燃混剪，橙青调色，快节奏"
```

视频增强（防抖 + 降噪 + 调色）：

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --enhance stabilize denoise color-grade
```

字幕压制：

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --subtitles tiktok
```

导出时间轴（导入 Premiere / DaVinci）：

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --export-timeline edl

# OTIO 格式（DaVinci Resolve / Premiere Pro / FCP）
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --export-timeline otio
```

使用风格预设：

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --style-preset vlog

# 可用预设：action, vlog, documentary, wedding, gaming,
#           mtv, sport, travel, cinematic, viral, lofi
```

BGM 关键词搜索（自动下载 BGM）：

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm-query "高燃 BGM" \
  --style dynamic
```

自动裁剪（横屏转竖屏）：

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --enhance auto-reframe
```

启动 WebUI：

```bash
python pipeline.py --webui
# 浏览器打开 http://localhost:8000
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--movies` | 本地视频文件路径（可多个） | - |
| `--query` | 搜索关键词（与 `--movies` 二选一） | - |
| `--source` | 素材来源 | `playphrase` |
| `--clip-limit` | 最大下载片段数（仅 `--query` 模式） | `20` |
| `--bgm` | BGM 文件路径（不传则自动搜索下载） | - |
| `--bgm-query` | BGM 搜索关键词（不传则按 `--style` 自动搜索） | - |
| `--style` | 基础风格（`dynamic`/`calm`/`intense`） | `dynamic` |
| `--style-preset` | 风格预设（覆盖 `--style` 和 `--enhance`） | - |
| `--output` | 输出文件名 | `final.mp4` |
| `--threshold` | 镜头检测灵敏度（0.01~1.0，越小切得越细） | `0.2` |
| `--prompt` | 自然语言描述 | - |
| `--subtitles` | 字幕风格（`none`/`tiktok`/`youtube`/`minimal`/`cinematic`） | `none` |
| `--enhance` | 视频增强（`stabilize`/`denoise`/`color-grade`/`auto-reframe`） | - |
| `--export-timeline` | 导出时间轴（`edl`/`csv`/`json`/`xml`/`otio`） | - |
| `--webui` | 启动 WebUI 界面 | - |

风格预设列表：

| 预设 | 名称 | 适用场景 |
|------|------|----------|
| `action` | 动作片 | 快节奏、高强度 |
| `vlog` | Vlog | 日常记录、轻松氛围 |
| `documentary` | 纪录片 | 沉稳、专业 |
| `wedding` | 婚礼 | 浪漫、温馨 |
| `gaming` | 游戏 | 高能、炫酷 |
| `mtv` | MTV | 音乐视频、节奏感 |
| `sport` | 体育 | 动感、激情 |
| `travel` | 旅行 | 治愈、清新 |
| `cinematic` | 电影感 | 大片质感 |
| `viral` | 爆款 | 短视频、吸睛 |
| `lofi` | Lo-fi | 复古、文艺 |

## 素材来源

| 来源 | 类型 | 说明 |
|------|------|------|
| `playphrase` | 电影原片（推荐） | 3900 万+ 英文台词片段，无水印，需 Playwright |
| `quodb` | 电影台词库 | 提供台词 + 电影 + 时间码，需配合其他源下载视频 |
| `bilibili` | 影视/二创 | B站视频，可能有作者水印 |
| `youtube` | 全品类 | 需要能访问 YouTube + 登录 cookies |
| `dailymotion` | 全品类 | 国际视频平台 |
| `douyin` | 短视频 | 抖音视频 |
| `ixigua` | 影视/综艺 | 西瓜视频 |
| `acfun` | 动漫/二创 | AcFun 弹幕视频 |
| `vimeo` | 创意短片 | 高质量创意视频 |
| `yarn` | 台词搜索 | 英文台词搜片段（需 Playwright） |
| `zhaotaici` | 台词搜索 | 中文台词搜片段（需 Playwright） |

台词搜索源说明：YARN / PlayPhrase / QuoDB / 找台词网 需要安装 Playwright，这些站点有 Cloudflare 反爬保护，必须用浏览器自动化。

```bash
pip install playwright
# 使用系统已安装的 Chrome 浏览器，无需额外下载 Chromium
```

## 项目结构

```text
ai-montage-agent/
├── pipeline.py              # 兼容入口（实际实现在 packages/montage）
├── requirements.txt         # Python 依赖
├── packages/
│   ├── montage/             # 核心 Pipeline（镜头/节拍/卡点/渲染）
│   ├── core_types/          # 统一数据类型
│   ├── video_crawler/       # 素材爬取（B站/YouTube/台词搜索/BGM 搜索）
│   ├── video_enhancement/   # 视频增强（防抖/降噪/调色/裁剪/闪避/主体追踪）
│   ├── subtitle_engine/     # 字幕引擎（Whisper 转录 + 压制）
│   ├── ai_director/         # LLM 自然语言控制
│   ├── timeline_export/     # 时间轴导出（EDL/CSV/JSON/XML/OTIO）
│   ├── sound_engine/        # 音效卡点（impact/whoosh）
│   ├── audio_mixer/         # 多轨混音
│   ├── montage_engine/      # 蒙太奇引擎（10+ 种转场）
│   ├── render_engine/       # 渲染引擎（FFmpeg 执行器）
│   └── webui/               # WebUI（FastAPI + B站漫画风格前端）
├── apps/                    # 入口说明（主入口是 pipeline.py / --webui）
├── scripts/                 # smoke/ 冒烟测试 + demo/ 演示
├── artifacts/               # 演示样片（gitignored）
├── var/                     # 运行产物 cache/output/uploads（gitignored）
└── tests/                   # pytest
```

## 技术栈

- **视频处理**：FFmpeg、OpenCV
- **音频分析**：librosa
- **字幕**：openai-whisper / faster-whisper
- **LLM**：OpenAI 兼容 API（支持 Ollama 本地部署）
- **WebUI**：FastAPI + SSE
- **素材爬取**：yt-dlp + B站 API + Playwright（台词搜索）

## 许可证

[MIT License](#许可证)

[⬆ 返回顶部 / Back to top](#ai-montage-agent)

如果这个项目对你有帮助，欢迎赞赏支持 ☕

![e2704e340ac74802081327175480db49](README.assets/e2704e340ac74802081327175480db49.png)

---

# English

- [Features](#features)
- [Quick Start](#quick-start)
- [Arguments](#arguments)
- [Sources](#sources)
- [Project Structure](#project-structure)
- [Tech Stack](#tech-stack)
- [License](#license)

## Features

| Feature | Description | Status |
|---------|-------------|--------|
| Shot detection | FFmpeg scene detect, auto shot cutting | ✅ |
| BGM beat analysis | librosa detects beats / energy / drops | ✅ |
| Highlight scoring | 5-axis scoring (motion / shot variety / faces / camera / audio) | ✅ |
| Beat sync | Shots aligned to beats with strong/weak grading | ✅ |
| Clip search | Auto-download from Bilibili / YouTube / quote search | ✅ |
| Style templates | 11 presets (Vlog / documentary / wedding / gaming, etc.) | ✅ |
| Video enhancement | Stabilize / denoise / color grade | ✅ |
| Auto-reframe | Subject tracking, landscape → portrait | ✅ |
| Dialogue ducking | BGM auto-ducks under speech | ✅ |
| Color harmonizer | Brightness matching across clips | ✅ |
| Subtitles | Whisper speech-to-caption, 5 styles | ✅ |
| LLM control | Natural language → edit instructions | ✅ |
| Timeline export | EDL / CSV / JSON / XML / OTIO | ✅ |
| Rich transitions | dissolve / zoom / shake / wipe, 10+ types | ✅ |
| WebUI | FastAPI + Bilibili-comic-style frontend | ✅ |

## Quick Start

### 1. Requirements

- Python 3.10+
- FFmpeg (required)

### 2. Installation

```bash
# Install FFmpeg
# Windows: scoop install ffmpeg / choco install ffmpeg
# Mac:     brew install ffmpeg
# Linux:   sudo apt install ffmpeg

# Install Python dependencies
pip install -r requirements.txt
```

### 3. Basic Usage

Local video files:

```bash
python pipeline.py \
  --movies movie1.mp4 movie2.mp4 \
  --bgm bgm.mp3 \
  --style dynamic \
  --output my_montage.mp4
```

Search clips by keyword (PlayPhrase recommended — original film clips, no watermark):

```bash
python pipeline.py \
  --query "i love you" \
  --bgm bgm.mp3 \
  --style dynamic \
  --output my_montage.mp4
```

Bilibili clips (playurl API is used for downloads; may carry creator watermarks):

```bash
python pipeline.py \
  --query "marvel montage" \
  --source bilibili \
  --bgm bgm.mp3 \
  --style intense \
  --output my_montage.mp4
```

Remix someone else's montage:

```bash
python pipeline.py \
  --movies someone_montage.mp4 \
  --bgm new_bgm.mp3 \
  --style intense \
  --output my_remix.mp4
```

### 4. Advanced Features

LLM natural-language control:

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --prompt "make a 30s high-energy Marvel montage, teal-orange grade, fast cuts"
```

Video enhancement (stabilize + denoise + color grade):

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --enhance stabilize denoise color-grade
```

Burn-in subtitles:

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --subtitles tiktok
```

Export timeline (import into Premiere / DaVinci):

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --export-timeline edl

# OTIO format (DaVinci Resolve / Premiere Pro / FCP)
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --export-timeline otio
```

Use a style preset:

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --style-preset vlog

# Available presets: action, vlog, documentary, wedding, gaming,
#                    mtv, sport, travel, cinematic, viral, lofi
```

Search BGM by keyword (auto-download):

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm-query "epic BGM" \
  --style dynamic
```

Auto-reframe (landscape → portrait):

```bash
python pipeline.py \
  --movies video.mp4 \
  --bgm bgm.mp3 \
  --enhance auto-reframe
```

Launch the WebUI:

```bash
python pipeline.py --webui
# Open http://localhost:8000 in your browser
```

## Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--movies` | Local video file paths (one or more) | - |
| `--query` | Search keyword (mutually exclusive with `--movies`) | - |
| `--source` | Clip source | `playphrase` |
| `--clip-limit` | Max clips to download (`--query` mode only) | `20` |
| `--bgm` | BGM file path (auto-searched if omitted) | - |
| `--bgm-query` | BGM search keyword (auto by `--style` if omitted) | - |
| `--style` | Base style (`dynamic`/`calm`/`intense`) | `dynamic` |
| `--style-preset` | Style preset (overrides `--style` and `--enhance`) | - |
| `--output` | Output filename | `final.mp4` |
| `--threshold` | Shot detection sensitivity (0.01~1.0, lower = finer cuts) | `0.2` |
| `--prompt` | Natural-language description | - |
| `--subtitles` | Subtitle style (`none`/`tiktok`/`youtube`/`minimal`/`cinematic`) | `none` |
| `--enhance` | Enhancements (`stabilize`/`denoise`/`color-grade`/`auto-reframe`) | - |
| `--export-timeline` | Timeline export (`edl`/`csv`/`json`/`xml`/`otio`) | - |
| `--webui` | Launch the WebUI | - |

Style presets:

| Preset | Name | Best for |
|--------|------|----------|
| `action` | Action | Fast-paced, high-intensity |
| `vlog` | Vlog | Everyday, relaxed |
| `documentary` | Documentary | Steady, professional |
| `wedding` | Wedding | Romantic, warm |
| `gaming` | Gaming | High-energy, flashy |
| `mtv` | MTV | Music video, rhythmic |
| `sport` | Sport | Dynamic, passionate |
| `travel` | Travel | Soothing, fresh |
| `cinematic` | Cinematic | Blockbuster feel |
| `viral` | Viral | Short-form, eye-catching |
| `lofi` | Lo-fi | Retro, artsy |

## Sources

| Source | Type | Notes |
|--------|------|-------|
| `playphrase` | Original film clips (recommended) | 39M+ English quote clips, no watermark, needs Playwright |
| `quodb` | Movie quote database | Quotes + film + timecode; pair with another source to download video |
| `bilibili` | Film / fan edits | Bilibili videos, may carry creator watermarks |
| `youtube` | All categories | Requires YouTube access + login cookies |
| `dailymotion` | All categories | International video platform |
| `douyin` | Short-form | Douyin videos |
| `ixigua` | Film / variety | Xigua Video |
| `acfun` | Anime / fan edits | AcFun danmaku videos |
| `vimeo` | Creative shorts | High-quality creative videos |
| `yarn` | Quote search | English quote clip search (needs Playwright) |
| `zhaotaici` | Quote search | Chinese quote clip search (needs Playwright) |

Quote-search sources (YARN / PlayPhrase / QuoDB / Zhaotaici) require Playwright — these sites use Cloudflare anti-scraping, so browser automation is mandatory.

```bash
pip install playwright
# Uses the system-installed Chrome browser; no extra Chromium download needed
```

## Project Structure

```text
ai-montage-agent/
├── pipeline.py              # Compatibility entry (implementation in packages/montage)
├── requirements.txt         # Python dependencies
├── packages/
│   ├── montage/             # Core pipeline (shots/beats/sync/render)
│   ├── core_types/          # Shared data types
│   ├── video_crawler/       # Clip crawling (Bilibili/YouTube/quote/BGM search)
│   ├── video_enhancement/   # Enhancement (stabilize/denoise/grade/reframe/ducking/tracking)
│   ├── subtitle_engine/     # Subtitle engine (Whisper transcribe + burn-in)
│   ├── ai_director/         # LLM natural-language control
│   ├── timeline_export/     # Timeline export (EDL/CSV/JSON/XML/OTIO)
│   ├── sound_engine/        # Beat-synced SFX (impact/whoosh)
│   ├── audio_mixer/         # Multi-track audio mixing
│   ├── montage_engine/      # Montage engine (10+ transitions)
│   ├── render_engine/       # Render engine (FFmpeg executor)
│   └── webui/               # WebUI (FastAPI + Bilibili-comic-style frontend)
├── apps/                    # Entry-point notes (main entry: pipeline.py / --webui)
├── scripts/                 # smoke/ tests + demo/ samples
├── artifacts/               # Demo renders (gitignored)
├── var/                     # Runtime artifacts cache/output/uploads (gitignored)
└── tests/                   # pytest
```

## Tech Stack

- **Video processing**: FFmpeg, OpenCV
- **Audio analysis**: librosa
- **Subtitles**: openai-whisper / faster-whisper
- **LLM**: OpenAI-compatible API (supports local Ollama)
- **WebUI**: FastAPI + SSE
- **Clip crawling**: yt-dlp + Bilibili API + Playwright (quote search)

## License

MIT License

[⬆ Back to top](#ai-montage-agent)

If this project helps you, feel free to buy me a coffee ☕

![e2704e340ac74802081327175480db49](README.assets/e2704e340ac74802081327175480db49-1792459188812-2.png)
