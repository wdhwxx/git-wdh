# -*- coding: utf-8 -*-
"""T-41 KP-P2-04 基准缺失：空基准告警标记 + alpha/beta/excess 显式 None。"""
import io
from pathlib import Path

import yaml

from myquant.backtest.metrics import MetricsCalculator
from myquant.models.backtest import EquityPoint, PerformanceMetrics
from myquant.utils import math_utils

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "myquant" / "backtest" / "engine.py"
YAML = ROOT / "config" / "backtest_config.example.yaml"


def _eps() -> list:
    return [
        EquityPoint(timestamp=0.0, date="2024-01-02", equity=1.0, cash=1.0, position_value=0.0, drawdown=0.0, benchmark_equity=1.0),
        EquityPoint(timestamp=86400.0, date="2024-01-03", equity=1.1, cash=1.0, position_value=0.1, drawdown=0.0, benchmark_equity=1.05),
    ]


def test_metrics_default_alpha_beta_none():
    """PerformanceMetrics 默认 alpha/beta/benchmark_return/excess_return 为 None。"""
    m = PerformanceMetrics()
    assert m.alpha is None
    assert m.beta is None
    assert m.benchmark_return is None
    assert m.excess_return is None


def test_metrics_calc_no_benchmark_sets_none():
    """MetricsCalculator 无基准曲线 → alpha/beta/excess/benchmark_return 显式 None。"""
    m = MetricsCalculator(risk_free_rate=0.02).calculate(_eps(), [], benchmark_curve=None)
    assert m.alpha is None
    assert m.beta is None
    assert m.excess_return is None
    assert m.benchmark_return is None


def test_math_utils_report_no_benchmark_none():
    """math_utils.performance_report 无基准 → beta/alpha 显式 None。"""
    report = math_utils.performance_report([0.01, 0.02], benchmark_returns=None)
    assert report["beta"] is None
    assert report["alpha"] is None


def test_math_utils_report_with_benchmark_numeric():
    """有基准 → beta/alpha 为数值（非 None）。"""
    report = math_utils.performance_report([0.01, 0.02], benchmark_returns=[0.005, 0.01])
    assert report["beta"] is not None
    assert report["alpha"] is not None


def test_engine_marks_benchmark_missing():
    """engine 基准加载空分支：标记 _benchmark_missing 且结果 extra 落盘。"""
    src = ENGINE.read_text(encoding="utf-8")
    assert "self._benchmark_missing" in src
    assert "基准 {self.config.benchmark} 数据缺失" in src
    assert "result.extra['benchmark_missing'] = True" in src


def test_yaml_benchmark_required():
    """example yaml 增 benchmark_required: true。"""
    cfg = yaml.safe_load(io.open(YAML, "r", encoding="utf-8"))
    assert cfg["backtest"].get("benchmark_required") is True
