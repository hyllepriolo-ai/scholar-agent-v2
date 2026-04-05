FROM python:3.10-slim

# 设置环境变量，确保 Python 输出直接打印到控制台，不留缓存
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# 安装系统依赖（如需对 PDF 等处理补充库可在此进行）
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装依赖
COPY requirements.txt /app/
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 复制项目核心目录
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# 创建临时挂载目录
RUN mkdir -p /app/uploads /app/exports

# 暴露端口
EXPOSE 8000

# 启动命令：使用 sh 以便支持 Heroku/Railway 动态指定的 $PORT 环境变量
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
