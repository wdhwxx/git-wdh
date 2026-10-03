# 文件名：myquant/indicators/factor_cache.py
"""
factor_cache.py - 三级因子缓存
================================
为指标/因子计算提供三级缓存体系，是回测性能优化 V5 的核心组件。

三级架构：
  L1 内存缓存  : 进程内 LRU + TTL，访问最快（微秒级），受 enable_l1 开关控制
  L2 磁盘缓存  : Parquet 文件持久化到 cache/factor/，跨进程/跨会话共享（毫秒级）
  L3 回源计算  : L1/L2 均未命中时执行实际计算函数，结果回填 L1+L2

核心特性：
  1. 数据指纹参与缓存 Key：数据变更自动导致缓存失效（配合 utils/data_fingerprint.py）
  2. enable_l1 开关：可全局或实例级关闭 L1 内存缓存
  3. 并行场景自动关闭 L1：多进程间内存不共享，L1 命中率低，自动降级为 L2+L3
  4. 缓存统计：各层级命中数、命中率、缓存大小
  5. 线程安全：L1 使用 core/cache.MemoryCache（自带锁），L2 操作加文件锁

依赖: hashlib, json, os, pathlib, logging, threading, time, typing
第三方: pandas, numpy
本地: myquant.core.cache.MemoryCache, myquant.core.constants, myquant.utils.data_fingerprint
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd

from myquant.core.cache import MemoryCache
from myquant.core.constants import CACHE_DEFAULT_TTL, CACHE_MAX_SIZE
from myquant.utils.data_fingerprint import DataFingerprint

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 默认缓存目录（相对于项目根）
DEFAULT_CACHE_DIR: str = "cache/factor"

# L1 默认配置
DEFAULT_L1_MAX_SIZE: int = CACHE_MAX_SIZE  # 1024 条
DEFAULT_L1_TTL: int = CACHE_DEFAULT_TTL    # 3600 秒 (1小时)

# L2 默认配置
DEFAULT_L2_ENABLED: bool = True
DEFAULT_L2_TTL: int = 86400 * 7             # 604800 秒 (7天)
L2_FILE_EXTENSION: str = ".parquet"

# 缓存命中层级标识
CACHE_HIT_L1: str = "l1"
CACHE_HIT_L2: str = "l2"
CACHE_HIT_L3: str = "l3"  # 未命中，执行计算

# 无数据指纹时的占位符
NO_FINGERPRINT_PLACEHOLDER: str = "nofp"

# nofp（无指纹）场景 L2 兜底 TTL（秒）：
# 数据指纹缺失时无法感知数据更新，永久占位会导致脏读；
# 此处封顶为 1 小时，到期自动重算，并打 WARNING 提示调用方传入指纹。
NOFP_FINGERPRINT_TTL: int = 3600

# 文件名中指纹截断长度（避免文件名过长）
FINGERPRINT_TRUNCATE_LEN: int = 16

# ─────────────────────────────────────────────
# 并行模式全局标志
# ─────────────────────────────────────────────
# P2 任务的 parallel_factor_context 上下文管理器会设置此标志。
# 并行模式下，所有 FactorCache 实例自动关闭 L1（多进程内存不共享）。
_parallel_mode: bool = False
_parallel_mode_lock = threading.Lock()


def set_parallel_mode(enabled: bool) -> None:
    """设置全局并行模式。

    并行模式下，新建的 FactorCache 实例自动关闭 L1 内存缓存；
    已存在的实例在后续 get/get_or_compute 调用中也会跳过 L1。

    Args:
        enabled: True 开启并行模式（关闭 L1），False 关闭并行模式（恢复 L1）。
    """
    global _parallel_mode
    with _parallel_mode_lock:
        _parallel_mode = enabled
    if enabled:
        logger.info("并行模式已开启：因子缓存 L1 内存缓存将自动关闭")
    else:
        logger.info("并行模式已关闭：因子缓存 L1 内存缓存已恢复")


def is_parallel_mode() -> bool:
    """查询当前是否处于并行模式。

    Returns:
        True 表示并行模式已开启（L1 被自动关闭）。
    """
    with _parallel_mode_lock:
        return _parallel_mode


# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────

def _hash_params(params: Dict[str, Any]) -> str:
    """将参数字典哈希为短十六进制字符串。

    参数按 key 排序后 JSON 序列化，确保相同参数产生相同哈希。

    Args:
        params: 因子参数字典（如 {"period": 18, "norm_window": 60}）。

    Returns:
        16 字符十六进制哈希字符串。
    """
    if not params:
        return "0" * 16
    sorted_params = dict(sorted(params.items()))
    raw = json.dumps(sorted_params, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _build_cache_key(
    factor_name: str,
    symbol: str,
    params: Dict[str, Any],
    data_fingerprint: Optional[DataFingerprint],
) -> str:
    """构建完整缓存 Key（L1 内存缓存使用）。

    Key 格式: {factor_name}:{symbol}:{params_hash}:{fingerprint_hash}

    Args:
        factor_name: 因子/指标名称（如 "sma", "rsrs"）。
        symbol: 标的代码（如 "600519.SH"）。
        params: 因子参数字典。
        data_fingerprint: 数据指纹；None 时使用占位符。

    Returns:
        完整缓存 Key 字符串。
    """
    fp_hash = (
        data_fingerprint.hash_value
        if data_fingerprint is not None
        else NO_FINGERPRINT_PLACEHOLDER
    )
    params_hash = _hash_params(params)
    return f"{factor_name}:{symbol}:{params_hash}:{fp_hash}"


def _sanitize_path_segment(s: str) -> str:
    """将字符串转为安全的文件路径段。

    保留字母数字与 ``._-``，其余字符替换为下划线；用于 factor_name / symbol
    在 L2 目录分层中的段名。

    Args:
        s: 原始字符串（因子名或标的代码）。

    Returns:
        安全的路径段字符串。
    """
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)


def _build_l2_relpath(
    factor_name: str,
    symbol: str,
    params: Dict[str, Any],
    data_fingerprint: Optional[DataFingerprint],
) -> Path:
    """构建 L2 磁盘缓存的相对路径（目录分层）。

    目录结构: ``{factor_name}/{safe_symbol}/{params_hash}_{fp_short}.parquet``

    因子名作为一级目录天然支持下划线等字符（不再需要从文件名反向切分），
    invalidate 按目录删除即可，从根上消灭 F-13 的解析歧义 bug。

    Args:
        factor_name: 因子名称。
        symbol: 标的代码。
        params: 因子参数。
        data_fingerprint: 数据指纹。

    Returns:
        相对于 cache_dir 的 Path（不含 .parquet 以外的目录名）。
    """
    safe_factor = _sanitize_path_segment(factor_name)
    safe_symbol = _sanitize_path_segment(symbol)
    params_hash = _hash_params(params)
    fp_short = (
        data_fingerprint.hash_value[:FINGERPRINT_TRUNCATE_LEN]
        if data_fingerprint is not None
        else NO_FINGERPRINT_PLACEHOLDER
    )
    return Path(safe_factor) / safe_symbol / f"{params_hash}_{fp_short}{L2_FILE_EXTENSION}"


# ─────────────────────────────────────────────
# FactorCache 三级因子缓存类
# ─────────────────────────────────────────────

class FactorCache:
    """三级因子缓存管理器。

    L1（内存）→ L2（磁盘）→ L3（回源计算）三级降级，计算结果自动回填。

    Usage:
        cache = FactorCache(cache_dir="cache/factor", enable_l1=True)

        # 方式1：手动 get/set
        result = cache.get("sma", "600519.SH", {"period": 20}, fp)
        if result is None:
            result = compute_sma(data, period=20)
            cache.set("sma", "600519.SH", {"period": 20}, fp, result)

        # 方式2：get_or_compute（推荐）
        result, hit_level = cache.get_or_compute(
            factor_name="sma",
            symbol="600519.SH",
            params={"period": 20},
            data=data,
            compute_func=lambda df, p: SMA(**p).calculate(df),
            data_fingerprint=fp,
        )
    """

    def __init__(
        self,
        cache_dir: str = DEFAULT_CACHE_DIR,
        enable_l1: bool = True,
        l1_max_size: int = DEFAULT_L1_MAX_SIZE,
        l1_ttl: int = DEFAULT_L1_TTL,
        l2_enabled: bool = DEFAULT_L2_ENABLED,
        l2_ttl: int = DEFAULT_L2_TTL,
    ) -> None:
        """
        Args:
            cache_dir: L2 磁盘缓存目录路径（相对或绝对）。
            enable_l1: 是否启用 L1 内存缓存。并行模式下此参数被强制覆盖为 False。
            l1_max_size: L1 最大缓存条目数（LRU 淘汰）。
            l1_ttl: L1 缓存过期时间（秒），0 表示永不过期。
            l2_enabled: 是否启用 L2 磁盘缓存。
            l2_ttl: L2 缓存过期时间（秒），0 表示永不过期。
        """
        self._cache_dir = Path(cache_dir)
        self._l2_enabled = l2_enabled
        self._l2_ttl = l2_ttl
        self._l1_ttl = l1_ttl

        # 并行模式检测：强制关闭 L1
        if is_parallel_mode() and enable_l1:
            logger.warning(
                "检测到并行模式已开启，L1 内存缓存被强制关闭 "
                "(多进程间内存不共享，L1 命中率低)"
            )
            enable_l1 = False

        self._enable_l1 = enable_l1

        # L1 内存缓存实例
        self._l1_cache: Optional[MemoryCache] = None
        if self._enable_l1:
            self._l1_cache = MemoryCache(max_size=l1_max_size, default_ttl=l1_ttl)

        # L2 磁盘缓存目录
        if self._l2_enabled:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

        # L2 文件锁（防止多进程/多线程同时写同一文件）
        self._l2_locks: Dict[str, threading.Lock] = {}
        self._l2_locks_lock = threading.Lock()

        # nofp 告警去重：同一 (factor, symbol) 只 WARNING 一次，避免刷屏
        self._nofp_warned: set[Tuple[str, str]] = set()

        # 统计计数器
        self._stats: Dict[str, int] = {
            "l1_hits": 0,
            "l2_hits": 0,
            "l3_misses": 0,   # L1/L2 均未命中，执行计算
            "l1_skips": 0,    # L1 被跳过（并行模式或禁用）
            "l2_writes": 0,
            "l1_writes": 0,
            "invalidate_count": 0,
        }

        logger.info(
            "FactorCache 初始化: cache_dir=%s, l1=%s, l2=%s, l1_ttl=%ds, l2_ttl=%ds",
            self._cache_dir, self._enable_l1, self._l2_enabled, l1_ttl, l2_ttl,
        )

    # ── 属性 ──

    @property
    def enable_l1(self) -> bool:
        """当前 L1 是否实际启用（考虑并行模式覆盖）。"""
        if is_parallel_mode():
            return False
        return self._enable_l1

    @property
    def cache_dir(self) -> Path:
        """L2 磁盘缓存目录。"""
        return self._cache_dir

    # ── L1 内部操作 ──

    def _l1_effectively_enabled(self) -> bool:
        """判断 L1 当前是否实际可用（实例配置 + 并行模式）。"""
        if is_parallel_mode():
            return False
        return self._enable_l1 and self._l1_cache is not None

    def _l1_get(self, key: str) -> Optional[pd.DataFrame]:
        """从 L1 内存缓存读取。"""
        if not self._l1_effectively_enabled():
            self._stats["l1_skips"] += 1
            return None
        result = self._l1_cache.get(key)
        if result is not None:
            self._stats["l1_hits"] += 1
        return result

    def _l1_set(self, key: str, value: pd.DataFrame) -> None:
        """写入 L1 内存缓存。"""
        if not self._l1_effectively_enabled():
            return
        self._l1_cache.set(key, value, ttl=self._l1_ttl)
        self._stats["l1_writes"] += 1

    # ── L2 内部操作 ──

    def _get_l2_lock(self, filename: str) -> threading.Lock:
        """获取指定文件的锁（线程安全创建）。"""
        with self._l2_locks_lock:
            if filename not in self._l2_locks:
                self._l2_locks[filename] = threading.Lock()
            return self._l2_locks[filename]

    def _l2_is_expired(self, filepath: Path, ttl: Optional[int] = None) -> bool:
        """检查 L2 缓存文件是否过期。

        Args:
            filepath: 缓存文件路径。
            ttl: 覆盖 TTL（秒）；None 时使用实例 l2_ttl。nofp 场景由调用方
                传入 min(l2_ttl, NOFP_FINGERPRINT_TTL) 以缩短兜底有效期。

        Returns:
            True 表示已过期（应删除并重新计算）。
        """
        effective_ttl = self._l2_ttl if ttl is None else ttl
        if effective_ttl <= 0:
            return False  # 永不过期
        try:
            mtime = filepath.stat().st_mtime
            return (time.time() - mtime) > effective_ttl
        except OSError:
            return True  # 无法获取修改时间，视为过期

    def _warn_nofp(self, factor_name: str, symbol: str) -> None:
        """无数据指纹时打一次 WARNING（同一 factor/symbol 去重）。

        nofp 场景无法感知数据更新，缓存文件名固定为 *_nofp.parquet；
        已通过 NOFP_FINGERPRINT_TTL 缩短兜底有效期，但仍显式告警提示
        调用方传入 data_fingerprint。

        Args:
            factor_name: 因子名称。
            symbol: 标的代码。
        """
        key = (factor_name, symbol)
        if key not in self._nofp_warned:
            self._nofp_warned.add(key)
            logger.warning(
                "无数据指纹（nofp）访问 L2 缓存: factor=%s, symbol=%s；"
                "无法感知数据更新，已启用短 TTL=%ds 兜底，"
                "请显式传入 data_fingerprint 以获得精确失效",
                factor_name, symbol, NOFP_FINGERPRINT_TTL,
            )

    def _l2_get(
        self,
        factor_name: str,
        symbol: str,
        params: Dict[str, Any],
        data_fingerprint: Optional[DataFingerprint],
    ) -> Optional[pd.DataFrame]:
        """从 L2 磁盘缓存读取（目录分层: factor/symbol/params_fp.parquet）。

        过期文件会被自动删除；nofp（指纹缺失）场景使用短 TTL 兜底。
        """
        if not self._l2_enabled:
            return None

        rel_path = _build_l2_relpath(factor_name, symbol, params, data_fingerprint)
        filepath = self._cache_dir / rel_path

        if not filepath.exists():
            return None

        # nofp 场景短 TTL + 告警
        # 注意：nofp 直接使用 NOFP_FINGERPRINT_TTL，不与 l2_ttl 取 min——
        # l2_ttl=0（永不过期）是用户对「有指纹、数据完整性可信」缓存的信任，
        # nofp 无指纹则必须强制短 TTL 兜底，不能被 0 豁免。
        effective_ttl = self._l2_ttl
        if data_fingerprint is None:
            effective_ttl = NOFP_FINGERPRINT_TTL
            self._warn_nofp(factor_name, symbol)

        if self._l2_is_expired(filepath, effective_ttl):
            try:
                filepath.unlink()
                logger.debug("L2 缓存已过期并删除: %s", filepath.name)
            except OSError as e:
                logger.warning("删除过期 L2 缓存失败: %s, 错误: %s", filepath, e)
            return None

        lock = self._get_l2_lock(str(rel_path))
        with lock:
            try:
                df = pd.read_parquet(filepath)
                self._stats["l2_hits"] += 1
                logger.debug("L2 缓存命中: %s (%d 行)", rel_path, len(df))
                return df
            except Exception as e:
                # 文件损坏等异常，删除并返回 None
                logger.warning("L2 缓存读取失败，将删除: %s, 错误: %s", filepath, e)
                try:
                    filepath.unlink()
                except OSError:
                    pass
                return None

    def _l2_set(
        self,
        factor_name: str,
        symbol: str,
        params: Dict[str, Any],
        data_fingerprint: Optional[DataFingerprint],
        result: pd.DataFrame,
    ) -> None:
        """写入 L2 磁盘缓存（Parquet 格式，目录分层: factor/symbol/）。"""
        if not self._l2_enabled:
            return

        rel_path = _build_l2_relpath(factor_name, symbol, params, data_fingerprint)
        filepath = self._cache_dir / rel_path
        # 确保父目录存在（因子/symbol 分层）
        filepath.parent.mkdir(parents=True, exist_ok=True)
        lock = self._get_l2_lock(str(rel_path))

        # nofp 场景写入时也告警（首次访问该 factor/symbol）
        if data_fingerprint is None:
            self._warn_nofp(factor_name, symbol)

        with lock:
            try:
                # 原子写入：先写临时文件再 rename
                tmp_path = filepath.with_suffix(".tmp")
                result.to_parquet(tmp_path, index=True)
                os.replace(tmp_path, filepath)
                self._stats["l2_writes"] += 1
                logger.debug("L2 缓存写入: %s (%d 行)", rel_path, len(result))
            except Exception as e:
                logger.warning("L2 缓存写入失败: %s, 错误: %s", rel_path, e)
                # 清理可能残留的临时文件
                try:
                    tmp_path = filepath.with_suffix(".tmp")
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass

    # ── 公开接口：get / set ──

    def get(
        self,
        factor_name: str,
        symbol: str,
        params: Optional[Dict[str, Any]] = None,
        data_fingerprint: Optional[DataFingerprint] = None,
    ) -> Optional[pd.DataFrame]:
        """从 L1→L2 逐级获取缓存，未命中返回 None。

        Args:
            factor_name: 因子/指标名称。
            symbol: 标的代码。
            params: 因子参数字典；None 视为空参数。
            data_fingerprint: 数据指纹；None 时使用占位符（无法检测数据变更）。

        Returns:
            缓存的因子结果 DataFrame；未命中返回 None。
        """
        params = params or {}
        key = _build_cache_key(factor_name, symbol, params, data_fingerprint)

        # L1
        result = self._l1_get(key)
        if result is not None:
            return result

        # L2
        result = self._l2_get(factor_name, symbol, params, data_fingerprint)
        if result is not None:
            # L2 命中后回填 L1（加速后续访问）
            self._l1_set(key, result)
            return result

        return None

    def set(
        self,
        factor_name: str,
        symbol: str,
        result: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        data_fingerprint: Optional[DataFingerprint] = None,
    ) -> None:
        """将因子结果写入 L1+L2 缓存。

        Args:
            factor_name: 因子/指标名称。
            symbol: 标的代码。
            result: 因子计算结果 DataFrame。
            params: 因子参数字典；None 视为空参数。
            data_fingerprint: 数据指纹；None 时使用占位符。
        """
        params = params or {}
        key = _build_cache_key(factor_name, symbol, params, data_fingerprint)
        self._l1_set(key, result)
        self._l2_set(factor_name, symbol, params, data_fingerprint, result)

    # ── 公开接口：get_or_compute（推荐使用） ──

    def get_or_compute(
        self,
        factor_name: str,
        symbol: str,
        params: Dict[str, Any],
        data: pd.DataFrame,
        compute_func: Callable[[pd.DataFrame, Dict[str, Any]], pd.DataFrame],
        data_fingerprint: Optional[DataFingerprint] = None,
    ) -> Tuple[pd.DataFrame, str]:
        """获取缓存或执行计算，返回 (结果, 命中层级)。

        逐级查找 L1→L2，均未命中时调用 compute_func 执行 L3 计算，
        结果自动回填到 L1 和 L2。

        Args:
            factor_name: 因子/指标名称。
            symbol: 标的代码。
            params: 因子参数字典（透传给 compute_func）。
            data: 行情数据 DataFrame（compute_func 的输入）。
            compute_func: 计算函数，签名为 (data: DataFrame, params: dict) -> DataFrame。
            data_fingerprint: 数据指纹；None 时使用占位符。

        Returns:
            (result, hit_level) 元组。
            hit_level 为 "l1" / "l2" / "l3"，分别表示 L1 命中 / L2 命中 / 执行计算。

        Raises:
            ValueError: compute_func 不是可调用对象。
            TypeError: data 不是 DataFrame。
        """
        if not callable(compute_func):
            raise ValueError(f"compute_func 必须是可调用对象，got: {type(compute_func).__name__}")
        if not isinstance(data, pd.DataFrame):
            raise TypeError(f"data 必须是 pandas DataFrame，got: {type(data).__name__}")

        key = _build_cache_key(factor_name, symbol, params, data_fingerprint)

        # ── L1 ──
        result = self._l1_get(key)
        if result is not None:
            return result, CACHE_HIT_L1

        # ── L2 ──
        result = self._l2_get(factor_name, symbol, params, data_fingerprint)
        if result is not None:
            self._l1_set(key, result)  # 回填 L1
            return result, CACHE_HIT_L2

        # ── L3: 执行计算 ──
        self._stats["l3_misses"] += 1
        logger.debug(
            "缓存未命中，执行计算: factor=%s, symbol=%s, params=%s",
            factor_name, symbol, params,
        )
        result = compute_func(data, params)

        if result is None:
            logger.warning(
                "compute_func 返回 None，不写入缓存: factor=%s, symbol=%s",
                factor_name, symbol,
            )
            return pd.DataFrame(), CACHE_HIT_L3

        # 回填 L1 + L2
        self._l1_set(key, result)
        self._l2_set(factor_name, symbol, params, data_fingerprint, result)

        return result, CACHE_HIT_L3

    # ── 公开接口：失效 / 清理 ──

    @staticmethod
    def _count_parquet_files(d: Path) -> int:
        """递归统计目录下 .parquet 文件数。"""
        if not d.is_dir():
            return 0
        return sum(1 for _ in d.rglob(f"*{L2_FILE_EXTENSION}"))

    def invalidate(
        self,
        factor_name: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> int:
        """失效匹配条件的缓存（L1+L2）。

        支持按因子名、标的代码或二者组合过滤。均为 None 时清空全部。

        Args:
            factor_name: 因子名称过滤；None 表示不限制。
            symbol: 标的代码过滤；None 表示不限制。

        Returns:
            失效的缓存条目总数（L1 删除数 + L2 删除文件数）。
        """
        removed = 0

        # L1 失效（key 用 ":" 分隔，factor_name 不含 ":"，解析无歧义）
        if self._l1_cache is not None:
            keys_to_delete = []
            for key in self._l1_cache.keys():
                parts = key.split(":", 3)
                if len(parts) < 4:
                    continue
                key_factor, key_symbol = parts[0], parts[1]
                if factor_name is not None and key_factor != factor_name:
                    continue
                if symbol is not None and key_symbol != symbol:
                    continue
                keys_to_delete.append(key)
            for key in keys_to_delete:
                if self._l1_cache.delete(key):
                    removed += 1

        # L2 失效：目录分层存储（{factor_name}/{symbol}/），按目录删除，
        # 不再从文件名反向切分（F-13：消灭下划线因子名解析歧义）
        if self._l2_enabled and self._cache_dir.exists():
            if factor_name is not None:
                safe_factor = _sanitize_path_segment(factor_name)
                factor_dir = self._cache_dir / safe_factor
                if symbol is not None:
                    # 仅删指定 symbol 子目录
                    safe_symbol = _sanitize_path_segment(symbol)
                    target = factor_dir / safe_symbol
                    if target.is_dir():
                        removed += self._count_parquet_files(target)
                        shutil.rmtree(target, ignore_errors=True)
                else:
                    # 删整个因子目录
                    if factor_dir.is_dir():
                        removed += self._count_parquet_files(factor_dir)
                        shutil.rmtree(factor_dir, ignore_errors=True)
            elif symbol is not None:
                # 遍历所有因子目录，删同名 symbol 子目录
                safe_symbol = _sanitize_path_segment(symbol)
                for factor_dir in self._cache_dir.iterdir():
                    if factor_dir.is_dir():
                        target = factor_dir / safe_symbol
                        if target.is_dir():
                            removed += self._count_parquet_files(target)
                            shutil.rmtree(target, ignore_errors=True)
            else:
                # 清空全部：删所有因子子目录 + 兼容旧版扁平散文件（根目录 .parquet）
                for child in self._cache_dir.iterdir():
                    if child.is_dir():
                        removed += self._count_parquet_files(child)
                        shutil.rmtree(child, ignore_errors=True)
                    elif child.suffix == L2_FILE_EXTENSION:
                        # 旧版扁平布局残留文件（F-13 升级前），一并清理
                        try:
                            child.unlink()
                            removed += 1
                        except OSError as e:
                            logger.warning("删除旧扁平 L2 文件失败: %s, 错误: %s", child, e)

        self._stats["invalidate_count"] += removed
        logger.info(
            "缓存失效完成: factor=%s, symbol=%s, 共删除 %d 条",
            factor_name or "ALL", symbol or "ALL", removed,
        )
        return removed

    def clear(self) -> None:
        """清空所有缓存（L1+L2）。"""
        if self._l1_cache is not None:
            self._l1_cache.clear()
        if self._l2_enabled and self._cache_dir.exists():
            for child in self._cache_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    # 兼容旧版扁平散文件
                    try:
                        child.unlink()
                    except OSError as e:
                        logger.warning("清空 L2 缓存时删除失败: %s, 错误: %s", child, e)
        logger.info("FactorCache 已清空全部缓存")

    def cleanup_expired(self) -> int:
        """清理 L2 中已过期的缓存文件（递归遍历因子/symbol 分层目录）。

        nofp 文件名含 "nofp" 段，使用短 TTL（NOFP_FINGERPRINT_TTL）判断过期。

        Returns:
            清理的过期文件数量。
        """
        if not self._l2_enabled or not self._cache_dir.exists():
            return 0
        cleaned = 0
        for filepath in self._cache_dir.rglob(f"*{L2_FILE_EXTENSION}"):
            # nofp 文件用短 TTL 兜底（不随 l2_ttl=0 永不过期）
            effective_ttl = self._l2_ttl
            if NO_FINGERPRINT_PLACEHOLDER in filepath.stem:
                effective_ttl = NOFP_FINGERPRINT_TTL
            if self._l2_is_expired(filepath, effective_ttl):
                try:
                    filepath.unlink()
                    cleaned += 1
                except OSError as e:
                    logger.warning("清理过期缓存失败: %s, 错误: %s", filepath, e)
        if cleaned > 0:
            logger.info("清理过期 L2 缓存: %d 个文件", cleaned)
        return cleaned

    # ── 公开接口：统计 ──

    def stats(self) -> Dict[str, Any]:
        """获取缓存统计信息。

        Returns:
            包含各层级命中数、命中率、缓存大小的字典。
        """
        l1_size = len(self._l1_cache) if self._l1_cache is not None else 0
        l2_files = (
            len(list(self._cache_dir.rglob(f"*{L2_FILE_EXTENSION}")))
            if self._l2_enabled and self._cache_dir.exists()
            else 0
        )
        total_requests = (
            self._stats["l1_hits"]
            + self._stats["l2_hits"]
            + self._stats["l3_misses"]
        )
        hit_rate = (
            (self._stats["l1_hits"] + self._stats["l2_hits"]) / total_requests
            if total_requests > 0
            else 0.0
        )
        return {
            "l1_enabled": self.enable_l1,
            "l2_enabled": self._l2_enabled,
            "l1_size": l1_size,
            "l2_files": l2_files,
            "l1_hits": self._stats["l1_hits"],
            "l2_hits": self._stats["l2_hits"],
            "l3_misses": self._stats["l3_misses"],
            "l1_skips": self._stats["l1_skips"],
            "l1_writes": self._stats["l1_writes"],
            "l2_writes": self._stats["l2_writes"],
            "total_requests": total_requests,
            "hit_rate": round(hit_rate, 4),
            "invalidate_count": self._stats["invalidate_count"],
            "cache_dir": str(self._cache_dir),
        }

    def __repr__(self) -> str:
        s = self.stats()
        return (
            f"FactorCache(l1={s['l1_enabled']}, l2={s['l2_enabled']}, "
            f"l1_size={s['l1_size']}, l2_files={s['l2_files']}, "
            f"hit_rate={s['hit_rate']:.2%})"
        )


# ─────────────────────────────────────────────
# 全局单例管理
# ─────────────────────────────────────────────

_default_factor_cache: Optional[FactorCache] = None
_default_cache_lock = threading.Lock()


def get_factor_cache(**kwargs: Any) -> FactorCache:
    """获取全局因子缓存单例。

    首次调用时创建实例，后续调用返回同一实例。
    如需不同配置的独立实例，请直接使用 FactorCache(...) 构造。

    Args:
        **kwargs: 透传给 FactorCache 构造函数的参数（仅首次调用生效）。

    Returns:
        全局 FactorCache 单例。
    """
    global _default_factor_cache
    with _default_cache_lock:
        if _default_factor_cache is None:
            _default_factor_cache = FactorCache(**kwargs)
        return _default_factor_cache


def reset_factor_cache() -> None:
    """重置全局因子缓存单例（下次调用 get_factor_cache 时重新创建）。

    主要用于测试或需要切换配置的场景。
    """
    global _default_factor_cache
    with _default_cache_lock:
        if _default_factor_cache is not None:
            _default_factor_cache.clear()
        _default_factor_cache = None
    logger.info("全局因子缓存单例已重置")


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import shutil
    import tempfile

    import numpy as np

    print("=" * 60)
    print("  factor_cache.py — 三级因子缓存自测 Demo")
    print("=" * 60)

    # 构造测试数据
    print("\n[1] 构造测试数据")
    dates = pd.date_range("2024-01-02", periods=30, freq="B")
    np.random.seed(42)
    test_data = pd.DataFrame(
        {
            "open": np.cumsum(np.random.randn(30)) + 100,
            "high": np.cumsum(np.random.randn(30)) + 102,
            "low": np.cumsum(np.random.randn(30)) + 98,
            "close": np.cumsum(np.random.randn(30)) + 100,
            "volume": np.random.randint(10000, 50000, 30),
        },
        index=dates,
    )
    test_data.index.name = "date"
    print(f"    数据形状: {test_data.shape}")

    # 构造数据指纹
    from myquant.utils.data_fingerprint import compute_fingerprint
    fp = compute_fingerprint(test_data, symbol="TEST.SH", data_source="demo")
    print(f"    数据指纹: {fp.hash_value[:16]}...")

    # 使用临时目录作为缓存目录
    tmp_cache_dir = Path(tempfile.mkdtemp(prefix="factor_cache_test_"))
    print(f"    临时缓存目录: {tmp_cache_dir}")

    try:
        # ── 测试 2: 基本 get/set ──
        print("\n[2] 基本 get/set 测试")
        cache = FactorCache(cache_dir=str(tmp_cache_dir), enable_l1=True, l2_ttl=0)

        test_result = pd.DataFrame({"sma_20": test_data["close"].rolling(20).mean()})
        cache.set("sma", "TEST.SH", test_result, params={"period": 20}, data_fingerprint=fp)

        cached = cache.get("sma", "TEST.SH", params={"period": 20}, data_fingerprint=fp)
        assert cached is not None, "L1 缓存应命中"
        pd.testing.assert_frame_equal(cached, test_result)
        print(f"    ✓ set/get 正常，L1 命中")

        # ── 测试 3: get_or_compute L3 未命中 ──
        print("\n[3] get_or_compute — L3 未命中（执行计算）")
        compute_count = {"n": 0}

        def fake_sma_compute(data: pd.DataFrame, params: dict) -> pd.DataFrame:
            compute_count["n"] += 1
            period = params.get("period", 20)
            return pd.DataFrame({f"sma_{period}": data["close"].rolling(period).mean()})

        result3, hit3 = cache.get_or_compute(
            factor_name="sma",
            symbol="TEST.SH",
            params={"period": 10},
            data=test_data,
            compute_func=fake_sma_compute,
            data_fingerprint=fp,
        )
        assert hit3 == "l3", f"应 L3 未命中，got: {hit3}"
        assert compute_count["n"] == 1, "计算函数应被调用 1 次"
        print(f"    ✓ L3 未命中，执行计算，结果已回填")

        # ── 测试 4: get_or_compute L1 命中 ──
        print("\n[4] get_or_compute — L1 命中")
        result4, hit4 = cache.get_or_compute(
            factor_name="sma",
            symbol="TEST.SH",
            params={"period": 10},
            data=test_data,
            compute_func=fake_sma_compute,
            data_fingerprint=fp,
        )
        assert hit4 == "l1", f"应 L1 命中，got: {hit4}"
        assert compute_count["n"] == 1, "计算函数不应再次被调用"
        pd.testing.assert_frame_equal(result4, result3)
        print(f"    ✓ L1 命中，未调用计算函数")

        # ── 测试 5: L2 磁盘缓存（新建禁用 L1 的实例） ──
        print("\n[5] L2 磁盘缓存命中测试")
        cache_no_l1 = FactorCache(cache_dir=str(tmp_cache_dir), enable_l1=False, l2_ttl=0)
        result5, hit5 = cache_no_l1.get_or_compute(
            factor_name="sma",
            symbol="TEST.SH",
            params={"period": 10},
            data=test_data,
            compute_func=fake_sma_compute,
            data_fingerprint=fp,
        )
        assert hit5 == "l2", f"应 L2 命中（L1 禁用，L2 已有文件），got: {hit5}"
        assert compute_count["n"] == 1, "计算函数不应被调用"
        # Parquet 序列化不保留 DatetimeIndex.freq 属性（跨语言格式限制），
        # 数据内容完全一致，比较时跳过 freq 检查
        pd.testing.assert_frame_equal(result5, result3, check_freq=False)
        print(f"    ✓ L2 磁盘缓存命中，未调用计算函数")

        # ── 测试 6: 数据指纹变更导致缓存失效 ──
        print("\n[6] 数据指纹变更 — 缓存自动失效")
        modified_data = test_data.copy()
        modified_data.iloc[-1, modified_data.columns.get_loc("close")] = 999.0
        fp_modified = compute_fingerprint(modified_data, symbol="TEST.SH", data_source="demo")

        result6, hit6 = cache.get_or_compute(
            factor_name="sma",
            symbol="TEST.SH",
            params={"period": 10},
            data=modified_data,
            compute_func=fake_sma_compute,
            data_fingerprint=fp_modified,
        )
        assert hit6 == "l3", f"指纹变更应导致 L3 未命中，got: {hit6}"
        assert compute_count["n"] == 2, "计算函数应被再次调用"
        print(f"    ✓ 数据指纹变更，缓存未命中，重新计算")

        # ── 测试 7: 参数变更导致缓存未命中 ──
        print("\n[7] 参数变更 — 缓存未命中")
        result7, hit7 = cache.get_or_compute(
            factor_name="sma",
            symbol="TEST.SH",
            params={"period": 30},
            data=test_data,
            compute_func=fake_sma_compute,
            data_fingerprint=fp,
        )
        assert hit7 == "l3", f"参数变更应 L3 未命中，got: {hit7}"
        print(f"    ✓ 参数变更，缓存未命中")

        # ── 测试 8: 并行模式自动关闭 L1 ──
        print("\n[8] 并行模式 — L1 自动关闭")
        set_parallel_mode(True)
        assert is_parallel_mode() is True

        cache_parallel = FactorCache(cache_dir=str(tmp_cache_dir), enable_l1=True, l2_ttl=0)
        assert cache_parallel.enable_l1 is False, "并行模式下 L1 应被强制关闭"

        # 并行模式下应走 L2
        result8, hit8 = cache_parallel.get_or_compute(
            factor_name="sma",
            symbol="TEST.SH",
            params={"period": 10},
            data=test_data,
            compute_func=fake_sma_compute,
            data_fingerprint=fp,
        )
        assert hit8 == "l2", f"并行模式下应 L2 命中，got: {hit8}"
        print(f"    ✓ 并行模式 L1 自动关闭，走 L2 磁盘缓存")

        set_parallel_mode(False)
        assert is_parallel_mode() is False
        print(f"    ✓ 并行模式已关闭，L1 恢复")

        # ── 测试 9: invalidate 失效 ──
        print("\n[9] invalidate 缓存失效")
        before_stats = cache.stats()
        removed = cache.invalidate(factor_name="sma", symbol="TEST.SH")
        assert removed > 0, f"应至少失效 1 条，got: {removed}"
        after = cache.get("sma", "TEST.SH", params={"period": 10}, data_fingerprint=fp)
        assert after is None, "失效后应无法获取缓存"
        print(f"    ✓ invalidate 成功，删除 {removed} 条缓存")

        # ── 测试 10: 统计信息 ──
        print("\n[10] 缓存统计信息")
        stats = cache.stats()
        print(f"    L1 命中: {stats['l1_hits']}")
        print(f"    L2 命中: {stats['l2_hits']}")
        print(f"    L3 未命中: {stats['l3_misses']}")
        print(f"    总请求: {stats['total_requests']}")
        print(f"    命中率: {stats['hit_rate']:.2%}")
        assert stats["total_requests"] > 0
        print(f"    ✓ 统计信息正常")

        # ── 测试 11: 全局单例 ──
        print("\n[11] 全局单例测试")
        reset_factor_cache()
        singleton1 = get_factor_cache(cache_dir=str(tmp_cache_dir / "global"))
        singleton2 = get_factor_cache()
        assert singleton1 is singleton2, "get_factor_cache 应返回同一实例"
        print(f"    ✓ 全局单例正常")
        reset_factor_cache()

    finally:
        # 清理临时目录
        shutil.rmtree(tmp_cache_dir, ignore_errors=True)
        print(f"\n[12] 临时缓存目录已清理: {tmp_cache_dir}")

    print("\n" + "=" * 60)
    print("  ✓ factor_cache.py 全部自测通过")
    print("=" * 60)
