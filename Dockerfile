FROM python:3.13-slim

# 服务器在国内，走腾讯云镜像源；本地构建可 --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=http://mirrors.tencentyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.tencentyun.com
# 🔴 不要再挂 --extra-index-url https://pypi.org/simple。
# 实测（腾讯云部署机，2026-09-29）：pypi.org 从腾讯云机器上只有 49 KB/s，
# 而腾讯云内网源有 64 MB/s。pip 对**每一条** requirement 都会去 extra-index
# 探一次，结果是构建 16 分钟零字节卡死。要加备用源就显式传这个 ARG，默认留空。
ARG PIP_EXTRA_INDEX_URL=

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MJ_DB_URL=postgresql+psycopg://mingjian:mingjian@postgres:5432/mingjian \
    MJ_STORAGE_ROOT=/app/.data/files \
    MJ_PORT=8000

WORKDIR /app

# Debian 源：默认是官方 deb.debian.org，国内机器上慢到能卡住构建（实测拉 9.6MB 的
# Packages 索引要几分钟）。部署时传 --build-arg DEBIAN_MIRROR=http://mirrors.tencentyun.com/debian
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security

# python:3.13-slim 是 Debian trixie，源是 deb822 格式的 debian.sources，直接整体重写
RUN printf 'Types: deb\nURIs: %s\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: %s\nSuites: trixie-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
        "$DEBIAN_MIRROR" "$DEBIAN_SECURITY_MIRROR" > /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 可观测是可选依赖：默认不装（langfuse 带 OpenTelemetry 全家桶，内网镜像下能把构建拖到 20 分钟）
ARG INSTALL_OBSERVE=0

# P5：Agentic RAG 的 MCP 子进程跑在 api 容器里，依赖必须一起打进镜像。
# 默认关掉 —— 装 torch CPU + FlagEmbedding 会让镜像从 ~1G 涨到 ~2.5G，
# 纯跑平台主链路的机器没必要付这个代价。
ARG INSTALL_RAG=0
# torch 的 CPU 轮子不在 PyPI 上，只有 PyTorch 官方源（国内走 aliyun 镜像）
# 🔴 不能对 aliyun 这个源用 `pip install --index-url`：它只是一个目录自动索引页，
# 文件名里的 `+` 被转义成 `&#43;`（torch-2.9.1&#43;cpu-cp313-...whl），pip 解析不出
# 版本号，直接报 "from versions: none"。所以这里改成 curl 按完整文件名把 wheel
# 拉下来、再本地安装。cp313 与基础镜像 python:3.13-slim 对应，升级 Python 要同步改。
ARG TORCH_INDEX_URL=https://mirrors.aliyun.com/pytorch-wheels/cpu/
# 注意两个名字：URL 里 `+` 必须写成 %2B，落盘的文件名必须是**原始 wheel 名**
# （含 +cpu/cp313 这些 tag）。pip 装本地 wheel 是靠文件名拆 tag 的，
# 存成 torch.whl 会报 "Invalid wheel filename (wrong number of parts)"。
ARG TORCH_WHEEL=torch-2.9.1+cpu-cp313-cp313-manylinux_2_28_x86_64.whl

COPY requirements.txt .
COPY requirements-observe.txt .
COPY requirements-rag.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" \
        ${PIP_EXTRA_INDEX_URL:+--extra-index-url "$PIP_EXTRA_INDEX_URL"} \
        -r requirements.txt \
 && if [ "$INSTALL_OBSERVE" = "1" ]; then \
        pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" \
            ${PIP_EXTRA_INDEX_URL:+--extra-index-url "$PIP_EXTRA_INDEX_URL"} -r requirements-observe.txt; \
    fi

RUN if [ "$INSTALL_RAG" = "1" ]; then \
        TORCH_URL="$TORCH_INDEX_URL/$(printf '%s' "$TORCH_WHEEL" | sed 's/+/%2B/g')" \
     && curl -fsSL --retry 3 -o "/tmp/$TORCH_WHEEL" "$TORCH_URL" \
     && pip install --no-cache-dir "/tmp/$TORCH_WHEEL" \
     && rm -f "/tmp/$TORCH_WHEEL" \
     && pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" \
            ${PIP_EXTRA_INDEX_URL:+--extra-index-url "$PIP_EXTRA_INDEX_URL"} -r requirements-rag.txt; \
    fi

COPY src ./src
COPY scripts ./scripts
# P2：技能目录必须进镜像，否则容器内 create_deep_agent(skills=...) 挂载不到 deep-research
COPY skills ./skills
RUN mkdir -p /app/.data/files

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
