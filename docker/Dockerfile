# 文件名：docker/Dockerfile
# Pythonwdhmm 鲲鹏量化交易系统 — Docker镜像构建文件
# 基于Python官方镜像，安装项目与所有依赖，为回测和实盘运行提供一致环境

# ===== 基础镜像 =====
FROM python:3.10-slim

# ===== 维护者信息 =====
LABEL maintainer="Pythonwdhmm"
LABEL description="沪深A股日线量化回测与实盘交易系统"
LABEL version="0.1.0"

# ===== 设置环境变量 =====
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ===== 设置工作目录 =====
WORKDIR /app

# ===== 安装系统依赖 =====
# gcc: 编译C扩展（numpy/pandas等需要）
# curl: 健康检查
# tzdata: 时区设置
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# ===== 先复制依赖文件，利用Docker缓存层 =====
COPY pyproject.toml README.md ./

# ===== 安装Python依赖 =====
# 安装核心依赖 + 实盘依赖 + 开发依赖
# 实盘依赖（websocket/aiohttp/schedule）为实时行情和券商对接预留
RUN pip install --no-cache-dir -e ".[live,dev]"

# ===== 复制项目代码 =====
COPY . .

# ===== 创建数据和输出目录 =====
RUN mkdir -p datasets/raw datasets/processed datasets/features \
    outputs logs database 实盘状态 实盘信号

# ===== 暴露端口（REST API；8501 为 dashboard 服务专用，疑点1确认后不再在此暴露）=====
EXPOSE 8000

# ===== 健康检查 =====
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1   # M10: 对齐真实路由（/health 不存在返回404）

# ===== 默认启动命令 =====
# M10修复: 默认启动 REST API 服务（健康检查可达、对外提供回测/实盘/数据接口），
# 原 CMD 为 run_回测入口.py --help，容器启动即退出且不监听 8000。
# 需运行回测/实盘时用 docker run 覆盖 command。
CMD ["python", "scripts/运行API.py", "--host", "0.0.0.0", "--port", "8000"]
