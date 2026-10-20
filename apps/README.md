# Apps

可运行入口说明：

- **主 CLI**：仓库根目录 `pipeline.py`  
  ```bash
  python pipeline.py --movies a.mp4 b.mp4 --bgm bgm.mp3 --sfx
  ```
- **WebUI**：`python pipeline.py --webui` → http://localhost:8000
- **`worker/`**：旧版 Typer CLI，保留用于历史命令，已不再作为主入口；新工作请使用根目录 `pipeline.py`。
- `api/` 旧桩已移除（之前只有 `time.sleep(2)`，不产生真实结果）。

核心实现位于 `packages/montage/`，根 `pipeline.py` 只是向后兼容入口。
