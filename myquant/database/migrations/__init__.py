# 文件名：myquant/database/migrations/__init__.py
"""
migrations - 数据库迁移框架
==========================
功能清单:
  1. 数据库版本管理（schema_version表记录当前版本）
  2. 增量升级（从低版本逐步升级到最新版本）
  3. 版本回滚（支持降级到指定版本）
  4. 迁移状态查询（当前版本/可用迁移/迁移历史）

设计原则:
  - 每个迁移是一个独立的函数，包含upgrade和downgrade
  - 迁移在事务中执行，失败自动回滚
  - 迁移历史记录在schema_migrations表中，可追溯
  - 为后续表结构变更预留扩展能力

依赖: myquant.database.connection, myquant.database.schema
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from ..connection import DatabaseConnection


# ─────────────────────────────────────────────
# 迁移版本定义
# ─────────────────────────────────────────────

# 当前最新版本号
CURRENT_VERSION = 2

# 迁移注册表：{版本号: (upgrade函数, downgrade函数, 描述)}
_migration_registry: Dict[int, Tuple[Callable, Callable, str]] = {}


def register_migration(
    version: int,
    upgrade: Callable,
    downgrade: Callable,
    description: str = "",
) -> None:
    """注册一个迁移版本"""
    _migration_registry[version] = (upgrade, downgrade, description)


# ─────────────────────────────────────────────
# 版本1：初始建表
# ─────────────────────────────────────────────

def _upgrade_v1(conn: DatabaseConnection) -> None:
    """版本1升级：创建所有初始表和索引"""
    from ..schema import create_all_tables
    create_all_tables(conn)


def _downgrade_v1(conn: DatabaseConnection) -> None:
    """版本1回滚：删除所有表"""
    from ..schema import drop_all_tables
    drop_all_tables(conn)


# 注册版本1
register_migration(1, _upgrade_v1, _downgrade_v1, "初始建表：行情/财务/因子/状态/数据源/同步状态")


# ─────────────────────────────────────────────
# 版本2：清洗元信息表 + 脏数据统计表（任务13）
# ─────────────────────────────────────────────

def _upgrade_v2(conn: DatabaseConnection) -> None:
    """
    版本2升级：创建清洗运行元信息表(clean_run_meta)与脏数据统计表(dirty_data_stats)

    注意：已有数据库升级时只补建这两张新表，不动旧表（CREATE TABLE IF NOT EXISTS 幂等）。
    """
    from ..schema import (
        CREATE_CLEAN_RUN_META_SQL,
        CREATE_DIRTY_DATA_STATS_SQL,
        CREATE_CLEAN_RUN_INDEX_SQL,
        CREATE_DIRTY_INDEX_SQL,
    )
    for sql in [
        CREATE_CLEAN_RUN_META_SQL,
        CREATE_DIRTY_DATA_STATS_SQL,
    ] + CREATE_CLEAN_RUN_INDEX_SQL + CREATE_DIRTY_INDEX_SQL:
        conn.execute_update(sql)


def _downgrade_v2(conn: DatabaseConnection) -> None:
    """版本2回滚：删除清洗元信息表和脏数据统计表"""
    from ..schema import TABLE_NAMES
    # 先删子表（外键关联 clean_run_meta）
    conn.execute_update(f"DROP TABLE IF EXISTS {TABLE_NAMES['dirty_data_stats']}")
    conn.execute_update(f"DROP TABLE IF EXISTS {TABLE_NAMES['clean_run_meta']}")


# 注册版本2
register_migration(2, _upgrade_v2, _downgrade_v2, "新增清洗元信息表+脏数据统计表（任务13）")


# ─────────────────────────────────────────────
# 迁移管理类
# ─────────────────────────────────────────────

class MigrationManager:
    """
    数据库迁移管理器
    ================
    管理数据库schema版本的升级和回滚。

    Usage:
        from myquant.database import get_connection
        from myquant.database.migrations import MigrationManager
        conn = get_connection()
        manager = MigrationManager(conn)

        # 升级到最新版本
        manager.upgrade()

        # 查看当前版本
        print(manager.current_version())
    """

    def __init__(self, connection: DatabaseConnection):
        self.conn = connection
        self._ensure_migration_table()

    def _ensure_migration_table(self) -> None:
        """确保迁移记录表存在"""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT,
                applied_at  TEXT DEFAULT (datetime('now')),
                is_current  INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()

    def current_version(self) -> int:
        """获取当前数据库版本号"""
        row = self.conn.execute_one(
            "SELECT MAX(version) as ver FROM schema_migrations WHERE is_current = 1"
        )
        return row["ver"] if row and row["ver"] else 0

    def available_migrations(self) -> List[Tuple[int, str]]:
        """获取所有可用的迁移版本"""
        return sorted(
            [(v, desc) for v, (_, _, desc) in _migration_registry.items()],
            key=lambda x: x[0],
        )

    def upgrade(self, target_version: Optional[int] = None) -> List[int]:
        """
        升级数据库到指定版本

        Parameters
        ----------
        target_version : int | None
            目标版本，None则升级到最新版本

        Returns
        -------
        list[int] : 已应用的迁移版本列表
        """
        if target_version is None:
            target_version = CURRENT_VERSION
        current = self.current_version()
        applied = []

        for version in range(current + 1, target_version + 1):
            if version not in _migration_registry:
                raise ValueError(f"未找到版本 {version} 的迁移")
            upgrade_fn, _, description = _migration_registry[version]
            # 在事务中执行迁移
            with self.conn.transaction():
                upgrade_fn(self.conn)
                # 记录迁移
                self.conn.execute_update(
                    "UPDATE schema_migrations SET is_current = 0 WHERE is_current = 1"
                )
                self.conn.execute_update(
                    "INSERT OR REPLACE INTO schema_migrations (version, description, is_current) VALUES (?, ?, 1)",
                    (version, description),
                )
            applied.append(version)

        return applied

    def downgrade(self, target_version: int) -> List[int]:
        """
        回滚数据库到指定版本

        Parameters
        ----------
        target_version : int
            目标版本（回滚到此版本，高于此版本的迁移都回滚）

        Returns
        -------
        list[int] : 已回滚的迁移版本列表
        """
        current = self.current_version()
        rolled_back = []

        for version in range(current, target_version, -1):
            if version not in _migration_registry:
                raise ValueError(f"未找到版本 {version} 的迁移")
            _, downgrade_fn, _ = _migration_registry[version]
            with self.conn.transaction():
                downgrade_fn(self.conn)
                self.conn.execute_update(
                    "DELETE FROM schema_migrations WHERE version = ?",
                    (version,),
                )
                if version - 1 > 0:
                    self.conn.execute_update(
                        "UPDATE schema_migrations SET is_current = 1 WHERE version = ?",
                        (version - 1,),
                    )
            rolled_back.append(version)

        return rolled_back

    def migration_history(self) -> List[Dict]:
        """获取迁移历史记录"""
        rows = self.conn.execute(
            "SELECT * FROM schema_migrations ORDER BY version ASC"
        )
        return [dict(row) for row in rows]

    def is_latest(self) -> bool:
        """是否已是最新版本"""
        return self.current_version() >= CURRENT_VERSION


# ─────────────────────────────────────────────
# 便捷函数
# ─────────────────────────────────────────────

def migrate_to_latest(connection: DatabaseConnection) -> List[int]:
    """一键升级到最新版本"""
    manager = MigrationManager(connection)
    return manager.upgrade()


def get_database_version(connection: DatabaseConnection) -> int:
    """获取当前数据库版本"""
    manager = MigrationManager(connection)
    return manager.current_version()
