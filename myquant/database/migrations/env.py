# 文件名：myquant/database/migrations/env.py
"""
Alembic 环境配置 — Pythonwdhmm 鲲鹏量化交易系统
================================================
功能：
  1. 自动探测项目根目录，解析 SQLite 数据库绝对路径
  2. 支持 online 模式（连接数据库执行迁移）和 offline 模式（生成SQL脚本）
  3. 迁移在事务中执行，失败自动回滚
  4. 与项目 raw sqlite3 风格一致，迁移脚本用 op.execute() 执行原始SQL
  5. 支持环境变量 QUANT_DB_URL 覆盖数据库路径（实盘/测试切换）

设计原则：
  - 不依赖 SQLAlchemy ORM metadata（项目用原始SQL建表）
  - 数据库路径与 myquant/database/connection.py 的 DEFAULT_DB_FILENAME 一致
  - 为实盘无缝对接预留：环境变量覆盖、多数据库支持
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

logger = logging.getLogger("alembic.env")

# ─────────────────────────────────────────────
# 项目根目录自动探测（向上查找 pyproject.toml）
# ─────────────────────────────────────────────

def _find_project_root() -> Path:
    """从当前文件位置向上探测项目根目录"""
    current = Path(__file__).resolve()
    for _ in range(10):
        current = current.parent
        if (current / "pyproject.toml").exists():
            return current
    # 回退：env.py 位于 myquant/database/migrations/，上三级是项目根
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _find_project_root()

# 将项目根加入 sys.path，确保迁移脚本可 import 项目模块
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ─────────────────────────────────────────────
# Alembic Config 对象
# ─────────────────────────────────────────────

config = context.config

# 日志配置（如果 alembic.ini 中配置了 loggers）
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception as e:
        # #R1修复：日志配置失败不影响迁移主流程，但记录debug日志便于排查
        logger.debug(f"Alembic logging config failed: {e}")

# ─────────────────────────────────────────────
# 数据库 URL 解析
# ─────────────────────────────────────────────

def _resolve_db_url() -> str:
    """
    解析数据库连接URL：
    1. 环境变量 QUANT_DB_URL 优先（实盘/测试切换）
    2. alembic.ini 中的 sqlalchemy.url
    3. SQLite 相对路径转为基于项目根的绝对路径
    """
    # 环境变量优先
    env_url = os.environ.get("QUANT_DB_URL")
    if env_url:
        return env_url

    # 从 alembic.ini 读取
    url = config.get_main_option("sqlalchemy.url", "")

    # SQLite 相对路径转绝对路径
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        # sqlite:///relative/path → 相对路径
        rel_path = url[len("sqlite:///"):]
        abs_path = (PROJECT_ROOT / rel_path).resolve()
        # 确保父目录存在
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{abs_path}"

    return url


# 设置解析后的 URL
_db_url = _resolve_db_url()
config.set_main_option("sqlalchemy.url", _db_url)

# ─────────────────────────────────────────────
# target_metadata（项目用原始SQL，不使用SQLAlchemy ORM metadata）
# 如果未来引入 SQLAlchemy 模型，在此绑定 Base.metadata
# ─────────────────────────────────────────────

target_metadata = None


# ─────────────────────────────────────────────
# Offline 模式：不连接数据库，直接生成 SQL 脚本
# ─────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    离线模式：以 'offline' 方式运行迁移。
    不连接数据库，将 SQL 输出到 stdout 或文件。
    用法：alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite 特殊配置
        render_as_batch=True,  # SQLite 不支持部分ALTER，用batch模式
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ─────────────────────────────────────────────
# Online 模式：连接数据库执行迁移
# ─────────────────────────────────────────────

def run_migrations_online() -> None:
    """
    在线模式：连接数据库并执行迁移。
    每个迁移在独立事务中执行，失败自动回滚。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # 迁移用短连接，不需要连接池
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite batch模式
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


# ─────────────────────────────────────────────
# 入口：根据是否离线模式分发
# ─────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
