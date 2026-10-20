# AI Montage Agent Makefile

.PHONY: install dev test lint run-webui demo clean format check

# 安装依赖
install:
	pip install -r requirements.txt

# 开发模式安装
dev:
	pip install -r requirements.txt
	pip install pytest black ruff mypy

# 运行测试
test:
	pytest tests/ -v

# 代码检查
lint:
	black packages/ scripts/ tests/
	ruff check packages/ scripts/ tests/
	mypy packages/

# 运行 WebUI
run-webui:
	python pipeline.py --webui

# 运行演示
demo:
	python scripts/demo/make_demo.py

# 清理临时文件
clean:
	rm -rf temp/ var/ __pycache__ .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# 格式化代码
format:
	black packages/ scripts/ tests/

# 检查代码
check:
	ruff check packages/ scripts/ tests/
	mypy packages/

# 帮助
help:
	@echo "可用命令:"
	@echo "  make install    - 安装依赖"
	@echo "  make dev        - 开发模式安装"
	@echo "  make test       - 运行测试"
	@echo "  make lint       - 代码检查"
	@echo "  make run-webui  - 运行 WebUI"
	@echo "  make demo       - 运行演示"
	@echo "  make clean      - 清理临时文件"
	@echo "  make format     - 格式化代码"
	@echo "  make check      - 检查代码"
