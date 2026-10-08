# 文件名：myquant/utils/cache_sync.py
"""
鲲鹏量化交易系统 - 缓存同步管理器（CacheSyncManager）
======================================================
V5回测性能优化 P0 第8项：数据更新后自动更新指纹 + 主动缓存失效

核心功能:
    1. 数据下载/清洗完成后，自动计算数据指纹并与历史指纹对比
    2. 识别变更标的，主动失效其因子缓存（L1内存 + L2磁盘）
    3. 更新指纹仓库（原子写入，线程安全）
    4. 提供缓存状态查询、清理、过期回收等运维接口

设计原则:
    - 数据变更必须导致缓存失效（防止使用过期因子计算结果）
    - 未变更标的不失效缓存（保留热缓存，避免不必要的重算）
    - 指纹仓库与缓存目录路径可配置，支持多环境
    - 所有操作有详细日志和统计，便于运维排查

依赖:
    - myquant.utils.data_fingerprint (FingerprintStore, compute_fingerprint_from_file)
    - 因子缓存实例由装配层经 factor_cache_factory 注入（G1 EX-07B，本模块不直接依赖
      myquant.indicators.factor_cache；未装配时缓存操作降级为空操作）
    - pandas, pathlib, logging, typing
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from myquant.utils.data_fingerprint import (
    DataFingerprint,
    FingerprintStore,
    compute_fingerprint_from_file,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

DEFAULT_FINGERPRINT_STORE_NAME: str = "fingerprint_store.json"
"""默认指纹仓库文件名"""

SUPPORTED_DATA_EXTENSIONS: tuple = (".parquet", ".csv", ".pq")
"""支持的数据文件格式"""

# ============================================================================
# 同步结果数据类
# ============================================================================

@dataclass
class SyncResult:
    """
    缓存同步结果

    Attributes:
        total_scanned: 扫描的标的总数。
        fingerprints_updated: 指纹更新数量（含新增和变更）。
        changed_symbols: 发生数据变更的标的列表。
        cache_invalidated: 缓存失效的标的数量。
        cache_entries_removed: 实际删除的缓存条目数。
        unchanged_symbols: 未变更的标的数量。
        errors: 错误信息列表。
        duration_ms: 同步耗时（毫秒）。
    """

    total_scanned: int = 0
    fingerprints_updated: int = 0
    changed_symbols: List[str] = field(default_factory=list)
    cache_invalidated: int = 0
    cache_entries_removed: int = 0
    unchanged_symbols: int = 0
    errors: List[str] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def change_rate(self) -> float:
        """变更率（0-1）"""
        if self.total_scanned == 0:
            return 0.0
        return len(self.changed_symbols) / self.total_scanned

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "total_scanned": self.total_scanned,
            "fingerprints_updated": self.fingerprints_updated,
            "changed_symbols": self.changed_symbols.copy(),
            "cache_invalidated": self.cache_invalidated,
            "cache_entries_removed": self.cache_entries_removed,
            "unchanged_symbols": self.unchanged_symbols,
            "change_rate": round(self.change_rate, 4),
            "errors": self.errors.copy(),
            "duration_ms": round(self.duration_ms, 2),
        }

    def summary(self) -> str:
        """生成可读的同步摘要"""
        lines = [
            "=== 缓存同步结果 ===",
            f"  扫描标的: {self.total_scanned}",
            f"  变更标的: {len(self.changed_symbols)} (变更率 {self.change_rate:.1%})",
            f"  未变更: {self.unchanged_symbols}",
            f"  指纹更新: {self.fingerprints_updated}",
            f"  缓存失效: {self.cache_invalidated} 个标的, {self.cache_entries_removed} 条",
            f"  耗时: {self.duration_ms:.1f}ms",
        ]
        if self.changed_symbols:
            lines.append(f"  变更列表: {', '.join(self.changed_symbols[:10])}")
            if len(self.changed_symbols) > 10:
                lines.append(f"    ... 等共 {len(self.changed_symbols)} 个")
        if self.errors:
            lines.append(f"  错误: {len(self.errors)} 个")
            for err in self.errors[:5]:
                lines.append(f"    - {err}")
        return "\n".join(lines)


# ============================================================================
# 缓存同步管理器
# ============================================================================

class CacheSyncManager:
    """
    缓存同步管理器：数据更新后自动更新指纹 + 主动失效缓存

    工作流程:
        1. 扫描 processed 目录，找到所有标的数据文件
        2. 对每个标的计算数据指纹
        3. 与指纹仓库中的历史指纹对比
        4. 变更标的：失效其因子缓存（L1+L2），更新指纹
        5. 未变更标的：保留缓存，不做任何操作
        6. 持久化指纹仓库

    Attributes:
        processed_dir: 清洗后数据目录。
        fingerprint_store: 指纹仓库实例。
        cache_dir: 因子缓存目录。
        factor_cache: 因子缓存实例（延迟初始化）。
    """

    def __init__(
        self,
        processed_dir: Optional[Union[str, Path]] = None,
        fingerprint_store_path: Optional[Union[str, Path]] = None,
        cache_dir: Optional[Union[str, Path]] = None,
        project_root: Optional[Union[str, Path]] = None,
        factor_cache_factory: Optional[Callable[[], Any]] = None,
    ):
        """
        初始化缓存同步管理器

        Args:
            processed_dir: 清洗后数据目录，None 则使用默认 datasets/processed/。
            fingerprint_store_path: 指纹仓库文件路径，None 则使用 cache/fingerprint/。
            cache_dir: 因子缓存目录，None 则使用 cache/factor/。
            project_root: 项目根目录，None 则自动探测。
            factor_cache_factory: 因子缓存工厂（返回 FactorCache 实例）；
                由装配层注入（G1 EX-07B，本模块不再直接依赖 indicators.factor_cache）。
                None 时缓存失效/清理/统计操作降级为空操作并记 warning。
        """
        # 项目根目录探测
        if project_root is None:
            project_root = self._detect_project_root()
        self._project_root = Path(project_root)

        # 数据目录
        if processed_dir is None:
            self.processed_dir: Path = self._project_root / "datasets" / "processed"
        else:
            self.processed_dir = Path(processed_dir)

        # 指纹仓库路径
        if fingerprint_store_path is None:
            fingerprint_store_path = (
                self._project_root / "cache" / "fingerprint" / DEFAULT_FINGERPRINT_STORE_NAME
            )
        self._fingerprint_store_path = Path(fingerprint_store_path)
        self._fingerprint_store_path.parent.mkdir(parents=True, exist_ok=True)

        # 缓存目录
        if cache_dir is None:
            cache_dir = self._project_root / "cache" / "factor"
        self.cache_dir: Path = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 指纹仓库
        self.fingerprint_store: FingerprintStore = FingerprintStore(self._fingerprint_store_path)

        # 因子缓存（延迟初始化；工厂由装配层注入，避免 L0 反向依赖 L2）
        self._factor_cache: Optional[Any] = None
        self._factor_cache_factory: Optional[Callable[[], Any]] = factor_cache_factory

        logger.info(
            "CacheSyncManager 初始化: processed=%s, fingerprint_store=%s, cache_dir=%s",
            self.processed_dir, self._fingerprint_store_path, self.cache_dir,
        )

    # ------------------------------------------------------------------
    # 核心同步方法
    # ------------------------------------------------------------------

    def sync_all(self, data_source: str = "download") -> SyncResult:
        """
        全量同步：扫描所有标的，更新指纹并失效变更标的缓存

        Args:
            data_source: 数据来源标识（写入指纹元数据），如 "download" / "clean"。

        Returns:
            SyncResult 同步结果。
        """
        start_time = time.perf_counter()
        result = SyncResult()

        # 1. 扫描所有标的数据文件
        symbol_files = self._scan_data_files()
        result.total_scanned = len(symbol_files)

        if result.total_scanned == 0:
            logger.warning("processed 目录无数据文件: %s", self.processed_dir)
            result.duration_ms = (time.perf_counter() - start_time) * 1000
            return result

        logger.info("开始全量同步: 扫描到 %d 个标的", result.total_scanned)

        # 2. 逐个计算指纹并对比
        new_fingerprints: Dict[str, DataFingerprint] = {}
        for symbol, filepath in symbol_files.items():
            try:
                fp = compute_fingerprint_from_file(
                    filepath=filepath,
                    symbol=symbol,
                    data_source=data_source,
                )
                new_fingerprints[symbol] = fp

                # 检查是否变更
                if self.fingerprint_store.has_changed(symbol, fp):
                    result.changed_symbols.append(symbol)
                else:
                    result.unchanged_symbols += 1

            except Exception as e:
                error_msg = f"{symbol}: 指纹计算失败 - {type(e).__name__}: {e}"
                result.errors.append(error_msg)
                logger.warning("指纹计算失败 %s: %s", symbol, e)

        # 3. 失效变更标的的缓存
        if result.changed_symbols:
            removed = self._invalidate_cache_for_symbols(result.changed_symbols)
            result.cache_entries_removed = removed
            result.cache_invalidated = len(result.changed_symbols)
            logger.info(
                "数据变更标的 %d 个，已失效缓存 %d 条",
                len(result.changed_symbols), removed,
            )

        # 4. 更新指纹仓库（全量覆盖 + 原子写入）
        if new_fingerprints:
            self.fingerprint_store.sync(new_fingerprints)
            result.fingerprints_updated = len(new_fingerprints)

        result.duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info("全量同步完成: %s", result.summary())
        return result

    def sync_symbol(self, symbol: str, data_source: str = "clean") -> SyncResult:
        """
        同步单个标的：计算指纹，如变更则失效缓存并更新指纹

        Args:
            symbol: 标的代码。
            data_source: 数据来源标识。

        Returns:
            SyncResult 同步结果。
        """
        start_time = time.perf_counter()
        result = SyncResult()
        result.total_scanned = 1

        # 查找标的数据文件
        filepath = self._find_symbol_file(symbol)
        if filepath is None:
            result.errors.append(f"{symbol}: 未找到数据文件")
            result.duration_ms = (time.perf_counter() - start_time) * 1000
            logger.warning("未找到 %s 的数据文件", symbol)
            return result

        try:
            # 计算新指纹
            new_fp = compute_fingerprint_from_file(
                filepath=filepath,
                symbol=symbol,
                data_source=data_source,
            )

            # 检查是否变更
            if self.fingerprint_store.has_changed(symbol, new_fp):
                result.changed_symbols.append(symbol)
                # 失效缓存
                removed = self._invalidate_cache_for_symbols([symbol])
                result.cache_entries_removed = removed
                result.cache_invalidated = 1
                logger.info("%s 数据变更，已失效 %d 条缓存", symbol, removed)
            else:
                result.unchanged_symbols = 1
                logger.debug("%s 数据未变更，保留缓存", symbol)

            # 更新指纹仓库
            self.fingerprint_store.put(new_fp)
            self.fingerprint_store.save()
            result.fingerprints_updated = 1

        except Exception as e:
            error_msg = f"{symbol}: 同步失败 - {type(e).__name__}: {e}"
            result.errors.append(error_msg)
            logger.error("同步 %s 失败: %s", symbol, e)

        result.duration_ms = (time.perf_counter() - start_time) * 1000
        return result

    def sync_symbols(
        self,
        symbols: List[str],
        data_source: str = "download",
    ) -> SyncResult:
        """
        批量同步指定标的列表

        Args:
            symbols: 标的代码列表。
            data_source: 数据来源标识。

        Returns:
            SyncResult 同步结果。
        """
        start_time = time.perf_counter()
        result = SyncResult()
        result.total_scanned = len(symbols)

        if not symbols:
            result.duration_ms = (time.perf_counter() - start_time) * 1000
            return result

        new_fingerprints: Dict[str, DataFingerprint] = {}
        for symbol in symbols:
            filepath = self._find_symbol_file(symbol)
            if filepath is None:
                result.errors.append(f"{symbol}: 未找到数据文件")
                continue

            try:
                fp = compute_fingerprint_from_file(
                    filepath=filepath,
                    symbol=symbol,
                    data_source=data_source,
                )
                new_fingerprints[symbol] = fp

                if self.fingerprint_store.has_changed(symbol, fp):
                    result.changed_symbols.append(symbol)
                else:
                    result.unchanged_symbols += 1

            except Exception as e:
                result.errors.append(f"{symbol}: {type(e).__name__}: {e}")
                logger.warning("指纹计算失败 %s: %s", symbol, e)

        # 失效变更标的缓存
        if result.changed_symbols:
            removed = self._invalidate_cache_for_symbols(result.changed_symbols)
            result.cache_entries_removed = removed
            result.cache_invalidated = len(result.changed_symbols)

        # 更新指纹仓库
        if new_fingerprints:
            for fp in new_fingerprints.values():
                self.fingerprint_store.put(fp)
            self.fingerprint_store.save()
            result.fingerprints_updated = len(new_fingerprints)

        result.duration_ms = (time.perf_counter() - start_time) * 1000
        return result

    # ------------------------------------------------------------------
    # 缓存管理方法
    # ------------------------------------------------------------------

    def invalidate_symbol(self, symbol: str) -> int:
        """
        失效指定标的的所有因子缓存

        Args:
            symbol: 标的代码。

        Returns:
            失效的缓存条目数。
        """
        cache = self._get_factor_cache()
        if cache is None:
            return 0
        removed = cache.invalidate(symbol=symbol)
        logger.info("已失效 %s 的缓存: %d 条", symbol, removed)
        return removed

    def invalidate_factor(self, factor_name: str) -> int:
        """
        失效指定因子的所有标的缓存

        Args:
            factor_name: 因子名称。

        Returns:
            失效的缓存条目数。
        """
        cache = self._get_factor_cache()
        if cache is None:
            return 0
        removed = cache.invalidate(factor_name=factor_name)
        logger.info("已失效因子 %s 的缓存: %d 条", factor_name, removed)
        return removed

    def clear_all_cache(self) -> None:
        """清空所有因子缓存（L1+L2）"""
        cache = self._get_factor_cache()
        if cache is None:
            return
        cache.clear()
        logger.info("已清空所有因子缓存")

    def cleanup_expired_cache(self) -> int:
        """
        清理 L2 磁盘中已过期的缓存文件

        Returns:
            清理的文件数量。
        """
        cache = self._get_factor_cache()
        if cache is None:
            return 0
        cleaned = cache.cleanup_expired()
        logger.info("已清理过期缓存: %d 个文件", cleaned)
        return cleaned

    def get_cache_status(self) -> Dict[str, Any]:
        """
        获取缓存状态信息

        Returns:
            包含缓存统计、指纹仓库信息的字典。
        """
        cache = self._get_factor_cache()
        cache_stats: Dict[str, Any] = {} if cache is None else cache.stats()

        # L2 缓存文件大小
        l2_size_bytes = 0
        l2_file_count = 0
        if self.cache_dir.exists():
            for f in self.cache_dir.glob("*.parquet"):
                l2_size_bytes += f.stat().st_size
                l2_file_count += 1

        return {
            "cache": cache_stats,
            "l2_size_mb": round(l2_size_bytes / (1024 * 1024), 2),
            "l2_file_count": l2_file_count,
            "fingerprint_store": {
                "path": str(self._fingerprint_store_path),
                "symbol_count": len(self.fingerprint_store),
                "symbols": self.fingerprint_store.all_symbols(),
            },
            "processed_dir": {
                "path": str(self.processed_dir),
                "exists": self.processed_dir.exists(),
            },
        }

    def get_fingerprint_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取指定标的的指纹信息

        Args:
            symbol: 标的代码。

        Returns:
            指纹信息字典，不存在返回 None。
        """
        fp = self.fingerprint_store.get(symbol)
        if fp is None:
            return None
        return {
            "symbol": fp.symbol,
            "hash": fp.hash_value,
            "row_count": fp.row_count,
            "date_range": f"{fp.start_date} ~ {fp.end_date}",
            "columns": list(fp.columns),
            "data_source": fp.data_source,
            "created_at": fp.created_at,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get_factor_cache(self) -> Optional[Any]:
        """获取或创建因子缓存实例（延迟初始化；工厂由装配层注入）。

        Returns:
            FactorCache 实例；未装配工厂时返回 None（调用方降级为空操作）。
        """
        if self._factor_cache is None and self._factor_cache_factory is not None:
            self._factor_cache = self._factor_cache_factory()
        if self._factor_cache is None:
            logger.warning(
                "未装配因子缓存工厂（factor_cache_factory 未注入），"
                "缓存失效/清理/统计操作降级为空操作"
            )
        return self._factor_cache

    def _scan_data_files(self) -> Dict[str, Path]:
        """
        扫描 processed 目录，返回 {symbol: filepath} 映射

        支持两种格式:
            1. 扁平文件: {symbol}.parquet / {symbol}.csv
            2. 子目录: {symbol_safe}/bar_*.{format}（取最新文件）
        """
        result: Dict[str, Path] = {}

        if not self.processed_dir.exists():
            logger.warning("processed 目录不存在: %s", self.processed_dir)
            return result

        # 格式1: 扁平 parquet/csv 文件
        for data_file in sorted(self.processed_dir.iterdir()):
            if data_file.is_dir() or data_file.name.startswith("."):
                continue
            if data_file.suffix.lower() not in SUPPORTED_DATA_EXTENSIONS:
                continue
            symbol = data_file.stem
            if symbol not in result:
                result[symbol] = data_file

        # 格式2: 子目录（symbol_safe 目录下的最新数据文件）
        for symbol_dir in sorted(self.processed_dir.iterdir()):
            if not symbol_dir.is_dir() or symbol_dir.name.startswith("."):
                continue
            symbol = symbol_dir.name.replace("_", ".")
            if symbol in result:
                continue  # 扁平文件优先

            # 找最新的数据文件
            latest_file = None
            latest_mtime = 0
            for data_file in symbol_dir.glob("*"):
                if data_file.suffix.lower() not in SUPPORTED_DATA_EXTENSIONS:
                    continue
                mtime = data_file.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = data_file

            if latest_file is not None:
                result[symbol] = latest_file

        return result

    def _find_symbol_file(self, symbol: str) -> Optional[Path]:
        """
        查找指定标的的数据文件

        优先查找扁平文件，再查找子目录。
        """
        # 格式1: 扁平文件
        for ext in SUPPORTED_DATA_EXTENSIONS:
            filepath = self.processed_dir / f"{symbol}{ext}"
            if filepath.exists():
                return filepath

        # 格式2: 子目录
        symbol_safe = symbol.replace(".", "_")
        symbol_dir = self.processed_dir / symbol_safe
        if symbol_dir.exists():
            latest_file = None
            latest_mtime = 0
            for data_file in symbol_dir.glob("*"):
                if data_file.suffix.lower() not in SUPPORTED_DATA_EXTENSIONS:
                    continue
                mtime = data_file.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = data_file
            return latest_file

        return None

    def _invalidate_cache_for_symbols(self, symbols: List[str]) -> int:
        """
        失效指定标的列表的所有因子缓存

        Returns:
            失效的缓存条目总数。
        """
        if not symbols:
            return 0

        cache = self._get_factor_cache()
        if cache is None:
            return 0
        total_removed = 0
        for symbol in symbols:
            removed = cache.invalidate(symbol=symbol)
            total_removed += removed
        return total_removed

    @staticmethod
    def _detect_project_root() -> Path:
        """自动探测项目根目录（向上查找 pyproject.toml）"""
        current = Path(__file__).resolve()
        for _ in range(10):
            if (current / "pyproject.toml").exists():
                return current
            if current == current.parent:
                break
            current = current.parent
        # 回退到当前工作目录
        return Path.cwd()

    def __repr__(self) -> str:
        return (
            f"CacheSyncManager(processed={self.processed_dir}, "
            f"fingerprints={len(self.fingerprint_store)}, "
            f"cache_dir={self.cache_dir})"
        )


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import shutil
    import tempfile

    print("=" * 60)
    print("  cache_sync.py — 缓存同步管理器 自测 Demo")
    print("=" * 60)

    # 构造测试环境
    print("\n[1] 构造测试环境")
    tmp_root = Path(tempfile.mkdtemp(prefix="cache_sync_test_"))
    processed_dir = tmp_root / "datasets" / "processed"
    fingerprint_dir = tmp_root / "cache" / "fingerprint"
    cache_dir = tmp_root / "cache" / "factor"
    processed_dir.mkdir(parents=True)
    fingerprint_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)

    # 构造测试数据
    dates = pd.date_range("2024-01-02", periods=100, freq="B")
    for symbol in ["TEST001.SZ", "TEST002.SZ"]:
        base = 10 + np.random.rand() * 50
        close = base * np.cumprod(1 + np.random.randn(100) * 0.02)
        df = pd.DataFrame({
            "open": close * 0.99, "high": close * 1.01,
            "low": close * 0.98, "close": close,
            "volume": np.random.randint(100000, 500000, 100),
        }, index=dates)
        df.index.name = "date"
        df.to_parquet(processed_dir / f"{symbol}.parquet")
    print(f"    测试数据: 2 个标的 x 100 根K线")
    print(f"    临时目录: {tmp_root}")

    # ── 测试 2: 首次同步（全部为新增） ──
    print("\n[2] 首次同步（全部为新增，应全部变更）")
    from myquant.indicators.factor_cache import FactorCache
    manager = CacheSyncManager(
        processed_dir=processed_dir,
        fingerprint_store_path=fingerprint_dir / "store.json",
        cache_dir=cache_dir,
        factor_cache_factory=lambda: FactorCache(
            cache_dir=str(cache_dir), enable_l1=True,
        ),
    )
    result1 = manager.sync_all(data_source="test")
    print(f"    扫描: {result1.total_scanned}")
    print(f"    变更: {len(result1.changed_symbols)}")
    print(f"    指纹更新: {result1.fingerprints_updated}")
    assert result1.total_scanned == 2
    assert len(result1.changed_symbols) == 2  # 首次全部视为变更
    assert result1.fingerprints_updated == 2
    print(f"    ✓ 首次同步正常")

    # ── 测试 3: 二次同步（数据未变更） ──
    print("\n[3] 二次同步（数据未变更，应无变更）")
    result2 = manager.sync_all(data_source="test")
    print(f"    扫描: {result2.total_scanned}")
    print(f"    变更: {len(result2.changed_symbols)}")
    print(f"    未变更: {result2.unchanged_symbols}")
    assert result2.total_scanned == 2
    assert len(result2.changed_symbols) == 0
    assert result2.unchanged_symbols == 2
    print(f"    ✓ 未变更数据不触发缓存失效")

    # ── 测试 4: 修改数据后同步（应检测到变更） ──
    print("\n[4] 修改数据后同步（应检测到变更）")
    # 修改 TEST001 的数据
    df_mod = pd.read_parquet(processed_dir / "TEST001.SZ.parquet")
    df_mod.iloc[-1, df_mod.columns.get_loc("close")] = 999.0
    df_mod.to_parquet(processed_dir / "TEST001.SZ.parquet")

    result3 = manager.sync_all(data_source="test")
    print(f"    变更标的: {result3.changed_symbols}")
    print(f"    缓存失效: {result3.cache_invalidated} 个标的, {result3.cache_entries_removed} 条")
    assert "TEST001.SZ" in result3.changed_symbols
    assert "TEST002.SZ" not in result3.changed_symbols
    print(f"    ✓ 变更检测正确（仅 TEST001.SZ 变更）")

    # ── 测试 5: 单标的同步 ──
    print("\n[5] 单标的同步测试")
    result4 = manager.sync_symbol("TEST002.SZ", data_source="test")
    print(f"    扫描: {result4.total_scanned}")
    print(f"    变更: {len(result4.changed_symbols)}")
    assert result4.total_scanned == 1
    assert len(result4.changed_symbols) == 0  # TEST002 未变更
    print(f"    ✓ 单标的同步正常")

    # ── 测试 6: 缓存状态查询 ──
    print("\n[6] 缓存状态查询")
    status = manager.get_cache_status()
    print(f"    指纹仓库标的数: {status['fingerprint_store']['symbol_count']}")
    print(f"    L2 文件数: {status['l2_file_count']}")
    print(f"    L2 大小: {status['l2_size_mb']} MB")
    assert status["fingerprint_store"]["symbol_count"] == 2
    print(f"    ✓ 缓存状态查询正常")

    # ── 测试 7: 指纹信息查询 ──
    print("\n[7] 指纹信息查询")
    fp_info = manager.get_fingerprint_info("TEST001.SZ")
    assert fp_info is not None
    print(f"    标的: {fp_info['symbol']}")
    print(f"    行数: {fp_info['row_count']}")
    print(f"    日期范围: {fp_info['date_range']}")
    print(f"    哈希: {fp_info['hash'][:16]}...")
    print(f"    ✓ 指纹信息查询正常")

    # ── 测试 8: 手动失效指定标的 ──
    print("\n[8] 手动失效指定标的缓存")
    removed = manager.invalidate_symbol("TEST001.SZ")
    print(f"    失效条目数: {removed}")
    print(f"    ✓ 手动失效正常")

    # ── 测试 9: 清理过期缓存 ──
    print("\n[9] 清理过期缓存")
    cleaned = manager.cleanup_expired_cache()
    print(f"    清理文件数: {cleaned}")
    print(f"    ✓ 过期缓存清理正常")

    # ── 测试 10: 边界情况 ──
    print("\n[10] 边界情况测试")
    # 不存在的标的
    result_none = manager.sync_symbol("NOTEXIST.SZ")
    assert result_none.total_scanned == 1
    assert len(result_none.errors) == 1
    print(f"    ✓ 不存在标的正确记录错误")

    # 空列表
    result_empty = manager.sync_symbols([])
    assert result_empty.total_scanned == 0
    print(f"    ✓ 空标的列表正常")

    # 清理
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"\n[清理] 临时目录已清理: {tmp_root}")

    print("\n" + "=" * 60)
    print("  ✓ cache_sync.py 全部自测通过")
    print("=" * 60)
