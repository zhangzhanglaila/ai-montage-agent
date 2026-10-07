# AI Montage Agent — 后续开发方案与进度计划

> 制定日期：2026-10-04
> 基线版本：`main` @ `9d96ee9`（15 commits）
> 仓库：https://github.com/zhangzhanglaila/AI-Montage-Agent
> 原则：**每完成一个功能 = 至少一个独立 commit，且提交前必须通过验收门禁。**

---

## 0. 现状基线（开工前必读）

### 已有能力（勿重复开发）

| 领域 | 现状 | 代码位置 |
|------|------|----------|
| 镜头检测 | FFmpeg scene detect | `pipeline.py: ShotDetector(26)` |
| 高光评分 | 5 维评分 | `pipeline.py: HighlightScorer(329)` |
| 节拍分析 | librosa BPM/强弱拍/高潮 | `pipeline.py: BeatAnalyzer(214)` |
| 卡点同步 | 镜头对齐节拍 + 变速 + **拍数受可用画面长度约束** | `pipeline.py: BeatSyncEngine(552)` |
| 镜头选择 | 高光排序 + **源区间空隙约束** + **画面级去重** | `pipeline.py: MontagePipeline.select_highlight_shots()` |
| 渲染 | FFmpeg 拼接/转场/混音（**保留原音轨、每段音画等长**） | `pipeline.py: VideoRenderer(755)` |
| 转场 | 10 种预设 + 30+ FFmpeg 效果 | `packages/montage_engine/src/` |
| 素材爬取 | B站/YouTube/台词/BGM | `packages/video_crawler/src/` |
| 视频增强 | 防抖/降噪/调色/裁帧/闪避/色彩统一 | `packages/video_enhancement/src/` |
| 字幕 | Whisper 转录 + 6 种压制风格（**含 karaoke 实现**） | `packages/subtitle_engine/src/` |
| LLM 控制 | 自然语言 → 剪辑参数 | `packages/ai_director/src/creative_director.py` |
| 时间轴导出 | EDL/CSV/JSON/XML/OTIO | `packages/timeline_export/src/` |
| WebUI | FastAPI + SSE + 漫画风前端 | `packages/webui/src/app.py` |

### 关键集成点（新功能挂钩位置）

| 位置 | 说明 |
|------|------|
| `pipeline.py: MontagePipeline.run()` | 渲染后处理链：色彩协调 → 渲染 → 对话闪避 → 调色 → 竖屏裁切 → 音效卡点 → 配音旁白 |
| `pipeline.py: MontagePipeline.select_highlight_shots()` | **镜头选择**（第 5 步）：区间空隙约束 → 画面级去重 → 交替排列，新去重策略挂这里 |
| `pipeline.py: BeatSyncEngine.sync()` | 镜头↔节拍量化，画面长度约束（检查拍数来源）与变速都在这 |
| `pipeline.py: main()` | CLI 后处理链：`_apply_enhancement` → `_apply_subtitles` → `_export_timeline` |
| `packages/webui/src/app.py: _run_montage_task()` | WebUI 后台任务入口，新功能需同步加参数 |
| `packages/core_types/models.py` | 共享数据类；`Shot.embedding` 字段**已预留** |

> ⚠️ **加新 CLI 参数时**记得四件套一起改：`argparse` 定义 → `parser.parse_args()` 透传 →
> `MontagePipeline.run()` 形参 → `select_highlight_shots()`/`BeatSyncEngine.sync()` 消费点。
> `--min-shot-gap` / `--visual-dedup-threshold` 就是按这个链路接的。

### 已发现的现存小问题（顺手修）

1. `caption_burner.py` 已实现 `CaptionStyle.KARAOKE` + 逐字高亮，但 `pipeline.py` 的 `--subtitles` choices（L1214）**未暴露 karaoke** → 功能存在却调不出来。
2. `_apply_subtitles()` 固定用 `model_size="base"`，无参数可调；
3. `_apply_enhancement()` 不支持 `auto-reframe`（仅在 style preset 路径里生效）；
4. 工作区有未跟踪的 `.claude/settings.local.json` 被改，建议加入 `.gitignore`。

---

## 1. Git 协作规范（"每功能一 commit" 落地规则）

### 1.1 分支策略

```
main                    ← 永远可运行（每个 commit 都过门禁）
 └── feat/<scope>-<name>  ← 单功能开发分支，如 feat/voice-tts
```

- 一个功能一个分支，完成后 `--no-ff` 合并回 `main`（保留功能粒度历史）。
- 小到"暴露 karaoke 参数"这类改动，可直接在 `main` 上单 commit（遵守 1.3 门禁即可）。

### 1.2 Commit message 规范（沿用现有风格）

```
feat(<scope>): <动词 + 对象，简述做了什么>
```

| scope | 用途 |
|-------|------|
| `sfx` | 音效卡点 |
| `voice` | 配音 / TTS 旁白 |
| `cover` | 封面 / 缩略图 |
| `subtitle` | 字幕（翻译/双语/卡拉OK） |
| `index` | 语义镜头检索 |
| `track` | 人脸 / 主体追踪 |
| `speed` | 变速曲线 |
| `mixer` | 多轨音频 |
| `preview` | 实时预览 |
| `infra` | 队列 / 部署 / 数据库 |
| `publish` | 一键投稿 |
| `batch` | 批量成片 |
| `style` | 风格学习 / 模板 |

示例：`feat(voice): 新增 Edge-TTS 配音旁白引擎与旁白轨混音`

### 1.3 单个 commit 的验收门禁（**强制**）

每个功能提交前必须全部满足，缺一不可：

1. `make test` 全绿（`pytest tests/ -v`）；
2. 新增功能的**冒烟脚本**跑通并产出真实文件（见 1.4）；
3. CLI `--help` / 新参数可用，至少跑一次端到端小样（可用 `test_assets/` 里的短视频）；
4. 无新增 Python 报错/回归（`cache/` 生成物不进 git）。

### 1.4 冒烟脚本约定

每个功能配一个 `scripts/smoke_<feature>.py`，用 `test_assets/` 的素材最小化跑一遍，输出到 `output/smoke/`：

```bash
python scripts/smoke_voice.py     # 应产出 output/smoke/voice_demo.mp4
```

用途：既是本次 commit 的证据，也是回归测试基础。

### 1.5 进度追踪

每完成一项，在本文档 §5 表格勾选 ☑，并记录 commit hash。

---

## 2. 开发路线总览

| 阶段 | 主题 | 目标版本 | 功能数 | 预估（人日） |
|------|------|----------|--------|--------------|
| **Phase 1** | 内容化（从"卡点混剪"到"内容生成"） | v0.5 | 5 | 11–14 |
| **Phase 2** | 智能化（从"能剪"到"剪得聪明"） | v0.6 | 5 | 22–28 |
| **Phase 3** | 平台化（从"工具"到"产品"） | v0.7 / v1.0 | 5 | 24–34 |

> 工时按"熟练 Python 单人、含自测"估算。若每天投入 2–3 小时（业余），Phase 1 约 1–1.5 周，Phase 2 约 3–4 周，Phase 3 约 4–6 周。
> **建议先完整交付 Phase 1**——它把项目适用面翻倍，且底层（节拍/混音/字幕）大多现成。

---

## 3. Phase 1 — 内容化（v0.5）

### F1.1 音效卡点引擎（sfx）

- **目标**：在强拍/剪辑点自动叠 `whoosh / impact / riser / click` 音效，提升"专业感"。
- **新增**：`packages/sound_engine/src/sfx_engine.py`、`packages/sound_engine/src/sfx_library.py`（音效素材索引）、`packages/sound_engine/__init__.py`、`assets/sfx/README.md`（素材放哪、来源约定）。
- **核心接口**：
  ```python
  class SfxEngine:
      def plan(self, beats: list[Beat], style: str) -> list[SfxCue]  # 选点 + 选音效
      def mix(self, video_path: str, cues: list[SfxCue], output_path: str) -> str
  ```
- **集成**：`MontagePipeline.run()` 渲染后、`enable_sfx` 参数分支内调用；CLI 加 `--sfx`（choices: `none/auto`，默认 `none`）。
- **素材**：优先内置 5–8 个免版权音效（CC0），避免版权风险；不用爬虫下载音效。
- **验收**：`scripts/smoke_sfx.py` 用 1 段视频 + 1 首 BGM 产出带音效成片，音效落在强拍上（可用波形图核对）。
- **预估**：2–3 人日。
- **commit**：`feat(sfx): 新增节拍音效卡点引擎（whoosh/impact 自动叠加强拍）`

### F1.2 AI 配音旁白（voice）

- **目标**：支持"解说/盘点"流——LLM 生成解说词 → TTS 合成 → 与画面/BGM 混轨。
- **新增**：
  - `packages/voice_engine/src/script_writer.py`（LLM 生成旁白脚本，分句带时长）
  - `packages/voice_engine/src/tts_engine.py`（默认 `edge-tts`，可选 CosyVoice/GPT-SoVITS 适配器）
  - `packages/voice_engine/__init__.py`
- **核心接口**：
  ```python
  class ScriptWriter:
      def write(self, topic: str, target_sec: int, style: str) -> list[NarrationLine]
  class TtsEngine:
      def synthesize(self, lines: list[NarrationLine], voice: str, out_dir: str) -> list[str]
  ```
- **集成**：
  - `run()` 新增 `narration_script` / `voice` 参数，在渲染后把旁白轨与成片混音（复用 `dialogue_ducking` 闪避逻辑，旁白段 BGM 自动降 8–12dB）；
  - CLI：`--narrate "主题"`（LLM 自动写词）、`--voice zh-CN-YunxiNeural`、`--script script.txt`（自定义词）。
  - 依赖：`edge-tts`（免费、无需 key）加入 `requirements.txt`。
- **验收**：`scripts/smoke_voice.py` 产出"画面 + 中文旁白 + 自动闪避的 BGM"成片。
- **预估**：4–5 人日。
- **commit**：`feat(voice): 新增 AI 配音旁白引擎与旁白轨闪避混音`

### F1.3 封面 / 缩略图生成（cover）

- **目标**：出片即出封面，打通"发布"最后一环。
- **新增**：`packages/cover_engine/src/cover_maker.py`。
- **核心接口**：
  ```python
  def pick_best_frame(video_path: str, top_n: int = 10) -> str
  def make_cover(frame_path: str, title: str, subtitle: str = "",
               size: str = "16:9", out_path: str = None) -> str
  ```
- **实现要点**：复用 `HighlightScorer` 的高分帧抽帧；Pillow 叠标题（描边 + 底衬）、竖版 `3:4`、横版 `16:9` 各一张；字体走系统字体回退链。
- **集成**：CLI `--cover "标题"`，输出到 `output/<name>_cover.jpg`；WebUI 任务结果区展示。
- **验收**：`scripts/smoke_cover.py` 产出 2 张封面图，标题清晰可读。
- **预估**：2 人日。
- **commit**：`feat(cover): 新增视频封面/缩略图自动生成（16:9 + 3:4）`

### F1.4 字幕翻译 · 双语（subtitle）

- **目标**：一键生成中英（或源→目标）双语字幕，服务出海/搬运。
- **新增**：`packages/subtitle_engine/src/translator.py`；`caption_burner` 增加 `BILINGUAL` 风格 + 双行 ASS 生成。
- **核心接口**：
  ```python
  def translate_segments(segments: list[dict], target: str = "en",
                         backend: str = "llm") -> list[dict]  # 保留时间码
  def build_bilingual_ass(segments, style, out_path) -> str
  ```
- **集成**：CLI `--translate en`（与 `--subtitles` 组合）；LLM 走已有 OpenAI 兼容配置，无 key 时降级 `deep-translator`（Google 免费源）。
- **验收**：`scripts/smoke_translate.py` 产出中英双行字幕成片。
- **预估**：2 人日。
- **commit**：`feat(subtitle): 新增字幕翻译与双语字幕压制`

### F1.5 卡拉OK 字幕打通（subtitle）

- **目标**：把已实现但未暴露的逐字高亮字幕接出来（**低成本高收益**）。
- **改动**：
  - `pipeline.py: --subtitles` choices 增加 `karaoke`；
  - `_apply_subtitles()` 支持 `model_size` 参数、karaoke 时强制 `word_timestamps=True` 并输出 JSON（`CaptionBurner` 已支持 `words`）；
  - WebUI 字幕下拉增加 karaoke。
- **验收**：`python pipeline.py --movies test_assets/fix_0.mp4 --bgm xxx.mp3 --subtitles karaoke` 产出逐字高亮成片。
- **预估**：1 人日（含联调）。
- **commit**：`feat(subtitle): 暴露逐字卡拉OK字幕样式并打通 word_timestamps`

---

## 4. Phase 2 — 智能化（v0.6）

### F2.1 语义镜头检索 / 镜头图谱（index）
- 用 CLIP 给每个镜头打 embedding 存 `cache/index/shot_index.json`（`Shot.embedding` 字段已预留），支持"找所有爆炸镜头""找微笑特写"检索。
- 新增 `packages/shot_index/src/{embedder,index_store,retriever}.py`；CLI `--query-shots "a smile"`。
- 验收：检索 20 个镜头返回按相似度排序结果，命中目视合理。**5–6 人日**。
- commit：`feat(index): 新增 CLIP 语义镜头索引与检索`

### F2.2 人脸 / 主体追踪（track）
- 升级 `auto_reframe`：从"固定中心裁剪"→"主体轨迹跟随裁剪"，避免竖屏丢主角；顺带支持自动打码。
- 新增 `packages/video_understanding/src/subject_tracker.py`（OpenCV/轻量模型），改造 `video_enhancement/src/auto_reframe.py`。
- 验收：`smoke_track.py` 竖屏输出中主体稳定居中。**5–6 人日**。
- commit：`feat(track): 主体追踪裁切替换固定中心裁切`

### F2.3 变速曲线（speed）
- 整段等比变速 → 关键帧速度斜坡（平滑慢动作 / 卡点冲刺）。
- 新增 `packages/montage_engine/src/speed_curve.py`（生成 `setpts` 表达式 + 音频 `atempo` 补偿），接入 `VideoRenderer._adjust_speed`。
- 验收：成片存在"加速→正常→慢放"平滑过渡且音画同步。**3–4 人日**。
- commit：`feat(speed): 新增关键帧变速曲线（速度斜坡）`

### F2.4 多轨音频混音（mixer）
- 人声 / BGM / 音效三轨独立增益与自动化，是 F1.1/F1.2 的底层支撑。
- 新增 `packages/audio_mixer/src/mixer.py`（FFmpeg `amix`/`sidechaincompress` + 增益包络）。
- 验收：三轨可分别设置音量，旁白段 BGM 自动 duck。**3–4 人日**。
- commit：`feat(mixer): 新增人声/BGM/音效多轨混音与增益自动化`

### F2.5 实时预览（preview）
- WebUI 增加低码率代理 + 分片进度推送，边调参边预览。
- 改造 `packages/webui/src/app.py`（SSE → WebSocket 或分片轮询）、新增代理生成逻辑。
- 验收：WebUI 拖动风格参数后 10s 内出现预览片段。**5–8 人日**。
- commit：`feat(preview): WebUI 新增低码率实时预览`

---

## 5. Phase 3 — 平台化（v0.7 / v1.0）

| ID | 功能 | 要点 | 预估 | commit |
|----|------|------|------|--------|
| F3.1 | 任务队列 + 持久化 | ~~Celery/RQ + Redis~~ → **纯标准库 SQLite + worker 线程**（离线可用，接口可平替 Celery），替换内存态任务表；**另有可选 Redis 后端**（多进程/多机共享任务表，`MONTAGE_TASK_BACKEND=redis` 一行切换） | 6–8 | `feat(infra): 引入任务队列与任务持久化` |
| F3.2 | Docker 部署 | Dockerfile（ffmpeg + Playwright + torch 分层缓存）+ compose | 3–4 | `feat(infra): 新增 Docker 一键部署` |
| F3.3 | 一键投稿发布 | 对接 B站开放平台 / 抖音 / YouTube Data API | 6–8 | `feat(publish): 新增 B 站一键投稿` |
| F3.4 | 批量成片 | 一条 BGM × N 组素材 → N 条成片（矩阵号） | 4–5 | `feat(batch): 新增批量成片任务` |
| F3.5 | 风格学习 / 模板市场 | 从示例混剪反推风格参数，可导出/导入模板 | 5–9 | `feat(style): 新增风格学习与模板导入导出` |

---

## 6. 进度追踪表

> 每完成一项：勾选 ☑ 并填 commit hash。**未过 §1.3 门禁不得勾选。**

### Phase 1 — v0.5
| ID | 功能 | 状态 | commit | 完成日期 |
|----|------|------|--------|----------|
| F1.5 | 卡拉OK 字幕打通 | ☑ | `a026e54` | 2026-10-04 |
| F1.1 | 音效卡点 | ☑ | `05f21bc` | 2026-10-04 |
| F1.2 | AI 配音旁白 | ☑ | `d987228` | 2026-10-04 |
| F1.3 | 封面生成 | ☑ | `de41ea0` | 2026-10-04 |
| F1.4 | 字幕翻译·双语 | ☑ | `68b7fd0` | 2026-10-04 |

**Phase 1 完成 → 已打 tag `v0.5`。** 新增 4 个能力包（sound / voice / cover / subtitle 扩写），
CLI 新增 `--sfx / --narrate / --script / --cover* / --translate*` 共 12 个参数，
3 个自包含冒烟脚本（sfx / voice / cover / translate），测试套件 11/11 绿。

### 额外修复（开发中发现的既有缺陷，独立提交）
| 说明 | commit | 日期 |
|------|--------|------|
| 静音/异常 BGM 导致 loudnorm 归一化崩溃（tests 长期失败） | `a59fd08` | 2026-10-04 |
| 单一 xfade 段时清理误删结果文件（tests 随机失败） | `6282c81` | 2026-10-04 |
| **转场/拼接渲染丢失原片音轨**（成片永远只有纯 BGM，无原声） | `519e382` | 2026-10-07 |
| **镜头选择产生重复镜头**（约束只比"上一个"、且用起点差） | `5df4807` | 2026-10-07 |
| **镜头切割偏移一整个 GOP**（`-c copy` 只能从关键帧起切，实际内容比标称早） | `8c46c23` | 2026-10-07 |
| **成片视频流被截断**（每段音画不等长 + xfade offset 越界，画面 1.5s / 声音 7.0s） | `a9fb817` | 2026-10-07 |
| **素材自带回顾导致的画面重复**（区间去重原理上抓不到，需画面级去重） | `d83e9fd` | 2026-10-07 |

#### 修复记录：镜头选择产生重复镜头（2026-10-07）
- **现象**：成片里反复出现"同一段连续画面"，看着像重复镜头。
- **根因**（`pipeline.run()` 里的选镜头代码，已抽成 `MontagePipeline.select_highlight_shots()`）：
  ```python
  min_gap = 20
  if abs(shot.shot_id - last_id) >= min_gap:   # ← 只跟"上一个入选者"比
  ```
  1) **只跟上一个比，不跟所有已选镜头比**。而循环是按分数降序走的，于是
     `shot2[1.00-2.00]` 与 `shot4[2.13-3.00]` 这种**源上仅隔 0.13s** 的镜头，只要各自
     离"上一个"够远就都能入选。实测 15 个镜头里 **9 对源起点差 <2s**，
     `id71/75/77` 三个还全挤在 `32.87~36.37` 这 3.5s 窗口里。
  2) **比的是"起点差"而不是"区间空隙"**：`id61[26.40-28.43]` 与
     `id62[28.43-29.63]` 起点差 2.03s 看着够远，实则两段**严丝合缝相接**。
     （`_detect_scenes()` 产出的场景本身就是首尾相接的，所以"相接"是常态。）
  3) **shot_id 差值不可靠**：`shot_id = video_index*10000 + i`，而 `detect()` 会
     `continue` 掉 <0.1s 的短场景，id 并不连续；跨源比较更是毫无意义。
- **修复**：抽出 `select_highlight_shots()`，对**所有**已选镜头做「源区间空隙
  `max(起点)-min(终点)` ≥ min_sep」的**全对**约束；`min_sep` 随源跨度自适应
  （`max(min_shot_gap, span/(max_per_video*2.5))`，默认 `min_shot_gap=1.0`）。
  新增 CLI `--min-shot-gap`（0 关闭约束，回到旧行为）。顺带把分数排序的
  并列项按 `start_time` 决胜，结果确定性可复现。
- **实测**（真实素材 `output/test_montage8.mp4`，calm 风格，67 镜头）：
  | `--min-shot-gap` | 入选 | 最小源区间空隙 | 相接对数 |
  |---|---|---|---|
  | 0（旧行为） | 15 | **0.00s** | **4** |
  | **1.0（新默认）** | 13 | **1.17s** | **0** |
  | 3.0 | 8 | 3.47s | 0 |
  修复前 4 对相接：`9.27-10.63→10.63-12.00`、`25.43-26.40→26.40-28.43`、
  `26.40-28.43→28.43-29.63`、`34.50-36.37→36.37-37.97`（每对都是同一段连续画面）。
  同一个文件当两个源传入（检出 134 = 2×67 重复内容）也能收敛到 15 个、
  最小空隙 1.13s、0 相接。
  对比成片：`output/demo/dedup_before_gap0.mp4`(19.1s) / `dedup_after_gap1.mp4`(14.8s)。
- **门禁**：`pytest` **42 passed**；新增 `scripts/smoke_shot_dedup.py`
  **21 项可证伪断言全过**，含**反证**——用旧算法在同一个构造池/同素材上跑，
  它确实会同时选中 `2` 与 `4`（源上仅隔 0.13s），且在 22 镜头素材上只能选出 1 个
  （凑不出成片）；既有 6 个冒烟脚本无回归；`--min-shot-gap` 已进 `--help`。

#### 修复记录：转场/拼接渲染丢失原片音轨（2026-10-07）
- **现象**：成片全程只有 BGM，听不到原片人声/现场声；日志静默打印
  「混音失败（可能无原音轨），使用纯 BGM」。
- **根因（比预估更靠前）**：`_prepare_clip()` 两个分支都带 `-an`，**在第一步就把原音轨丢了**，
  所以无论走 cut 路径还是转场路径，底片都没有音轨 → Step3 混音的 `[0:a]` 不存在 →
  回退纯 BGM。`_render_with_transitions()` 的 `scaled_*.mp4` 又叠了一层 `-an`，
  且 xfade 段只 map 了视频流。
- **修复**（`pipeline.py`，`VideoRenderer`）：
  1. `_prepare_clip`：保留原音轨；变速改用 **多级 `atempo` 串联**（`_atempo_chain`，单级只支持
     [0.5,2.0]）同步变速、`apad` 补齐到画面长度；源无音轨时用 `anullsrc` **补静音轨**，
     保证所有 clip 流结构一致（否则 concat demuxer 的 `-c copy` 会错位）。
  2. `_render_with_transitions` 的 `scaled_*.mp4`：去掉 `-an`，统一 `aac/44100/立体声`。
  3. `_xfade_segment`：新增 `acrossfade` 音频链（`d` 取 `min(转场时长, 0.9×被叠合片长)`，
     避免 acrossfade 报错），末尾 `apad` + 输出 `-t <视频累计时长>` 精确对齐，
     防止 concat demuxer 按流各自拼接造成音画逐段漂移。
  4. 顺带修 xfade 的 `offset`：原 `min(offset, accum_dur-trans_dur-0.1)` 因 `offset` 恒等于
     `accum_dur`，**每个转场都被无条件提前 0.1s**，成片比解析值短 `0.1s×转场数`。
     改为 `offset = accum_dur - trans_dur`（转场结束正好落在接缝）。
- **实测**（真实素材 `output/test_montage8.mp4`，转场渲染底片 vs 原片同窗口分频段）：
  低 −13.6/−12.3、中 −18.2/−16.1、高 −25.8/−23.4 dB —— 频段差 ≤2.4dB，原声原样流过。
  用**数字静音**当 BGM 走完整 `render()`，成片 max −16.0dB、高频 −46.2dB
  （若回退纯 BGM 必为 −91dB）→ 证明成片声音确实来自原片。真实 CLI 跑 calm（全转场）
  风格，日志无任何「混音失败 / xfade segment 失败」回退信息。
- **门禁**：`pytest` **42 passed**（无 skip）；新增 `scripts/smoke_transition_audio.py`
  **18 项可证伪断言全过**（含 FFT 主频检出 440/1200Hz、反证静音、变速音画等长、
  fade 成片时长=3.5s 解析值、音画时长差 <0.1s）；既有 sfx/voice/cover/mixer/speed 冒烟无回归。

#### 修复记录：镜头切割偏移一整个 GOP（2026-10-07，`8c46c23`）
- **现象**：用户连续两次反馈「还是一直有重复镜头」「还是有一个片段用了 4 次」。
- **根因**：`ShotDetector.detect()` 切割用 `-ss <start> -i SRC -to <dur> -c copy`。
  **copy 模式无法丢帧**，ffmpeg 只能从 `<= start` 的**关键帧**开始拷，于是镜头文件的
  **实际内容**比记录的 `start_time` 提前一整个 GOP。本片实测：源关键帧间隔
  中位 1.20s / 最大 2.00s，67 个镜头里 **62 个**实际起点早于标称（中位 −0.59s，最大 −4.33s）。
  后果：**标称区间互不重叠、实际画面却大面积重叠**，源上同一时刻被最多 4~6 个镜头文件覆盖
  —— 这才是"同一个片段用了 4 次"的直接来源。区间去重（`5df4807`）修不了它，
  因为区间本来就是分开的。
- **修复**：改成精确重编码 `-ss <start> -i SRC -t <dur> -c:v libx264 -crf 18 -preset veryfast
  -pix_fmt yuv420p -c:a aac -b:a 192k`。重编码时 ffmpeg 会 seek 到关键帧后**解码并丢弃**
  start 之前的帧，切点精确。`-to` 在"输入 -ss"语境下语义含糊，改用相对时长 `-t`。超时 30s→90s。
- **取证方法（可复现，值得沉淀）**：把镜头文件与源都抽成 `fps=10, scale=96:54, rgb24` 的
  原始帧序列，用**整段序列滑窗**取最小 MSE，定位镜头内容在源上的**真实位置**。
  单帧定位不可用——黑场/淡入帧会和任何位置 err=0.0 假匹配（曾出现 28s 的荒谬偏移）。
  音频互相关也不可靠（极短片段下 correlation >1，中位偏移 −0.525s 但离群值满天飞）。
- **实测**（同一批 42 个长镜头）：实际起点与标称相差 ≤0.15s 的 **7/42 → 38/42**；
  源上被 ≥4 个镜头覆盖的时刻 **5 → 0**。成片入选的 13 个镜头：0 对重叠、最大覆盖 1。
- **门禁**：新增 `scripts/smoke_shot_cut_accuracy.py`，**自包含 + 31 项断言**：
  A 每个镜头实际内容起点 == 标称 start（≤1.5 帧，31/31）；
  D 文件时长 == 标称时长（31/31）；**B 反证**：老 `-c copy` 命令在**同一区间**上偏移 −1.00s；
  C 源时间轴上无时刻被 ≥2 个镜头实际覆盖。
  ⚠️ 夹具必须带**空间纹理**（用每块相位不同的棋盘）：纯色块的 `scene` 分数 <0.2，
  默认阈值下检测不到切点（第一版只检出 6 个、全落在关键帧上）。
  `pytest` 42 passed。

#### 修复记录：成片视频流被截断（2026-10-07，`a9fb817`）
- **现象**：真实端到端跑完 exit 0、日志正常，但 `output/nodup_final.mp4` 只有
  **画面 1.53s（46 帧）/ 声音 7.01s** —— 后面全是"只有声音没有画面"。
  （更早的 `demo_orig_audio.mp4` 也是 画面 6.37s / 声音 10.50s，说明是长期存在的问题。）
- **根因（三个缺陷叠加）**：
  1. 卡点时长 `duration = beat_count × beat_interval` **只由歌曲决定，从不看镜头实际有多少
     画面**；而 `apad` 只补音频。短镜头那一段就成了"画面 0.3s + 声音 1.5s"。
  2. `_get_duration()` 取的是容器 `format=duration`（= **所有流时长的最大值**）。音频被补齐后
     它代表的是**音频**长度，却被拿去算 xfade 的几何 → `offset` 超过真实画面长度 → 输出退化。
  3. `trans_dur` 没有和参与叠合的两段画面长度做约束。
- **修复**（`pipeline.py`）：
  1. `_prepare_clip`：用 `tpad=stop_mode=clone` **克隆末帧**把画面补足到 `duration`，
     使每段严格 音长 == 画长 == duration（顺带 `fps=30` 与 xfade 路径统一帧率）。
  2. `_get_duration`：优先取 `v:0` 的 stream duration，取不到才退回 `format=duration`。
  3. `_xfade_segment`：加 `trans_dur <= 0.9 × min(seg_dur[j], accum_dur)` 保护。
  4. `BeatSyncEngine.sync` 加画面长度约束：拍数压到"画面（按 speed 伸缩后）能撑满的整拍数"
     （0.25 拍容差 ≈0.12s），连 1 拍都撑不满的镜头跳过、让下一个镜头顶上。
     否则冻帧占比实测可达 **26%**（最坏一段 90% 是静止帧），修复后降到 **~2.5%**。
     注意：这条必须在**算 speed 之后**执行（画面伸缩量取决于 speed）。
- **实测**（同一素材 / calm / 13 入选）：
  | | 画面 | 声音 | 单段音画差 |
  |---|---|---|---|
  | 修复前 | **1.53s**（46 帧） | 7.01s | 最多 1.84s |
  | 修复后 | **11.30s**（341 帧） | 11.33s | **0/12 段 >0.032s（一帧）** |
- **门禁**：新增 `scripts/smoke_av_length.py`，**自包含 + 8 项断言**：补帧到请求时长、
  clip 音画等长、clip 仍带音轨、**负对照**（老滤镜链 画面 0.30s / 声音 1.50s）、
  `_get_duration` 返回视频流时长、拍数不超可用画面、0.20s 镜头被跳过、端到端成片音画等长。
  `pytest` 42 passed。

#### 功能记录：画面级去重（2026-10-07，`d83e9fd`）
- **动机**：修完上面两条后，"重复镜头"仍能复现。最后查到根因**在素材本身**：
  `output/test_montage8.mp4` 里同一段画面出现了多次 —— 源 `9.25-10.38s == 38.88-40.00s`
  （逐像素 MSE **0.0**），源 `1.0s ≈ 3.0s ≈ 22.6s ≈ 31.4s`，合计 **4.0s（占源 9.4%）**。
  选中这些镜头时它们的**源区间毫无重叠**，区间空隙约束全部放行 —— 这个只能比对
  **画面内容**才能发现，属于原理性的能力缺口（不是 bug）。
- **实现**：
  - `_visual_signature()`：镜头抽成 `8fps / 16x16 灰度` 指纹（256 维/帧），
    按 `(路径, mtime, 大小)` 缓存，重复调用不重复解码。
  - `_visual_shared_window_mse()`：取两段指纹**最相似的 0.5s 窗口**的 MSE。
    不用"整段前缀比对"——源里的重复区间常只覆盖镜头的一部分（实测源 9.25-10.38s 重复，
    而选中镜头是 9.27-10.64s，尾部 0.26s 并不重复），整段比会被不重复的尾部拉高而漏判。
    跳过近纯色/黑场窗口，否则黑场之间 MSE 0.0 互相误判。
  - `drop_visual_duplicates()`：按高光分降序，同一画面只留最高分代表。
  - 接进 `select_highlight_shots()`，且放在区间贪心**之前** —— 被剔掉的镜头由"下一个最优"
    补位，不会白白少镜头。新增 CLI `--visual-dedup-threshold`（0 关闭）。
- **分辨率/阈值是实测选的**（同一素材）：
  | 指纹 | 真实重复对 MSE | 最接近的非重复对 | 合成细棋盘素材的非重复对 |
  |---|---|---|---|
  | 8x8 | 0~23 | 1074 | **9.9（误判）** |
  | **16x16** | **0~37** | **1319** | **1658** |
  8x8 会误判的原因：细高频纹理（密棋盘）降采样后会被**平均成同一种灰**（`base` 与
  `255-base` 的均值恒为 ~127.5）。16x16 保留住了图案。阈值取 **200**，落在 37 与 1319 的
  几何中点附近，上下各留 5 倍余量。
- **实测**（真实素材 / calm / 67 镜头）：丢掉 **7 个**画面重复的镜头（MSE 0.0~37.3，
  源 3.00/22.60/31.43/38.90/0.13/22.07/30.90 各与 1.00 或 2.13 或 9.27 重复）；
  区间贪心照旧取满 **15 个**（补位生效）；成片 画面 9.43s / 声音 9.52s。
  成片级重复检查（10fps 滑窗、排除相邻窗口）：**1 处 → 0 处**。
- **门禁**：新增 `scripts/smoke_visual_dedup.py`，**自包含 + 11 项断言**：造一段
  "第 6 块图案 == 第 1 块"的源模拟回顾片段 → 检出 6 个镜头且源区间互不重叠、
  **用与指纹无关的 10fps/96x54 彩色滑窗独立复核**两块画面确实相同（MSE 0.0，对照 26032）、
  丢掉的是低分那一个、【反证】阈值 0 时不丢镜头、不同镜头 MSE 远大于阈值、
  `select_highlight_shots` 返回值两两不重复。`pytest` 42 passed。
- **已知既有问题（与本次无关）**：`scripts/smoke_preview.py` 因
  `packages/webui/src/app.py` 已把 `_tasks` 改成 `_store()` 而 `ImportError`（两文件均未改动）。

#### F1.5 / F1.1 / F1.2 实现记录
- **F1.5**：新增 `packages/subtitle_engine/src/burn_pipeline.py::transcribe_and_burn`，
  CLI 补齐 `karaoke`/`bold` 与 `--subtitle-model`，WebUI 改为共用该函数。
  （原 WebUI 虽暴露 karaoke 但固定转 SRT，逐字高亮实际失效，已一并修复。）
- **F1.1**：新增 `packages/sound_engine`；音效**程序化合成**（零版权风险、可离线），
  `SfxEngine.plan` 按风格密度选点、`mix` 用 `adelay + amix(normalize=0)` 混音。
- **F1.2**：新增 `packages/voice_engine`（脚本→TTS→混音）；旁白闪避用
  `volume` 滤镜的 `enable` 时间窗实现，规避 `sidechaincompress` 挂起风险。
  `edge-tts` 已实测可用。
- **F1.3**：新增 `packages/cover_engine`；`pick_best_frame` 用 ffmpeg 抽帧 + numpy
  拉普拉斯方差（清晰度）/ 亮度区间打分挑帧（**不引入 cv2**）；`make_cover` 支持
  5 种比例、标题像素级自动折行、渐变蒙版，复用 `subtitle_engine.find_font`
  保证中文标题正常渲染。CLI `--cover/--cover-title/--cover-subtitle/--cover-size/--cover-style`。
- **F1.4**：新增 `packages/subtitle_engine/src/translator.py`（LLM / deep-translator /
  直通三级后端，**保证译文行数与原文一致**以避免串行）；`CaptionBurner` 新增
  `BILINGUAL` 风格与 `build_bilingual_ass`（libass 双行：原文白字在上、译文黄字在下，
  字号随分辨率缩放）、`burn_ass`、`burn_segments`。CLI `--translate/--translate-backend/--subtitle-lang`，
  WebUI 字幕风格加「双语」并新增翻译目标语言下拉。

### Phase 2 — v0.6
| ID | 功能 | 状态 | commit | 完成日期 |
|----|------|------|--------|----------|
| F2.1 | 语义镜头检索 | ☑ | `9ecb480` | 2026-10-04 |
| F2.2 | 人脸/主体追踪 | ☑ | `906681c` / `494cf39` | 2026-10-04 |
| F2.3 | 变速曲线 | ☑ | `c9d2cf3` | 2026-10-04 |
| F2.4 | 多轨音频混音 | ☑ | `fb54f13` | 2026-10-04 |
| F2.5 | 实时预览 | ☑ | `4f50fd5` | 2026-10-04 |

#### F2.3 实现记录
- 新增 `packages/montage_engine/src/speed_curve.py`。
- **关键决策**：用「**分段等速**」而不是一条连续速度函数。因为 ffmpeg `atempo`
  不支持时变——若只让视频用 `setpts` 平滑变速、音频近似，两者时长会累积漂移导致
  音画不同步。分段后每段视频/音频都被压到同一输出时长，逐段对齐 ⇒ 全程严格同步。
- `atempo` 因子一律拆到 `[0.5, 2.0]` 再串联，兼容只支持该区间的老版 ffmpeg。
- 预设：`rush`(渐快冲刺) / `slowmo`(冲入慢放) / `hero`(慢-快-慢) / `punch`(脉冲)，
  另有 `SpeedCurve.ramp/sampled/constant` 供自定义。
- **顺带修 bug**：`FFmpegExecutor.adjust_speed` 原来直接写 `-af atempo=speed`，
  `speed > 2` 直接报错、源无音轨时 `-af` 也必失败；改为多级串联 + 音轨探测。
- CLI：`--speed-curve` / `--speed` / `--speed-steps`。
- 冒烟用双频音频 + FFT：2x 后仍读到 300Hz / 1200Hz（**变速不变调**），
  音视频流时长差 0.090s，斜坡时长与解析积分仅差 0.003s。

#### F2.4 实现记录
- 新增 `packages/audio_mixer`（`AudioTrack` / `AudioMixer` / `mix_tracks_from_spec`）。
- 通用轨道模型：`role`(voice/bgm/sfx/other) + `gain_db` 独立增益 + `start` 延迟入场
  + `loop` 循环铺满 + `duck_windows` 闪避区间。
- **自动闪避**：对 `role="voice"` 的轨跑 `silencedetect` 取补集得到说话时段，
  自动写入 `role="bgm"` 轨；闪避仍用 `volume` 的 `enable='between(t,a,b)+...'`
  时间窗（不用 `sidechaincompress`，避免被 `apad` 补长时挂起）。
- **关键点**：`amix` 必须 `normalize=0`，否则会把总电平除以轨数，设的增益全白费；
  所有输入统一 `aformat` 到同一采样率/声道，避免 amix 格式不一致失败。
- CLI：`--audio-mix SPEC.json`（spec 不写 video 时自动以本次成片为底）。
- 冒烟用**频段能量**定量验证：人声窗识别为 (1.0,2.0)/(4.0,5.0) 完全正确、
  闪避落差 13.5dB、BGM 从 0dB 调到 -20dB 实测差 **20.0dB**（严格线性）、
  三轨（300/1200/2600Hz）齐全、时长 6.00s。

#### F2.5 实现记录
- 新增 `packages/webui/src/preview.py`：
  - `get_proxy` 低码率代理（等比缩到 480，crf 32），前端秒开播放；
  - `get_param_preview` **参数预览**：只渲染一小段（默认前 6 秒）+ 小尺寸 +
    带调色，拖完参数约 1.5s 就能看到效果，不用整片重渲；
  - 两者都按「源文件 路径+mtime+大小 + 参数」的内容指纹缓存到 `cache/preview/`，
    命中缓存 build_ms = 0。
- 参数预览是**分步复用**已实现的模块（缩片 → `apply_color_grade` → `burn_ass`
  烧字幕），而不是在预览里另写一套滤镜 —— 片段小所以每步都便宜，
  好处是预览效果与成片一致，不会出现"预览好看成片不对"。
- 预览带字幕时会把字幕时间轴按 `start` 左移并裁窗（`_shift_subtitles`），
  保证片段与字幕对齐。
- WebUI 新端点：`GET /api/task/{id}/proxy`、`GET /api/task/{id}/proxy/video`、
  `POST /api/task/{id}/preview`、`GET /api/task/{id}/preview/{pid}`、
  `POST /api/preview/clear`；前端结果区改为 `<video>` 播代理并提供「参数预览」按钮。
- 冒烟 `scripts/smoke_preview.py`：代理 282kbps（原片 1408kbps）、
  参数预览 1.5s 出片（验收要求 ≤10s）、缓存命中 0ms，并用 FastAPI `TestClient`
  把 4 个端点真实打了一遍；另启动过真实 uvicorn 服务验证页面与路由。

**Phase 2 完成 → 已打 tag `v0.6`。** 新增 3 个能力包（shot_index / audio_mixer /
webui.preview），扩写 2 个（video_understanding.subject_tracker / auto_reframe、
montage_engine.speed_curve），新增 5 个自包含冒烟脚本。

#### F2.2 实现记录
- **发现真 bug**：原 `auto_reframe` 的动态裁切表达式写成
  `if(between(t,a,b),x,x)` 再相加，数学上**恒等于 x**，从未真正跟随主体；
  且强依赖 `cv2`（本机未装）→ 直接 ImportError。
- 新增 `packages/video_understanding/src/subject_tracker.py`：**纯 numpy/Pillow**
  的 显著性（局部对比度 + 饱和度） + 帧差运动 → x/y 能量投影 → 滑动窗口定位 →
  EMA + 移动平均平滑。`cv2` 仅作可选人脸增强，缺失时静默跳过。
- **关键算法细节**：当主体明显小于裁切窗口时，窗口和会出现**平台**，
  朴素 `argmax` 会固定在平台最左端导致窗口不动。改为「平台内挑中心最贴近
  能量质心」——既修好小主体，又保留多主体时选最密集窗口的鲁棒性。
- 重写 `auto_reframe`：生成**分段线性 `x(t)` 表达式**交给 ffmpeg `crop`
  时间表达式，一次编码得到连续平滑跟随；失败回退静态居中。
  `target_aspect` 现支持任意比例；CLI 新增 `--reframe-aspect "3:4"` 与
  `--reframe-method subject|center`。
- 冒烟 `scripts/smoke_track.py`：合成「白方块匀速左→右」视频做**相关性断言**
  （实测 corr=0.971），并断言裁切窗口像素序列单调右移、跟随裁切后画面中心亮度
  由 0 → 207（证明主体确实进了画面中心），另有真实素材端到端。
- 踩坑：`drawbox` 的时间表达式在本机 ffmpeg 上不渲染（画面全黑），
  改用 `overlay x='(W-w)*t/T'`，脚本内加自检防止再犯。
- **修复（`494cf39`）**：原 `auto_reframe` 把输出尺寸**硬编码** 1080x1920，与
  `target_aspect` 无关 → 设 `16/9` 时裁出 16:9 窗口后又强行 `scale` 到 9:16，
  画面被**非等比拉伸压扁**。改为 `_output_size()`：按 `target_aspect` 先查
  `_CANONICAL_SIZE`（9:16/2:3/3:4/4:5/1:1/4:3/16:9/21:9），未命中再按裁切窗口
  等比推导；源比例与目标一致时走等比缩放快速路径。`pipeline` 产物后缀改按实际
  比例命名（`_16x9`/`_4x5`…，不再恒为 `_9x16`）。回归：`tests/test_reframe.py`
  （12 例，`pytest` 23 passed）+ `smoke_track` 16:9 → 1920x1080 断言。

#### F2.1 实现记录
- 新增 `packages/shot_index`（`embedder` / `index_store` / `retriever`）。
- **双后端可插拔**：`ClipEmbedder`（transformers CLIP，真跨模态，支持
  「a smiling face」这类自由文本）；`HeuristicEmbedder`（纯 numpy/Pillow：
  亮度/对比度/饱和度/色温/边缘密度 + 24 维 RGB 直方图 = 30 维，文本侧靠中英
  关键词表映射到同一空间）。`get_embedder("auto")` 优先 CLIP，不可用则**静默降级**。
- **防卡死**：CLIP 无本地缓存且未设 `MONTAGE_CLIP_ALLOW_DOWNLOAD=1` 时**跳过加载**，
  避免离线环境卡在 HF 连接超时（实测降级耗时 0.26s）；`library_available` 用
  `find_spec` 而非 import，省掉 ~10s 重库导入。
- **建索引效率**：不对镜头逐个切分视频（太慢），改为**一次**低帧率抽帧（2fps）
  再给每个镜头挑时间最近帧；embedding 存 sidecar `.npy`，元数据存 JSON。
- 切点检测走 ffmpeg `select='gt(scene,X)'`，**不依赖 scenedetect/opencv**
  （本机两者均未安装）。
- CLI：`--index-shots [PATH]` / `--query-shots "文本"` / `--index-top-k` /
  `--index-export DIR` / `--embedder auto|clip|heuristic`。
- 冒烟 `scripts/smoke_index.py`：不满足于「跑通」，做**行为断言**——「暗」的
  top-1 代表帧平均亮度必须显著低于「亮」的 top-1（实测 28.4 vs 164.5，
  目视确认为暗场走廊 vs 明亮日戏）。

### Phase 3 — v0.7 / v1.0
| ID | 功能 | 状态 | commit | 完成日期 |
|----|------|------|--------|----------|
| F3.1 | 任务队列 + 持久化 | ☑ | `fe48893` | 2026-10-05 |
| F3.1-r | 任务队列 Redis 后端（可选） | ☑ | `fafba5e` | 2026-10-06 |
| F3.2 | Docker 部署 | ☐ | | |
| F3.3 | 一键投稿发布 | ☐ | | |
| F3.4 | 批量成片 | ☐ | | |
| F3.5 | 风格学习 / 模板市场 | ☐ | | |

#### F3.1 实现记录
- **技术选型（重要）**：计划里写的是 Celery/RQ + Redis，但本项目跑在**离线环境**
  （未装 Redis，也不该为"任务能重试"强绑一个中间件）。改用**纯标准库 SQLite +
  进程内 worker 线程**实现等价语义，接口做成可替换（将来接 Celery 只要实现同名
  方法，WebUI/CLI 不用改）。
- 新增 `packages/task_queue`：
  - `store.TaskStore`：SQLite（WAL + 单连接 + RLock，多线程安全）。
    `claim_next()` 用 `BEGIN IMMEDIATE` 事务 + 条件 UPDATE，**并发下同一任务不会被
    认领两次**（实测 6 线程抢 20 个任务，20 个唯一认领、零重复）。
    `mark_error()` 按 `min(base*2^(n-1), cap)` 退避重新入队，超 `max_attempts` 才落 error。
    `resume_stale()` 把 `running` 且 `updated_at` 超时的任务捞回队列；次数已用尽的
    直接转 error（**避免崩溃循环**）。
  - `store.TaskHandle`：dict 风格句柄，`handle["progress"]=40` 即写库。于是 WebUI 里
    170 行的 `_run_montage_task` **只改了一行**（`task = _tasks[id]` →
    `task = store.handle(id)`）就迁移到持久化存储。
  - `worker.TaskWorker`：后台 daemon 线程轮询；启动时先 `resume_stale()` 续跑；
    `stop()` 用 Event 中断睡眠实现优雅退出；handler 抛异常才触发重试。
  - `errors.TaskError(retryable=False)`：确定性失败（素材搜不到、参数非法）
    **不消耗重试次数**，一次落 error。
- WebUI（`packages/webui/src/app.py`）：
  - 删掉 `_tasks = {}` 内存字典，全部改走持久化任务表；
  - `/api/montage` 改为"落盘 + enqueue"，由 worker 执行（不再用 `BackgroundTasks`）；
  - FastAPI `lifespan` 启动/停止 worker；
  - 新增 `/api/tasks`（列表+统计）、`/api/task/{id}/retry`、`/api/task/{id}/cancel`；
  - SSE 进度接口只在 `done/error/canceled` 结束 —— `queued` 可能是"重试等待中"，
    要继续推送（比原来的内存版更正确）。
- CLI（`pipeline.py`）：`--task-list` / `--task-status` / `--task-show` /
  `--task-retry` / `--task-cancel` / `--task-resume` / `--task-stale` / `--task-db`。
- 门禁：
  - `pytest` **35 passed**（新增 `tests/test_task_queue.py` 10 例，含 WebUI 集成）；
  - `scripts/smoke_task_queue.py` 6 项可证伪断言全过：持久化跨连接 /
    原子认领 / 退避重试(attempts=3 & 退避曲线 [1,2,4,8,8,8] & 到点才可认领) /
    不可重试不重试 / 断点续跑 / worker 生命周期。
  - **真实 uvicorn 端到端**：起服务提交任务→执行→终态；随后**杀进程**、注入一条
    卡在 `running` 的任务、重启服务 → 自动重新入队并执行（attempts 1→2、
    `started_at` 更新），确证"进程重启不丢任务"。
- 后续可选项：worker 目前是**进程内线程**；要横向扩展可换成独立 worker 进程
  （接口已就绪），或替换为 Celery/RQ 后端。

#### F3.1-r 实现记录（Redis 后端，可选扩展）
- **定位**：SQLite 是默认后端（离线可用，单机够用）。当需要**多进程 / 多机共享同一张
  任务表**（多个 WebUI 实例、独立 worker 进程、横向扩容）时，切 Redis：
  `MONTAGE_TASK_BACKEND=redis` + `MONTAGE_REDIS_URL=redis://host:6379/0`。
  WebUI / CLI / worker 业务代码**一行不改**（`runtime.get_store()` 按环境变量选实现）。
- 新增 `packages/task_queue/src/redis_store.py`，与 `TaskStore` **同接口**：
  - 数据结构：`{prefix}:task:<id>`(HASH) / `:queued`(ZSET，score=`seq-priority*1e12`
    编码排队次序) / `:running`(ZSET，score=updated_at，供 stale 扫描) / `:ids`(ZSET) / `:seq`。
  - `claim_next()` 用 **Lua `EVAL`** 在服务端原子完成「找→校验退避→改状态+attempts+1→
    移出 queued→记入 running」，多进程并发也不会重复认领。
  - `runtime.py` 增加落后端切换；`__init__` 导出 `RedisTaskStore` / `redis_available` /
    `default_backend` / `default_redis_url`。
- **真实 Redis 验证中发现的两个兼容性坑（只有真服务端才暴露，已修）**：
  1. **RESP3/HELLO**：redis-py ≥ 8 默认 `protocol=3`，握手先发 `HELLO`，而老 Redis
     3.0.504 不认识 → `unknown command 'HELLO'`。必须显式 `protocol=2`。
  2. **多字段 HSET**：`HSET key f1 v1 f2 v2` 是 Redis **4.0** 才有的；3.x 只认
     `HSET key field value`。redis-py 的 `hset(mapping=...)` 恰好发多字段形式 →
     `wrong number of arguments for 'hset'`。改为 **逐字段 HSET**（Lua 内同样拆开）。
- **接口一致性修复**：Redis 版 `update()` 原本用当前时钟覆盖 `updated_at`，而 SQLite 版
  尊重调用方显式传入的值（`updated_at` 本身在 `_COLUMNS` 内）→ 导致 `resume_stale`
  的 stale 判定在 Redis 上失效（测试抓出）。已对齐语义；顺带删掉 SQLite 里那段永远
  走不到的 `elif key == "updated_at": continue` 死分支。
- 门禁：
  - `pytest` **42 passed**（`tests/test_task_queue.py` 新增 7 例 Redis 测试，
    `skipif(not redis_available(...))` 门控，无 Redis 的机器自动跳过、不影响全绿）；
  - `scripts/smoke_task_queue_redis.py` **25 项可证伪断言全过**（真 Redis 实例）：
    基础读写 / 退避曲线 `[1,2,4,8,8]` / 非重试落终态 / 6 线程×20 任务原子抢单零重复 /
    `resume_stale` 计数 `{resumed:1, exhausted:1}` / worker 端到端失败重试到 done /
    **CLI `pipeline.py --task-list` 走 Redis 后端并列出任务**；
  - `scripts/smoke_task_queue.py`（SQLite）6 项**无回归**；
  - Redis 连不上时该冒烟脚本打印 SKIP 并退出码 0（不拖垮无 Redis 环境的门禁）。
- 验证环境：用户本机 `D:\programs\Redis-x64-3.0.504`（Redis **3.0.504** Windows 版），
  `redis-py 8.0.0`。

---

## 7. 依赖与风险

| 项 | 说明 | 应对 |
|----|------|------|
| `edge-tts` 网络可用性 | 需联网合成语音 | 提供本地 TTS 适配器接口作为降级 |
| 音效版权 | 不能用来源不明的音效 | 仅内置 CC0 素材 |
| CLIP 权重需联网下载 | 离线环境无法加载 CLIP | 已实现 `HeuristicEmbedder` 离线兜底 + 无缓存时跳过加载避免卡死 |
| FFmpeg 版本差异 | `xfade`/`sidechaincompress` 行为不一 | 启动时探测 ffmpeg 版本并降级 |
| 平台投稿 API 门槛 | 需开发者资质/审核 | 先做本地导出 + 手动上传引导，再放开 API |
| 测试素材不足 | `test_assets/` 仅 5 段 | 补一段带人声的视频用于旁白/字幕冒烟 |

---

## 8. 建议的开工顺序

1. **先做 F1.5**（1 人日，打通已有能力，最快建立"commit + 门禁"节奏）
2. **再做 F1.1 + F1.2**（本项目最大价值增量：内容化）
3. 然后 F1.3 → F1.4，收尾 Phase 1 并打 tag `v0.5`
4. Phase 2 起每完成一个大功能打一次 tag

> 具体从哪一项开始，由你拍板；确认后我按 §1 的规范逐个实现并提交。
