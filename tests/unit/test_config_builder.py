# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_config_builder.py
"""
回测配置构建（myquant/backtest/config_builder.py）单元测试（G0-9 补测）
==========================================================
覆盖：
  - load_stock_pool_from_csv：CSV 加载代码列 / 缺列报错
  - apply_exclude_prefixes：前缀过滤 / 空前缀原样返回
  - build_backtest_config：CLI--symbols 优先 / stock-pool 文件 / 配置 codes /
    three_factor 显式 raise / 无标的 raise / 排除前缀生效 / 返回合法 BacktestConfig
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pytest

from myquant.backtest.config_builder import (
    apply_exclude_prefixes,
    build_backtest_config,
    load_stock_pool_from_csv,
)


# ─────────────────────────────────────────────
# apply_exclude_prefixes
# ─────────────────────────────────────────────
class TestApplyExcludePrefixes:
    def test_empty_prefixes_returns_original(self) -> None:
        symbols = ["600519.SH", "300750.SZ"]
        assert apply_exclude_prefixes(symbols, []) == symbols

    def test_filters_by_prefix_with_suffix_stripped(self) -> None:
        symbols = ["600519.SH", "300750.SZ", "688111.SH", "000001.SZ"]
        filtered = apply_exclude_prefixes(symbols, ["30", "688"])
        assert filtered == ["600519.SH", "000001.SZ"]

    def test_filters_bare_codes(self) -> None:
        symbols = ["600519", "300750", "000001"]
        assert apply_exclude_prefixes(symbols, ["300"]) == ["600519", "000001"]


# ─────────────────────────────────────────────
# load_stock_pool_from_csv
# ─────────────────────────────────────────────
class TestLoadStockPoolFromCsv:
    def test_loads_code_column(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "pool.csv"
        pd.DataFrame({"code": ["600519.SH", "300750.SZ"]}).to_csv(
            csv_path, index=False, encoding="utf-8-sig",
        )
        assert load_stock_pool_from_csv(str(csv_path)) == ["600519.SH", "300750.SZ"]

    def test_loads_symbol_column(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "pool.csv"
        pd.DataFrame({"symbol": ["000001.SZ"]}).to_csv(
            csv_path, index=False, encoding="utf-8-sig",
        )
        assert load_stock_pool_from_csv(str(csv_path)) == ["000001.SZ"]

    def test_missing_code_column_raises(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "pool.csv"
        pd.DataFrame({"name": ["x"]}).to_csv(csv_path, index=False, encoding="utf-8-sig")
        with pytest.raises(ValueError):
            load_stock_pool_from_csv(str(csv_path))


# ─────────────────────────────────────────────
# build_backtest_config
# ─────────────────────────────────────────────
def _valid_config() -> dict:
    """一个合法的回测配置字典（含回测时间段/资金/股票池）。"""
    return {
        "backtest": {
            "start_date": "2024-01-01",
            "end_date": "2024-06-30",
            "initial_capital": 1_000_000,
        },
        "strategy": {"name": "ma_cross", "params": {"fast": 5, "slow": 20}},
        "sim_broker": {"commission_rate": 0.0002, "slippage_value": 0.0002},
        "stock_pool": {"type": "custom", "codes": ["600519.SH", "000001.SZ"]},
    }


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "symbols": None, "stock_pool": None, "strategy": None,
        "start": None, "end": None, "capital": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestBuildBacktestConfig:
    def test_cli_symbols_take_priority(self, tmp_path: Path) -> None:
        config = build_backtest_config(
            _valid_config(), _args(symbols="000001.SZ, 600519.SH"), tmp_path,
        )
        assert config.symbols == ["000001.SZ", "600519.SH"]
        assert config.start_date == "2024-01-01"
        assert config.initial_capital == 1_000_000

    def test_stock_pool_file_priority(self, tmp_path: Path) -> None:
        pool_csv = tmp_path / "pool.csv"
        pd.DataFrame({"code": ["300750.SZ"]}).to_csv(
            pool_csv, index=False, encoding="utf-8-sig",
        )
        config = build_backtest_config(
            _valid_config(), _args(stock_pool=str(pool_csv)), tmp_path,
        )
        assert config.symbols == ["300750.SZ"]

    def test_config_codes_used_when_no_cli(self, tmp_path: Path) -> None:
        config = build_backtest_config(_valid_config(), _args(), tmp_path)
        assert config.symbols == ["600519.SH", "000001.SZ"]

    def test_exclude_prefixes_applied(self, tmp_path: Path) -> None:
        cfg = _valid_config()
        cfg["stock_pool"]["exclude_prefixes"] = ["300"]
        cfg["stock_pool"]["codes"] = ["600519.SH", "300750.SZ"]
        config = build_backtest_config(cfg, _args(), tmp_path)
        assert config.symbols == ["600519.SH"]

    def test_three_factor_raises(self, tmp_path: Path) -> None:
        cfg = _valid_config()
        cfg["stock_pool"]["type"] = "three_factor"
        with pytest.raises(ValueError, match="three_factor"):
            build_backtest_config(cfg, _args(), tmp_path)

    def test_no_symbols_raises(self, tmp_path: Path) -> None:
        cfg = _valid_config()
        cfg["stock_pool"]["codes"] = []
        with pytest.raises(ValueError, match="未指定股票池"):
            build_backtest_config(cfg, _args(), tmp_path)

    def test_commission_model_built(self, tmp_path: Path) -> None:
        config = build_backtest_config(_valid_config(), _args(), tmp_path)
        assert config.commission is not None
        assert config.slippage is not None


# ─────────────────────────────────────────────
# P0-03 卡本体收尾（#347）：config_builder 断链修复
# ─────────────────────────────────────────────
class TestP003ConfigWiring:
    """配置→行为断链修复断言。

    P0-03 卡原文：config_builder 从不给 enforce_limit/price_limit_pct/
    enable_t1/lot_size 赋值 → 恒类默认；YAML 旧键 enforce_limit_up_down 死键。
    #347 后：sim_broker 段新键真实透传 + slippage_mode 显式分支（无静默零滑点）。
    """

    def test_sim_broker_new_keys_flow_to_config(self, tmp_path: Path) -> None:
        cfg = _valid_config()
        cfg["sim_broker"].update({
            "enforce_limit": True,
            "price_limit_pct": 0.20,
            "enable_t1": False,
            "lot_size": 1000,
            "participation_rate": 0.5,
        })
        config = build_backtest_config(cfg, _args(), tmp_path)
        assert config.enforce_limit is True
        assert config.price_limit_pct == 0.20
        assert config.enable_t1 is False
        assert config.lot_size == 1000
        assert config.participation_rate == 0.5

    def test_slippage_mode_none_explicit(self, tmp_path: Path) -> None:
        cfg = _valid_config()
        cfg["sim_broker"].update({"slippage_mode": "none"})
        config = build_backtest_config(cfg, _args(), tmp_path)
        assert config.slippage.type == "none"

    def test_slippage_mode_unknown_raises(self, tmp_path: Path) -> None:
        """未知 slippage_mode 显式报错（消灭静默零滑点通道，与 P0-05 语义一致）。"""
        cfg = _valid_config()
        cfg["sim_broker"].update({"slippage_mode": "mystery"})
        with pytest.raises(ValueError, match="slippage_mode"):
            build_backtest_config(cfg, _args(), tmp_path)
