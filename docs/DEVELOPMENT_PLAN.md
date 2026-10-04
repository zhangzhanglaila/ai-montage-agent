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
| 卡点同步 | 镜头对齐节拍 + 变速 | `pipeline.py: BeatSyncEngine(427)` |
| 渲染 | FFmpeg 拼接/转场/混音 | `pipeline.py: VideoRenderer(630)` |
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
| `pipeline.py: MontagePipeline.run()` L1117–1177 | 渲染后处理链：色彩协调 → 渲染 → 对话闪避 → 调色 → 竖屏裁切 |
| `pipeline.py: main()` L1347–1358 | CLI 后处理链：`_apply_enhancement` → `_apply_subtitles` → `_export_timeline` |
| `packages/webui/src/app.py: _run_montage_task()` L33 | WebUI 后台任务入口，新功能需同步加参数 |
| `packages/core_types/models.py` | 共享数据类；`Shot.embedding` 字段**已预留** |

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
| F3.1 | 任务队列 + 持久化 | Celery/RQ + Redis + SQLite/Postgres，替换内存态任务表 | 6–8 | `feat(infra): 引入任务队列与任务持久化` |
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
| F2.2 | 人脸/主体追踪 | ☑ | `906681c` | 2026-10-04 |
| F2.3 | 变速曲线 | ☑ | `c9d2cf3` | 2026-10-04 |
| F2.4 | 多轨音频混音 | ☑ | `PENDING` | 2026-10-04 |
| F2.5 | 实时预览 | ☑ | `PENDING` | 2026-10-04 |

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
| F3.1 | 任务队列 + 持久化 | ☐ | | |
| F3.2 | Docker 部署 | ☐ | | |
| F3.3 | 一键投稿发布 | ☐ | | |
| F3.4 | 批量成片 | ☐ | | |
| F3.5 | 风格学习 / 模板市场 | ☐ | | |

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
