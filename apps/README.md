# Apps

可运行入口说明：

- **主 CLI**：仓库根目录 `pipeline.py`  
  ```bash
  python pipeline.py --movies a.mp4 b.mp4 --bgm bgm.mp3 --sfx
  ```
- **WebUI**：`python pipeline.py --webui` → http://localhost:8000

旧版 `worker/` Typer CLI 和只有 `time.sleep(2)` 的 `api/` 桩已移除。
核心实现位于 `packages/montage/`，根 `pipeline.py` 只是向后兼容入口。
