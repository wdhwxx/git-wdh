# 文件名：tests/unit/test_engine_benchmark.py
"""
test_engine_benchmark.py - P3-3 三模式回测引擎性能基准回归单元测试
==================================================================
覆盖 myquant/backtest/engine_benchmark.py：
  - EngineBenchmarkConfig  配置与防御校验
  - EngineBenchmark        三模式基准执行（耗时 / 吞吐 / 相对加速比）
  - EngineBenchmarkReport / EngineBenchmarkResult 结果结构

测试维度:
  1. 配置：默认值、total_bars、非法参数防御
  2. 执行：三模式都成功、期末净值一致性（三引擎同数据应一致）
  3. 加速比：event 基准 = 1.0，其余 >= 0；确定性可复现
  4. 报告：to_dict JSON 可序列化、summary 关键内容
  5. 快速模式可跑通

运行方式:
  pytest tests/unit/test_engine_benchmark.py -v
"""

from __future__ import annotations

import json

import pytest

from myquant.backtest.engine_benchmark import (
    BASELINE_ENGINE,
    DEFAULT_ENGINES,
    ENGINE_EVENT,
    ENGINE_MIXED,
    ENGINE_VECTORIZED,
    EngineBenchmark,
    EngineBenchmarkConfig,
    EngineBenchmarkReport,
    EngineBenchmarkResult,
)

# ─────────────────────────────────────────────
#  1. 配置
# ─────────────────────────────────────────────

class TestConfig:
    """基准配置测试。"""

    def test_defaults(self) -> None:
        """默认规模应合法。"""
        config = EngineBenchmarkConfig()
        assert config.n_bars >= 20
        assert config.n_symbols >= 1
        assert config.repetitions >= 1
        assert config.warmup is True

    def test_total_bars(self) -> None:
        """total_bars = n_bars * n_symbols。"""
        config = EngineBenchmarkConfig(n_bars=100, n_symbols=3)
        assert config.total_bars == 300

    def test_invalid_n_bars_raises(self) -> None:
        """n_bars < 20 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            EngineBenchmarkConfig(n_bars=10)

    def test_invalid_n_symbols_raises(self) -> None:
        """n_symbols < 1 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            EngineBenchmarkConfig(n_symbols=0)

    def test_invalid_repetitions_raises(self) -> None:
        """repetitions < 1 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            EngineBenchmarkConfig(repetitions=0)

    def test_invalid_engine_raises(self) -> None:
        """非法引擎模式应抛出 ValueError。"""
        with pytest.raises(ValueError):
            EngineBenchmark(engines=("bogus",))


# ─────────────────────────────────────────────
#  2. 基准执行
# ─────────────────────────────────────────────

class TestBenchmarkRun:
    """三模式基准执行测试。"""

    @staticmethod
    def _quick_config() -> EngineBenchmarkConfig:
        """快速基准配置（小规模，测试提速）。"""
        return EngineBenchmarkConfig(
            n_bars=100, n_symbols=1, repetitions=1, seed=7, warmup=False
        )

    def test_three_engines_all_success(self) -> None:
        """三模式都应成功完成。"""
        report = EngineBenchmark(self._quick_config()).run()
        assert isinstance(report, EngineBenchmarkReport)
        assert len(report.results) == len(DEFAULT_ENGINES) == 3
        for r in report.results:
            assert r.is_success is True, f"{r.engine} 失败: {r.error}"
            assert r.elapsed >= 0
            assert r.bars_per_sec > 0
            assert r.engine in DEFAULT_ENGINES

    def test_final_equity_consistent_across_engines(self) -> None:
        """三引擎同一策略同数据，期末净值应一致。"""
        report = EngineBenchmark(self._quick_config()).run()
        equities = {r.engine: r.final_equity for r in report.results}
        assert equities[ENGINE_EVENT] == pytest.approx(equities[ENGINE_MIXED], rel=1e-6)
        assert equities[ENGINE_EVENT] == pytest.approx(equities[ENGINE_VECTORIZED], rel=1e-6)

    def test_trades_consistent_across_engines(self) -> None:
        """三引擎成交笔数应一致（同一策略口径）。"""
        report = EngineBenchmark(self._quick_config()).run()
        trades = {r.engine: r.trades for r in report.results}
        assert trades[ENGINE_EVENT] == trades[ENGINE_MIXED] == trades[ENGINE_VECTORIZED]

    def test_event_baseline_speedup_is_one(self) -> None:
        """event 基准引擎加速比应为 1.0。"""
        report = EngineBenchmark(self._quick_config()).run()
        assert report.speedups[BASELINE_ENGINE] == 1.0

    def test_speedup_non_negative(self) -> None:
        """其余引擎加速比应为非负数。"""
        report = EngineBenchmark(self._quick_config()).run()
        for engine in DEFAULT_ENGINES:
            assert report.speedups[engine] >= 0

    def test_deterministic_trades_and_equity(self) -> None:
        """同配置两次运行，成交与期末净值应可复现（确定性）。"""
        r1 = EngineBenchmark(self._quick_config()).run()
        r2 = EngineBenchmark(self._quick_config()).run()
        for a, b in zip(r1.results, r2.results):
            assert a.trades == b.trades
            assert a.final_equity == pytest.approx(b.final_equity)


# ─────────────────────────────────────────────
#  3. 报告结构
# ─────────────────────────────────────────────

class TestReport:
    """报告结构测试。"""

    def test_to_dict_json_serializable(self) -> None:
        """to_dict 应可 JSON 序列化且含关键字段。"""
        report = EngineBenchmark(
            EngineBenchmarkConfig(n_bars=100, n_symbols=1, repetitions=1, warmup=False)
        ).run()
        d = report.to_dict()
        assert d["baseline"] == BASELINE_ENGINE
        assert d["config"]["total_bars"] == 100
        assert len(d["results"]) == 3
        assert len(d["speedups"]) == 3
        json.dumps(d, ensure_ascii=False)

    def test_result_to_dict(self) -> None:
        """EngineBenchmarkResult.to_dict 字段完整。"""
        report = EngineBenchmark(
            EngineBenchmarkConfig(n_bars=100, n_symbols=1, repetitions=1, warmup=False)
        ).run()
        r: EngineBenchmarkResult = report.results[0]
        d = r.to_dict()
        for key in ("engine", "elapsed_s", "bars_per_sec", "trades", "final_equity", "is_success"):
            assert key in d

    def test_summary_contains_engines_and_speedups(self) -> None:
        """summary 应包含三引擎名与相对加速比。"""
        report = EngineBenchmark(
            EngineBenchmarkConfig(n_bars=100, n_symbols=1, repetitions=1, warmup=False)
        ).run()
        text = report.summary()
        for engine in DEFAULT_ENGINES:
            assert engine in text
        assert "相对加速比" in text
        assert "吞吐量" in text
