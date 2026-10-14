# 文件名：tests/unit/test_factor_cache_nofp.py
"""
test_factor_cache_nofp.py - F-13 因子缓存失效解析修复测试
==========================================================
覆盖 myquant/indicators/factor_cache.py 的两项修复：
  1. L2 目录分层存储（{factor_name}/{symbol}/{params_hash}_{fp}.parquet）：
     消灭因子名含下划线（bollinger_bands 等）时 invalidate 解析失败的 bug；
     invalidate 按目录删除，无需从文件名反向切分。
  2. nofp（无数据指纹）短 TTL + WARNING：
     无指纹时缓存文件名固定 *_nofp.parquet，无法感知数据更新；
     现已封顶 NOFP_FINGERPRINT_TTL=3600s 并打 WARNING。

测试维度:
  1. 目录分层结构：下划线因子名落在独立因子目录下
  2. 下划线因子名 invalidate 100% 失效（数据更新后重算）
  3. invalidate(symbol=) 跨因子删除；invalidate(factor+symbol) 组合
  4. nofp WARNING 日志可见（caplog）
  5. nofp 短 TTL：旧 mtime 的 nofp 文件被视为过期重算
  6. 旧版扁平散文件兼容清理
  7. stats/cleanup_expired 递归计数

运行方式:
  pytest tests/unit/test_factor_cache_nofp.py -v
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from myquant.indicators.factor_cache import (
    CACHE_HIT_L2,
    CACHE_HIT_L3,
    NO_FINGERPRINT_PLACEHOLDER,
    NOFP_FINGERPRINT_TTL,
    FactorCache,
    _build_l2_relpath,
    _sanitize_path_segment,
)
from myquant.utils.data_fingerprint import compute_fingerprint
from tests.conftest import make_ohlcv_df

# ─────────────────────────────────────────────
#  辅助
# ─────────────────────────────────────────────

def _make_df(n_rows: int = 60) -> pd.DataFrame:
    """构造带 OHLCV 列的行情（中文列，#38 规范）。"""
    return make_ohlcv_df(
        closes=[10.0 + i * 0.01 for i in range(n_rows)],
        opens=10.0,
        highs=10.5,
        lows=9.5,
        volumes=10000.0,
        start="2023-01-02",
    )


def _fingerprint() -> Any:
    """构造固定指纹。"""
    return compute_fingerprint(_make_df(60), "600519.SH", data_source="test")


def _sma_compute() -> Any:
    """构造 SMA 计算函数（返回带计数的闭包）。"""
    calls: list[int] = []

    def _compute(data: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        calls.append(1)
        p = int(params.get("period", 20))
        return pd.DataFrame({"sma": data["收盘价"].rolling(p).mean()})

    _compute.calls = calls  # type: ignore[attr-defined]
    return _compute


# ═════════════════════════════════════════════
#  1. 路径构建与目录分层
# ═════════════════════════════════════════════

class TestL2DirectoryLayout:
    """L2 目录分层路径构建。"""

    def test_sanitize_path_segment(self) -> None:
        """特殊字符替换为下划线，alnum/._- 保留。"""
        assert _sanitize_path_segment("bollinger_bands") == "bollinger_bands"
        assert _sanitize_path_segment("600519.SH") == "600519.SH"
        assert _sanitize_path_segment("a/b:c*d") == "a_b_c_d"

    def test_relpath_nested_for_underscore_factor(self) -> None:
        """下划线因子名路径应落在因子名一级目录下，而非扁平文件名。"""
        fp = _fingerprint()
        rel = _build_l2_relpath("bollinger_bands", "600519.SH", {"period": 20}, fp)
        # 应为 bollinger_bands/600519.SH/{params}_{fp}.parquet
        assert rel.parts[0] == "bollinger_bands"
        assert rel.parts[1] == "600519.SH"
        assert rel.suffix == ".parquet"
        # 文件名（最后一段）应只含 params_hash 与 fp_short 两段
        filename = rel.stem
        parts = filename.split("_")
        assert len(parts) == 2, f"文件名应只含两段，got: {parts}"

    def test_relpath_nofp_placeholder(self) -> None:
        """无指纹时文件名末段为 nofp。"""
        rel = _build_l2_relpath("sma", "600519.SH", {"period": 20}, None)
        assert rel.stem.endswith(f"_{NO_FINGERPRINT_PLACEHOLDER}")

    def test_write_creates_nested_dirs(self, tmp_path: Path) -> None:
        """写入 L2 应自动创建因子/symbol 子目录。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        cache.get_or_compute(
            "bollinger_bands", "600519.SH", {"period": 20},
            df, _sma_compute(), _fingerprint(),
        )
        factor_dir = tmp_path / "cache" / "bollinger_bands" / "600519.SH"
        assert factor_dir.is_dir()
        parquets = list(factor_dir.glob("*.parquet"))
        assert len(parquets) == 1


# ═════════════════════════════════════════════
#  2. 下划线因子名 invalidate 100% 失效
# ═════════════════════════════════════════════

class TestUnderscoreFactorInvalidate:
    """F-13 核心回归：含下划线因子名 invalidate 后 L2 100% 失效。"""

    def test_invalidate_underscore_factor_deletes_dir(self, tmp_path: Path) -> None:
        """invalidate(factor_name='bollinger_bands') 应删除整个因子目录。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        fp = _fingerprint()
        cache.get_or_compute("bollinger_bands", "600519.SH", {"period": 20}, df, _sma_compute(), fp)
        cache.get_or_compute("bollinger_bands", "000001.SZ", {"period": 20}, df, _sma_compute(), fp)
        cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, _sma_compute(), fp)

        removed = cache.invalidate(factor_name="bollinger_bands")
        assert removed == 2, f"应删除 2 个 bollinger_bands 缓存文件，got: {removed}"
        # 因子目录应不存在
        assert not (tmp_path / "cache" / "bollinger_bands").exists()
        # sma 目录应保留
        assert (tmp_path / "cache" / "sma").is_dir()

    def test_invalidate_underscore_factor_then_recompute(self, tmp_path: Path) -> None:
        """invalidate 后同参数重新 get_or_compute 应走 L3（不再命中旧缓存）。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        fp = _fingerprint()
        comp = _sma_compute()
        _, h1 = cache.get_or_compute("bollinger_bands", "600519.SH", {"period": 20}, df, comp, fp)
        assert h1 == CACHE_HIT_L3
        n_calls = len(comp.calls)

        # invalidate 后再取
        cache.invalidate(factor_name="bollinger_bands")
        _, h2 = cache.get_or_compute("bollinger_bands", "600519.SH", {"period": 20}, df, comp, fp)
        assert h2 == CACHE_HIT_L3, f"invalidate 后应 L3 重算，got: {h2}"
        assert len(comp.calls) == n_calls + 1

    def test_invalidate_by_symbol_cross_factor(self, tmp_path: Path) -> None:
        """invalidate(symbol='600519.SH') 应跨因子删除该 symbol 子目录。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        fp = _fingerprint()
        cache.get_or_compute("bollinger_bands", "600519.SH", {"period": 20}, df, _sma_compute(), fp)
        cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, _sma_compute(), fp)
        cache.get_or_compute("sma", "000001.SZ", {"period": 20}, df, _sma_compute(), fp)

        removed = cache.invalidate(symbol="600519.SH")
        assert removed == 2, f"应删 2 个 600519.SH 文件，got: {removed}"
        # 600519.SH 在两个因子下都应被删
        assert not (tmp_path / "cache" / "bollinger_bands" / "600519.SH").exists()
        assert not (tmp_path / "cache" / "sma" / "600519.SH").exists()
        # 000001.SZ 保留
        assert (tmp_path / "cache" / "sma" / "000001.SZ").is_dir()

    def test_invalidate_factor_and_symbol_combination(self, tmp_path: Path) -> None:
        """invalidate(factor+symbol) 只删组合，不影响其他。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        fp = _fingerprint()
        cache.get_or_compute("bollinger_bands", "600519.SH", {"period": 20}, df, _sma_compute(), fp)
        cache.get_or_compute("bollinger_bands", "000001.SZ", {"period": 20}, df, _sma_compute(), fp)

        removed = cache.invalidate(factor_name="bollinger_bands", symbol="600519.SH")
        assert removed == 1
        assert not (tmp_path / "cache" / "bollinger_bands" / "600519.SH").exists()
        assert (tmp_path / "cache" / "bollinger_bands" / "000001.SZ").is_dir()

    def test_invalidate_all_clears_subdirs(self, tmp_path: Path) -> None:
        """invalidate() 无参应清空所有因子子目录。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        fp = _fingerprint()
        for f in ("sma", "bollinger_bands", "rsrs"):
            cache.get_or_compute(f, "600519.SH", {"period": 20}, df, _sma_compute(), fp)
        removed = cache.invalidate()
        assert removed >= 3
        # 缓存目录下不应再有因子子目录
        subdirs = [p for p in (tmp_path / "cache").iterdir() if p.is_dir()]
        assert len(subdirs) == 0


# ═════════════════════════════════════════════
#  3. nofp 短 TTL + WARNING
# ═════════════════════════════════════════════

class TestNofpShortTtl:
    """nofp（无指纹）场景短 TTL 兜底与告警。"""

    def test_nofp_warning_logged_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """无指纹写入/读取应打 WARNING，同一 factor/symbol 只告警一次。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False, l2_ttl=0)
        df = _make_df()
        with caplog.at_level(logging.WARNING, logger="myquant.indicators.factor_cache"):
            comp = _sma_compute()
            cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, None)
            # 第二次同 factor/symbol 不应再告警
            cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, None)

        nofp_warnings = [r for r in caplog.records if "nofp" in r.getMessage().lower()]
        assert len(nofp_warnings) == 1, f"应只告警一次，got: {len(nofp_warnings)}"

    def test_nofp_short_ttl_expires_old_file(self, tmp_path: Path) -> None:
        """nofp 文件即使 l2_ttl=0（永不过期），超过 NOFP_FINGERPRINT_TTL 也应重算。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False, l2_ttl=0)
        df = _make_df()
        comp = _sma_compute()
        # 第一次写入 nofp 缓存
        cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, None)
        n_after_write = len(comp.calls)

        # 手动把 mtime 改为 2 小时前（> NOFP_FINGERPRINT_TTL=3600s）
        rel = _build_l2_relpath("sma", "600519.SH", {"period": 20}, None)
        old_file = tmp_path / "cache" / rel
        old_time = time.time() - 2 * NOFP_FINGERPRINT_TTL
        os.utime(old_file, (old_time, old_time))

        # 再次 get_or_compute：应识别 nofp 文件过期 → L3 重算
        _, hit = cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, None)
        assert hit == CACHE_HIT_L3, f"过期 nofp 文件应触发重算，got: {hit}"
        assert len(comp.calls) == n_after_write + 1

    def test_nofp_within_ttl_hits_l2(self, tmp_path: Path) -> None:
        """nofp 文件在短 TTL 内应正常命中 L2（不重算）。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False, l2_ttl=0)
        df = _make_df()
        comp = _sma_compute()
        cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, None)
        n = len(comp.calls)
        # 刚写入（mtime=now），应 L2 命中
        _, hit = cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, None)
        assert hit == CACHE_HIT_L2
        assert len(comp.calls) == n


# ═════════════════════════════════════════════
#  4. 旧扁平文件兼容与递归统计
# ═════════════════════════════════════════════

class TestLegacyFlatFiles:
    """旧版扁平布局散文件在清空时被兼容清理。"""

    def test_clear_removes_legacy_flat_files(self, tmp_path: Path) -> None:
        """根目录下的旧扁平 .parquet 在 invalidate()/clear() 时被清理。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        cache.cache_dir.mkdir(parents=True, exist_ok=True)
        # 模拟旧版扁平散文件
        legacy = cache.cache_dir / "old_sma_600519.SH_abcd1234ef567890_nofp.parquet"
        legacy.write_bytes(b"fake")
        assert legacy.exists()
        # invalidate() 全清应删根目录散文件
        removed = cache.invalidate()
        assert removed >= 1
        assert not legacy.exists()

    def test_stats_recursive_l2_files(self, tmp_path: Path) -> None:
        """stats() 的 l2_files 应递归统计子目录中的 parquet。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        fp = _fingerprint()
        cache.get_or_compute("sma", "600519.SH", {"period": 20}, df, _sma_compute(), fp)
        cache.get_or_compute("sma", "000001.SZ", {"period": 20}, df, _sma_compute(), fp)
        cache.get_or_compute("bollinger_bands", "600519.SH", {"period": 20}, df, _sma_compute(), fp)
        stats = cache.stats()
        assert stats["l2_files"] == 3, f"应递归统计 3 个文件，got: {stats['l2_files']}"


# ═════════════════════════════════════════════
#  5. 既有行为回归锚
# ═════════════════════════════════════════════

class TestRegressionAnchors:
    """确保 F-13 改动不破坏既有三级命中/指纹失效语义。"""

    def test_l1_then_l2_still_works(self, tmp_path: Path) -> None:
        """三级命中主路径不受影响。"""
        cache1 = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        cache2 = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        comp = _sma_compute()
        _, h1 = cache1.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, _fingerprint())
        _, h2 = cache2.get_or_compute("sma", "600519.SH", {"period": 20}, df, comp, _fingerprint())
        assert h1 == CACHE_HIT_L3
        assert h2 == CACHE_HIT_L2
        assert len(comp.calls) == 1

    def test_fingerprint_change_still_recomputes(self, tmp_path: Path) -> None:
        """指纹变更仍触发 L3（新目录路径下 fp_short 不同）。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df1 = _make_df(60)
        df2 = _make_df(61)
        comp = _sma_compute()
        fp1 = compute_fingerprint(df1, "600519.SH", data_source="test")
        fp2 = compute_fingerprint(df2, "600519.SH", data_source="test")
        cache.get_or_compute("sma", "600519.SH", {"period": 20}, df1, comp, fp1)
        cache.get_or_compute("sma", "600519.SH", {"period": 20}, df2, comp, fp2)
        assert len(comp.calls) == 2
