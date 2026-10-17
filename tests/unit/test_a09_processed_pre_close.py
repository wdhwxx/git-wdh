# 文件名：tests/unit/test_a09_processed_pre_close.py
"""A-09 processed 前收盘价两写入方 schema 一致单测（方案二）。

背景：A-09 方案一（模板补「前收盘价」列）被 R-24 超大文件白名单冻结硬顶
（manager.py 2204 行基线只减不增、无对价通道）→ 改走报告备选方案二：
processed 统一不含「前收盘价」派生列，由读取层兜底派生
（provider.py:797-799 / mixed_engine.py:1120-1123，收盘价.shift(1) 同口径）。

验收：
  1. 两写入方 schema 一致=均无「前收盘价」列（增量模板 == 全量导出列集合）；
  2. 读取层兜底派生存在（消费侧 vectorized_engine fail-closed 不误拒）；
  3. 防回归：导出脚本不得重新写回派生列。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, List

import pytest

from myquant.data_access.manager import PROCESSED_COLUMN_TEMPLATE

_PROJECT = Path(__file__).resolve().parents[2]
_EXPORT_SCRIPT = _PROJECT / "scripts" / "数据导出_DB到processed.py"
_PROVIDER_SCRIPT = _PROJECT / "myquant" / "data_access" / "provider.py"
_MIXED_ENGINE_SCRIPT = _PROJECT / "myquant" / "backtest" / "mixed_engine.py"

#: 导出列集合（不含前收盘价；与 test_data_export_columns.EXPECTED_CN_COLUMNS 同步）
_EXPORT_CN_COLUMNS: List[str] = [
    "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额",
    "涨跌幅", "振幅", "量比", "换手率", "流通股本", "市盈动", "每股收益",
    "available_time",
]


def _load_export_module() -> Any:
    """importlib 加载导出脚本（薄入口模块，与 test_data_export_columns 同模式）。"""
    spec = importlib.util.spec_from_file_location("export_processed_a09", _EXPORT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════
# 1. 两写入方 schema 一致（核心验收）
# ═══════════════════════════════════════════


def test_manager_template_has_no_pre_close() -> None:
    """增量写入模板不含「前收盘价」（方案二：processed 统一无派生列）。"""
    assert "前收盘价" not in PROCESSED_COLUMN_TEMPLATE


def test_export_columns_match_manager_template() -> None:
    """全量导出列集合 == 增量模板列集合（两写入方 schema 一致=核心验收）。"""
    mod = _load_export_module()
    # 导出映射不含 pre_close（无前收盘价派生）
    assert "pre_close" not in mod.CN_COLUMN_MAP
    # 导出列集合（日期列 + 业务列 + available_time）与增量模板完全一致
    assert ["日期"] + _EXPORT_CN_COLUMNS == PROCESSED_COLUMN_TEMPLATE


# ═══════════════════════════════════════════
# 2. 读取层兜底派生存在（消费侧保障）
# ═══════════════════════════════════════════


def test_provider_has_fallback_derivation() -> None:
    """provider 读取路径含前收盘价兜底派生（收盘价.shift(1)，消费侧 fail-closed 不误拒）。"""
    src = _PROVIDER_SCRIPT.read_text(encoding="utf-8")
    assert "前收盘价" in src
    assert ".shift(1)" in src
    # 兜底条件：缺前收盘价列且含收盘价列时派生
    assert '"前收盘价" not in df.columns' in src
    assert '"收盘价" in df.columns' in src


def test_mixed_engine_has_fallback_derivation() -> None:
    """混合引擎读取路径含前收盘价兜底派生（与 provider 同口径）。"""
    src = _MIXED_ENGINE_SCRIPT.read_text(encoding="utf-8")
    assert '"前收盘价" not in df.columns' in src
    assert '.shift(1)' in src


# ═══════════════════════════════════════════
# 3. 防回归：导出脚本不得写回派生列
# ═══════════════════════════════════════════


def test_export_script_does_not_derive_pre_close() -> None:
    """导出脚本 load_symbol_data 内无前收盘价派生代码（防回归写回破坏一致）。"""
    src = _EXPORT_SCRIPT.read_text(encoding="utf-8")
    body = src.split("def load_symbol_data")[1].split("def verify_parquet")[0]
    # 派生代码特征（df.insert 派生列 / _pre_close 变量）不得回归；注释提及方案说明不算
    assert "df.insert" not in body
    assert "_pre_close" not in body
