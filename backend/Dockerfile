# 前端使用 React + Vite 构建静态资源。
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json ./package.json
COPY frontend/index.html ./index.html
COPY frontend/vite.config.js ./vite.config.js
COPY frontend/src ./src
RUN npm install && npm run build

# 使用精简版 Python 3.12 镜像。
FROM python:3.12-slim
# 安装 FluidSynth 与 Debian 提供的免费 GM 采样音源。
RUN apt-get update && apt-get install -y --no-install-recommends fluidsynth fluid-soundfont-gm ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# 将容器内的工作目录设为 /app。
WORKDIR /app
# 先安装依赖，单独修改应用代码时可以复用这一构建层。
COPY backend/requirements.txt requirements.txt
# 使用清华 PyPI 镜像安装 Python 依赖，保留 BuildKit 中的 pip 下载缓存。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -r requirements.txt
# 复制 API 和 MCP 服务入口。
COPY backend/app.py .
# 复制音乐生成模块。
COPY backend/generation.py .
COPY backend/resume.py .
COPY backend/instrumental.py .
COPY backend/runtime_config.py .
COPY backend/arrangement_skills ./arrangement_skills
COPY backend/web ./web
COPY --from=frontend /backend/react-dist ./react-dist
# 监听容器的所有网卡，使同一容器网络中的服务可通过 8000 端口访问 API。
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
