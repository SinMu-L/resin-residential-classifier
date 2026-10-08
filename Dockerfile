FROM python:3.12-slim

# 以非 root 用户运行（通过 entrypoint 中 gosu 降权）
RUN groupadd -r app && useradd -r -g app app

WORKDIR /app

# 先装依赖，利用层缓存；PIP_INDEX_URL 可指定镜像源（如国内 build 加速）
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

# 复制应用代码
COPY app ./app
COPY scripts ./scripts

# 安装 gosu（启动阶段从 root 降权）与 ca-certificates（HTTPS 下载离线库所需）
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 应用数据目录：
#   /data/app   应用 SQLite（容器外持久化）
#   /data/source Resin 数据源（只读挂载）
#   /data/mmdb   离线库 Merged-IP.mmdb（可写挂载；由 entrypoint 下载/更新）
RUN mkdir -p /data/app /data/source /data/mmdb && chown -R app:app /data/app /data/mmdb /app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
