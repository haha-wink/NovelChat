# 后端镜像（轻量部署用）
#
# 构建：docker build -t novel-roleplay-backend ./backend
# 运行依赖：backend/.env（密钥）、backend/data（数据库与原文，建议挂卷）
#
# 体积说明：requirements 里有 torch，默认会拉 CUDA 版（2GB+）。
# 这里显式装 CPU 版，镜像能小 1.5GB 左右；要 GPU 推理就把 TORCH_INDEX 改回空。

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# 先装依赖，改代码时不用重装
COPY requirements.txt ./
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN if [ -n "$TORCH_INDEX" ]; then \
        pip install --index-url "$TORCH_INDEX" torch==2.15.0; \
    fi \
 && pip install -r requirements.txt

# 只拷代码；data/ 由 compose 挂卷（数据库、原文、模型都在里面）
COPY app ./app
COPY data/stickers ./data/stickers

EXPOSE 8000

# --app-dir app：backend/app 是导入根，模块名保持 core.* / services.*
CMD ["uvicorn", "main:app", "--app-dir", "app", "--host", "0.0.0.0", "--port", "8000"]