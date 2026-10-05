# 文件名：myquant/database/connection.py
"""
connection.py - 数据库连接管理
==============================
功能清单:
  1. SQLite同步连接管理（线程安全）
  2. 连接池（多线程场景复用连接）
  3. 事务上下文管理器（with语句自动提交/回滚）
  4. 批量执行优化（executemany）
  5. 数据库文件路径自动探测（项目根目录/database/quant.db）
  6. 为异步连接预留接口（实盘高并发场景）

依赖: sqlite3, threading, pathlib, contextlib
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from myquant.core.logger import get_logger

logger = get_logger("database.connection")

# ─────────────────────────────────────────────
# 常量定义
# ─────────────────────────────────────────────

# 默认数据库文件名
DEFAULT_DB_FILENAME = "quant.db"

# 连接池最大连接数
DEFAULT_POOL_SIZE = 5

# SQLite pragmas（性能优化）
DEFAULT_PRAGMAS = {
    "journal_mode": "WAL",          # 写前日志，支持并发读写
    "synchronous": "NORMAL",         # 平衡性能和安全性
    "cache_size": "-64000",          # 64MB缓存
    "foreign_keys": "ON",            # 启用外键约束
    # M9修复: 默认 FILE——MEMORY 在大查询(排序/临时表)时占满内存导致 OOM；
    # FILE 使用磁盘临时文件，超大查询安全；需极速小查询可显式传 pragmas 覆盖
    "temp_store": "FILE",            # 临时表存磁盘（防 OOM，可覆盖）
    # BASE-6: busy_timeout 显式化（毫秒；与 sqlite3.connect(timeout=30) 一致，
    # 高写入/锁竞争时写等待而非立即失败，配合 WAL 消除锁表）
    "busy_timeout": "30000",
}


# ─────────────────────────────────────────────
# 数据库连接类
# ─────────────────────────────────────────────

class DatabaseConnection:
    """
    SQLite数据库连接管理器
    ======================
    线程安全，支持连接池和事务管理。

    Usage:
        # 方式1：直接使用
        conn = DatabaseConnection("path/to/quant.db")
        conn.connect()
        rows = conn.execute("SELECT * FROM market_data")
        conn.close()

        # 方式2：上下文管理器（自动关闭）
        with DatabaseConnection("path/to/quant.db") as conn:
            rows = conn.execute("SELECT * FROM market_data")

        # 方式3：事务（自动提交/回滚）
        with DatabaseConnection("path/to/quant.db") as conn:
            with conn.transaction():
                conn.execute("INSERT INTO ...")
                conn.execute("UPDATE ...")
    """

    def __init__(
        self,
        db_path: Union[str, Path],
        pool_size: int = DEFAULT_POOL_SIZE,
        pragmas: Optional[Dict[str, str]] = None,
    ):
        """
        Parameters
        ----------
        db_path : str | Path
            数据库文件路径
        pool_size : int
            连接池大小（默认5）
        pragmas : dict | None
            SQLite pragmas配置，None则使用默认值
        """
        self.db_path = Path(db_path)
        self.pool_size = pool_size
        self.pragmas = pragmas or DEFAULT_PRAGMAS
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connected = False
        # M1修复: 线程id→连接 注册表，close_all() 可跨线程关闭全部连接
        # （原 close() 只关当前线程连接，其他线程连接泄漏）
        self._thread_conns: Dict[int, sqlite3.Connection] = {}

    # ── 连接管理 ──

    def connect(self) -> None:
        """建立数据库连接（线程本地存储，每线程一个连接）"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            return
        # 确保目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        # 应用pragmas
        for key, value in self.pragmas.items():
            conn.execute(f"PRAGMA {key} = {value}")
        self._local.conn = conn
        with self._lock:
            self._thread_conns[threading.get_ident()] = conn
            self._connected = True

    def close(self) -> None:
        """关闭当前线程的数据库连接（并从注册表注销）"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
            with self._lock:
                self._thread_conns.pop(threading.get_ident(), None)

    def close_all(self) -> None:
        """关闭本实例全部线程的连接（跨线程，M1修复），程序退出/全局关闭时调用"""
        with self._lock:
            conns = list(self._thread_conns.values())
            self._thread_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception as e:
                # KP-P1-07: 不静默吞噬——关闭异常记 warning 留痕，确保清理不中断且可追溯
                logger.warning(f"[db] 关闭连接异常（忽略以继续清理）: {e}")
        if hasattr(self._local, "conn"):
            self._local.conn = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """当前线程是否已连接"""
        return hasattr(self._local, "conn") and self._local.conn is not None

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程连接，未连接则自动连接"""
        if not self.is_connected:
            self.connect()
        return self._local.conn

    # ── 执行操作 ──

    def execute(
        self,
        sql: str,
        params: Optional[Union[Tuple, Dict]] = None,
    ) -> List[sqlite3.Row]:
        """
        执行SQL语句并返回所有结果行

        Parameters
        ----------
        sql : str
            SQL语句
        params : tuple | dict | None
            参数

        Returns
        -------
        list[sqlite3.Row] : 查询结果行列表
        """
        conn = self._get_conn()
        cursor = conn.execute(sql, params or ())
        rows = cursor.fetchall()
        cursor.close()
        return rows

    def execute_one(
        self,
        sql: str,
        params: Optional[Union[Tuple, Dict]] = None,
    ) -> Optional[sqlite3.Row]:
        """执行SQL并返回单行结果（无结果返回None）"""
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    def execute_update(
        self,
        sql: str,
        params: Optional[Union[Tuple, Dict]] = None,
    ) -> int:
        """
        执行INSERT/UPDATE/DELETE，返回受影响行数

        注意：此方法不会自动提交，需在事务中调用或手动commit
        """
        conn = self._get_conn()
        cursor = conn.execute(sql, params or ())
        affected = cursor.rowcount
        cursor.close()
        return affected

    def executemany(
        self,
        sql: str,
        params_list: List[Union[Tuple, Dict]],
    ) -> int:
        """
        批量执行SQL，返回受影响总行数

        用于批量插入行情数据等场景，比循环execute快10倍以上
        """
        if not params_list:
            return 0
        conn = self._get_conn()
        cursor = conn.executemany(sql, params_list)
        affected = cursor.rowcount
        cursor.close()
        return affected

    def commit(self) -> None:
        """手动提交事务"""
        self._get_conn().commit()

    def rollback(self) -> None:
        """手动回滚事务"""
        self._get_conn().rollback()

    # ── 事务上下文管理器 ──

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """
        事务上下文管理器
        with conn.transaction():
            conn.execute_update(...)
        正常退出自动commit，异常自动rollback
        """
        conn = self._get_conn()
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── 上下文管理器 ──

    def __enter__(self) -> "DatabaseConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"DatabaseConnection(db='{self.db_path}', connected={self._connected})"


# ─────────────────────────────────────────────
# 全局连接管理
# ─────────────────────────────────────────────

# 全局连接缓存（按路径缓存）
_connection_cache: Dict[str, DatabaseConnection] = {}
_cache_lock = threading.Lock()


def get_connection(
    db_path: Optional[Union[str, Path]] = None,
) -> DatabaseConnection:
    """
    获取全局数据库连接（单例模式，按路径缓存）

    Parameters
    ----------
    db_path : str | Path | None
        数据库路径，None则使用项目默认路径（项目根/database/quant.db）

    Returns
    -------
    DatabaseConnection : 数据库连接实例
    """
    if db_path is None:
        # 自动探测项目根目录
        project_root = _find_project_root()
        db_path = project_root / "database" / DEFAULT_DB_FILENAME
    db_path = str(Path(db_path).resolve())
    with _cache_lock:
        if db_path not in _connection_cache:
            _connection_cache[db_path] = DatabaseConnection(db_path)
        return _connection_cache[db_path]


def close_all_connections() -> None:
    """关闭所有缓存的数据库连接（程序退出时调用；M1: 跨线程全部关闭）"""
    with _cache_lock:
        conns = list(_connection_cache.values())
        _connection_cache.clear()
    for conn in conns:
        conn.close_all()


def _find_project_root() -> Path:
    """从当前文件向上搜索项目根目录（通过pyproject.toml识别）"""
    current = Path(__file__).resolve().parent
    for _ in range(5):
        if (current / "pyproject.toml").exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    return Path.cwd()
