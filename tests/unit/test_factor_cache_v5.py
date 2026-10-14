# 文件名：tests/unit/test_factor_cache_v5.py
"""
test_factor_cache_v5.py - P3-2 三级因子缓存回归单元测试
=======================================================
覆盖 myquant/indicators/factor_cache.py 与 factor_engine.py：
  - FactorCache    L1(内存)→L2(磁盘)→L3(回源计算) 三级降级缓存
  - FactorEngine   向量化因子引擎（批量多因子 + 缓存联动 + 指纹）

测试维度:
  1. L1 命中：get_or_compute 二次调用不重算（l1）
  2. L2 命中：跨实例磁盘缓存（enable_l1=False 走 L2）
  3. 失效键：不同参数 / 不同指纹 → 重算；invalidate 定向失效
  4. 统计：l1_hits / l2_hits / l3_misses / l2_writes 计数
  5. 防御：compute_func 不可调用 / data 非 DataFrame / compute 返回 None
  6. 并行模式：强制关闭 L1；单例 get_factor_cache + reset_factor_cache
  7. FactorEngine：单标的多因子合并、二次命中、批量、带统计、use_cache=False

运行方式:
  pytest tests/unit/test_factor_cache_v5.py -v
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from myquant.indicators.factor_cache import (
    CACHE_HIT_L1,
    CACHE_HIT_L2,
    CACHE_HIT_L3,
    FactorCache,
    get_factor_cache,
    is_parallel_mode,
    reset_factor_cache,
    set_parallel_mode,
)
from myquant.indicators.factor_engine import (
    FactorEngine,
)
from myquant.utils.data_fingerprint import compute_fingerprint
from tests.conftest import make_ohlcv_df

# ─────────────────────────────────────────────
#  辅助
# ─────────────────────────────────────────────

def _make_df(n_rows: int = 60) -> pd.DataFrame:
    """构造带 OHLCV 列的行情（SMA 计算所需，中文列，#38 规范）。"""
    return make_ohlcv_df(
        closes=[10.0 + i * 0.01 for i in range(n_rows)],
        opens=10.0,
        highs=10.5,
        lows=9.5,
        volumes=10000.0,
        start="2023-01-02",
    )


def _sma_compute(period: int = 20) -> Callable[[pd.DataFrame, dict[str, Any]], pd.DataFrame]:
    """构造 SMA 计算函数（闭包参数透传）。"""
    def _compute(data: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        p = int(params.get("period", period))
        return pd.DataFrame({"sma": data["收盘价"].rolling(p).mean()})
    return _compute


def _fingerprint() -> Any:
    """构造固定指纹。"""
    return compute_fingerprint(_make_df(60), "600519.SH", data_source="test")


# ═════════════════════════════════════════════
#  1. FactorCache 基础与三级命中
# ═════════════════════════════════════════════

class TestFactorCache:
    """三级因子缓存基础测试。"""

    def test_init_creates_cache_dir(self, tmp_path: Path) -> None:
        """L2 启用时应创建缓存目录。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"), l2_enabled=True)
        assert (tmp_path / "cache").is_dir()
        assert cache.enable_l1 is True
        assert cache.cache_dir == tmp_path / "cache"

    def test_get_or_compute_l3_then_l1(self, tmp_path: Path) -> None:
        """首次 l3 计算，二次同键 l1 命中且不重算。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        calls: list[int] = []

        def _counting(df_: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
            calls.append(1)
            return pd.DataFrame({"sma": df_["收盘价"].rolling(20).mean()})

        result1, hit1 = cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _counting, _fingerprint()
        )
        result2, hit2 = cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _counting, _fingerprint()
        )
        assert hit1 == CACHE_HIT_L3
        assert hit2 == CACHE_HIT_L1
        assert len(calls) == 1  # 只计算了一次
        pd.testing.assert_frame_equal(result1, result2)

    def test_different_params_recompute(self, tmp_path: Path) -> None:
        """不同参数应走不同缓存键，触发重算。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        r1, h1 = cache.get_or_compute(
            "sma", "600519.SH", {"period": 5}, df, _sma_compute(), _fingerprint()
        )
        r2, h2 = cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _sma_compute(), _fingerprint()
        )
        assert h1 == CACHE_HIT_L3
        assert h2 == CACHE_HIT_L3
        assert len(r1.columns) == len(r2.columns) == 1

    def test_different_fingerprint_recompute(self, tmp_path: Path) -> None:
        """数据指纹不同应触发重算（即使参数相同）。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"))
        df1 = _make_df(60)
        df2 = _make_df(61)  # 行数不同 → 指纹不同
        calls: list[int] = []

        def _counting(df_: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
            calls.append(1)
            return pd.DataFrame({"sma": df_["收盘价"].rolling(20).mean()})

        cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df1, _counting, _fingerprint()
        )
        cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df2, _counting,
            compute_fingerprint(df2, "600519.SH", data_source="test"),
        )
        assert len(calls) == 2

    def test_l2_hit_across_instances(self, tmp_path: Path) -> None:
        """关闭 L1 时二次调用（新实例）应命中 L2 磁盘缓存。"""
        cache1 = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        cache2 = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=False)
        df = _make_df()
        calls: list[int] = []

        def _counting(df_: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
            calls.append(1)
            return pd.DataFrame({"sma": df_["收盘价"].rolling(20).mean()})

        _, h1 = cache1.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _counting, _fingerprint()
        )
        _, h2 = cache2.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _counting, _fingerprint()
        )
        assert h1 == CACHE_HIT_L3
        assert h2 == CACHE_HIT_L2
        assert len(calls) == 1

    def test_compute_none_returns_empty_and_no_cache(self, tmp_path: Path) -> None:
        """compute_func 返回 None 时应返回空 DataFrame 且不写缓存。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        result, hit = cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df,
            lambda d, p: None,  # type: ignore[return-value]
            _fingerprint(),
        )
        assert hit == CACHE_HIT_L3
        assert result.empty
        # 再次调用仍应重算（未写缓存）
        calls: list[int] = []

        def _counting(df_: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
            calls.append(1)
            return pd.DataFrame({"sma": df_["收盘价"].rolling(20).mean()})

        _, h2 = cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _counting, _fingerprint()
        )
        assert h2 == CACHE_HIT_L3
        assert len(calls) == 1


# ═════════════════════════════════════════════
#  2. FactorCache 失效 / 清理 / 统计
# ═════════════════════════════════════════════

class TestFactorCacheInvalidate:
    """缓存失效 / 清理 / 统计测试。"""

    def test_invalidate_by_factor_and_symbol(self, tmp_path: Path) -> None:
        """invalidate 支持按因子 / 标的过滤，None 时清空。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        fp = _fingerprint()
        for factor in ("sma", "rsrs"):
            for symbol in ("600519.SH", "000001.SZ"):
                cache.get_or_compute(
                    factor, symbol, {"period": 20}, df, _sma_compute(), fp
                )
        # 按因子失效
        n = cache.invalidate(factor_name="sma")
        assert n > 0
        # 按标的失效
        n = cache.invalidate(symbol="600519.SH")
        assert n > 0
        # 全部清空
        n = cache.invalidate()
        assert n > 0

    def test_clear_and_cleanup(self, tmp_path: Path) -> None:
        """clear 应清空 L1+L2；cleanup_expired 应返回删除数（ttl=0 不过期）。"""
        cache = FactorCache(
            cache_dir=str(tmp_path / "cache"),
            l2_ttl=0,  # 永不过期
        )
        df = _make_df()
        cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _sma_compute(), _fingerprint()
        )
        assert cache.cleanup_expired() == 0  # 无过期项
        cache.clear()
        # 清理后应重算
        calls: list[int] = []

        def _counting(df_: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
            calls.append(1)
            return pd.DataFrame({"sma": df_["收盘价"].rolling(20).mean()})

        cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _counting, _fingerprint()
        )
        assert len(calls) == 1

    def test_stats_counters(self, tmp_path: Path) -> None:
        """统计计数：l1_hits / l3_misses / l2_writes 等。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _sma_compute(), _fingerprint()
        )
        cache.get_or_compute(
            "sma", "600519.SH", {"period": 20}, df, _sma_compute(), _fingerprint()
        )
        stats = cache.stats()
        assert stats["l3_misses"] == 1
        assert stats["l1_hits"] >= 1
        assert stats["l2_writes"] == 1

    def test_validation_errors(self, tmp_path: Path) -> None:
        """非法参数防御：不可调用 compute_func / 非 DataFrame。"""
        cache = FactorCache(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        with pytest.raises(ValueError):
            cache.get_or_compute(  # type: ignore[arg-type]
                "sma", "600519.SH", {}, df, "not_callable", _fingerprint()
            )
        with pytest.raises(TypeError):
            cache.get_or_compute(  # type: ignore[arg-type]
                "sma", "600519.SH", {}, [1, 2, 3], _sma_compute(), _fingerprint()
            )


# ═════════════════════════════════════════════
#  3. FactorCache 并行模式 / 单例
# ═════════════════════════════════════════════

class TestFactorCacheParallelMode:
    """并行模式强制关闭 L1 与单例测试。"""

    def test_parallel_mode_forces_l1_off(self, tmp_path: Path) -> None:
        """并行模式下 enable_l1 应被强制为 False。"""
        set_parallel_mode(True)
        try:
            assert is_parallel_mode() is True
            cache = FactorCache(cache_dir=str(tmp_path / "cache"), enable_l1=True)
            assert cache.enable_l1 is False
        finally:
            set_parallel_mode(False)
        assert is_parallel_mode() is False

    def test_singleton_and_reset(self, tmp_path: Path) -> None:
        """get_factor_cache 应返回同一实例，reset 后重建。"""
        try:
            c1 = get_factor_cache(cache_dir=str(tmp_path / "cache"))
            c2 = get_factor_cache(cache_dir=str(tmp_path / "cache"))
            assert c1 is c2
            reset_factor_cache()
            c3 = get_factor_cache(cache_dir=str(tmp_path / "cache"))
            assert c3 is not c1
        finally:
            reset_factor_cache()


# ═════════════════════════════════════════════
#  4. FactorEngine 因子引擎
# ═════════════════════════════════════════════

class TestFactorEngine:
    """向量化因子引擎测试。"""

    def test_calculate_merges_prefixed_columns(self, tmp_path: Path) -> None:
        """calculate 应合并各因子结果并加前缀。"""
        engine = FactorEngine(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        result = engine.calculate(
            data=df,
            symbol="600519.SH",
            factor_configs=[
                {"name": "sma", "params": {"period": 20}, "prefix": "sma20_"},
                {"name": "sma", "params": {"period": 5}, "prefix": "sma5_"},
            ],
        )
        assert "sma20_sma" in result.columns
        assert "sma5_sma" in result.columns
        assert result.shape[0] == len(df)

    def test_second_calculate_hits_cache(self, tmp_path: Path) -> None:
        """同参数二次计算应命中缓存（l3_misses 不增加）。"""
        engine = FactorEngine(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        configs = [{"name": "sma", "params": {"period": 20}, "prefix": "sma20_"}]
        engine.calculate(data=df, symbol="600519.SH", factor_configs=configs)
        l3_after_first = engine.stats()["l3_misses"]
        engine.calculate(data=df, symbol="600519.SH", factor_configs=configs)
        l3_after_second = engine.stats()["l3_misses"]
        assert l3_after_second == l3_after_first  # 未触发重算

    def test_calculate_batch_multi_symbol(self, tmp_path: Path) -> None:
        """calculate_batch 应返回每标的计算结果。"""
        engine = FactorEngine(cache_dir=str(tmp_path / "cache"))
        results = engine.calculate_batch(
            data_dict={
                "600519.SH": _make_df(60),
                "000001.SZ": _make_df(50),
            },
            factor_configs=[{"name": "sma", "params": {"period": 20}}],
        )
        assert set(results.keys()) == {"600519.SH", "000001.SZ"}
        assert results["600519.SH"].shape[0] == 60
        assert results["000001.SZ"].shape[0] == 50

    def test_calculate_with_stats(self, tmp_path: Path) -> None:
        """calculate_with_stats 应返回 (结果, 本次调用统计)。"""
        engine = FactorEngine(cache_dir=str(tmp_path / "cache"))
        result, call_stats = engine.calculate_with_stats(
            data=_make_df(),
            symbol="600519.SH",
            factor_configs=[{"name": "sma", "params": {"period": 20}}],
        )
        assert not result.empty
        assert call_stats["factor_count"] == 1
        assert call_stats["symbol"] == "600519.SH"
        assert call_stats["l3_misses"] >= 0
        assert call_stats["hit_rate"] >= 0.0

    def test_use_cache_false_always_recompute(self, tmp_path: Path) -> None:
        """use_cache=False 时 cache 为 None，每次都重算。"""
        engine = FactorEngine(cache_dir=str(tmp_path / "cache"), use_cache=False)
        assert engine.use_cache is False
        assert engine.cache is None
        df = _make_df()
        configs = [{"name": "sma", "params": {"period": 20}}]
        r1 = engine.calculate(data=df, symbol="600519.SH", factor_configs=configs)
        r2 = engine.calculate(data=df, symbol="600519.SH", factor_configs=configs)
        assert not r1.empty and not r2.empty

    def test_raise_on_error(self, tmp_path: Path) -> None:
        """raise_on_error=False 应跳过失败因子；True 应抛出。"""
        engine = FactorEngine(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        bad_configs = [{"name": "no_such_factor_xyz", "params": {}}]
        # 默认跳过
        result = engine.calculate(
            data=df, symbol="600519.SH", factor_configs=bad_configs
        )
        assert "no_such_factor_xyz" not in result.columns
        # 严格模式抛出
        with pytest.raises(ValueError):
            engine.calculate(
                data=df,
                symbol="600519.SH",
                factor_configs=bad_configs,
                raise_on_error=True,
            )

    def test_invalidate_and_stats(self, tmp_path: Path) -> None:
        """invalidate / clear_cache / reset_stats 可用且不抛异常。"""
        engine = FactorEngine(cache_dir=str(tmp_path / "cache"))
        df = _make_df()
        engine.calculate(
            data=df,
            symbol="600519.SH",
            factor_configs=[{"name": "sma", "params": {"period": 20}}],
        )
        assert engine.invalidate() >= 0
        engine.reset_stats()
        stats = engine.stats()
        assert stats["l3_misses"] == 0
        engine.clear_cache()
        assert engine.cache is not None
        assert engine.factor_stats() == {}
