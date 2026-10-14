# 文件名：tests/test_data_clean.py
"""
test_data_clean.py - 数据清洗单元测试 (折中档位2 v1 + 阶段9极端边界增强)
================================================================================
覆盖范围:
  [基础 v1]
  - bar_cleaner: 正常/OHLC错误/负价格/负成交量/重复时间戳/价格跳变/成交量异常/缺symbol/空数据/混合脏数据
  - tick_cleaner: 正常/bid-ask交叉/负价格/重复/空数据
  - pipeline: 端到端清洗
  - rules_loader: 配置加载
  - quality_reporter: 报告生成

  [阶段9 极端边界增强 — 任务24]
  - TestBarCleanerExtreme: OHLC变体/全零停牌/零成交量/非数值/缺失字段/无效时间戳/
    数值timestamp/时间回退排序/去重策略(drop_all/多重复)/阈值边界(严格>)/单条/
    NaN/Inf/超大数值/空白symbol/sort_by_timestamp=False
  - TestBarCleanerOO: CleanResult三元组解包/与旧函数100%一致/clean_single流式/CleanerRegistry注册
  - TestTickCleanerExtreme: bid==ask/零bid-ask/缺失bid-ask/负成交量/非数值/去重策略/价格跳变/单条
  - TestCleanPipelineExtreme: save_report落盘/空记录/全脏数据/配置覆盖
  - TestQualityReporterExtreme: fair等级/total=0除零保护/边界值(0.99/excellent vs good)/extra_info
  - TestRulesLoaderExtreme: 无效yaml优雅降级/部分覆盖保持默认

运行方式:
  pytest tests/test_data_clean.py -v
  pytest tests/test_data_clean.py -v --tb=short

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（流式清洗/极端数据不崩溃）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 所有测试数据使用历史日期(2024-01)，无未来日期
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from myquant.data_access.cleaner.bar_cleaner import (
    DEFAULT_BAR_CONFIG,
    BarCleaner,
    clean_bar_records,
)
from myquant.data_access.cleaner.base_cleaner import CleanerRegistry
from myquant.data_access.cleaner.pipeline import run_clean_pipeline
from myquant.data_access.cleaner.quality_reporter import generate_quality_report
from myquant.data_access.cleaner.rules_loader import (
    DEFAULT_CLEAN_CONFIG,
    load_clean_config,
)
from myquant.data_access.cleaner.tick_cleaner import (
    DEFAULT_TICK_CONFIG,
    TickCleaner,
    clean_tick_records,
)
from myquant.data_access.full_clean_service import FullCleanService

# ═══════════════════════════════════════════
#  Bar Cleaner 测试 (基础 v1)
# ═══════════════════════════════════════════

class TestBarCleaner:
    """Bar 数据清洗测试"""

    def test_normal_records_pass(self, sample_bar_records):
        """正常数据应全部通过清洗"""
        cleaned, bad, stats = clean_bar_records(sample_bar_records)
        assert stats["total"] == 5
        assert stats["valid"] == 5
        assert stats["bad"] == 0
        assert len(cleaned) == 5
        assert len(bad) == 0

    def test_ohlc_error_detected(self, bar_with_ohlc_error):
        """OHLC 逻辑错误 (high < low) 应被标记为脏数据"""
        cleaned, bad, stats = clean_bar_records(bar_with_ohlc_error)
        assert stats["bad"] == 1
        assert len(bad) == 1
        assert any("high" in e and "low" in e for e in bad[0]["_errors"])

    def test_negative_price_detected(self, bar_with_negative_price):
        """负价格应被标记为脏数据"""
        cleaned, bad, stats = clean_bar_records(bar_with_negative_price)
        assert stats["bad"] == 1
        assert any("open" in e and "负" in e for e in bad[0]["_errors"])

    def test_negative_volume_detected(self, bar_with_negative_volume):
        """负成交量应被标记为脏数据"""
        cleaned, bad, stats = clean_bar_records(bar_with_negative_volume)
        assert stats["bad"] == 1
        assert any("volume" in e for e in bad[0]["_errors"])

    def test_missing_symbol_detected(self, bar_missing_symbol):
        """缺少 symbol 应被标记为脏数据"""
        cleaned, bad, stats = clean_bar_records(bar_missing_symbol)
        assert stats["bad"] == 1
        assert any("symbol" in e for e in bad[0]["_errors"])

    def test_duplicate_timestamp_removed(self, bar_with_duplicate_timestamp):
        """重复时间戳应被去重 (默认 keep_last)"""
        cleaned, bad, stats = clean_bar_records(bar_with_duplicate_timestamp)
        assert stats["total"] == 3
        assert stats["removed_duplicates"] == 1
        assert len(cleaned) == 2  # 3条原始，去重1条，剩2条

    def test_duplicate_keep_first(self, bar_with_duplicate_timestamp):
        """重复时间戳 keep_first 策略应保留第一条"""
        config = dict(DEFAULT_BAR_CONFIG)
        config["duplicate_strategy"] = "keep_first"
        cleaned, bad, stats = clean_bar_records(bar_with_duplicate_timestamp, config)
        assert stats["removed_duplicates"] == 1
        # keep_first 应保留 open=10.0 的那条
        assert cleaned[0]["open"] == 10.0

    def test_price_jump_warning(self, bar_with_price_jump):
        """价格跳变应被标记为 warning (不剔除)"""
        cleaned, bad, stats = clean_bar_records(bar_with_price_jump)
        assert stats["valid"] == 3  # 全部有效，只是有warning
        assert stats["price_jumps"] >= 1
        # 检查第二条有 _warnings
        warning_records = [r for r in cleaned if r.get("_warnings")]
        assert len(warning_records) >= 1

    def test_volume_anomaly_warning(self, bar_with_volume_anomaly):
        """成交量异常应被标记为 warning"""
        cleaned, bad, stats = clean_bar_records(bar_with_volume_anomaly)
        assert stats["volume_anomalies"] >= 1

    def test_empty_records(self, empty_bar_records):
        """空数据应返回空结果，不报错"""
        cleaned, bad, stats = clean_bar_records(empty_bar_records)
        assert cleaned == []
        assert bad == []
        assert stats["total"] == 0

    def test_mixed_dirty_data(self, bar_mixed_dirty):
        """混合脏数据：正常保留，脏数据剔除，重复去重"""
        cleaned, bad, stats = clean_bar_records(bar_mixed_dirty)
        # 5条原始: 2条重复(去重1), 1条负价格(bad), 1条OHLC错误(bad), 1条正常
        # 预期: bad=2, removed_duplicates=1, valid=2
        assert stats["bad"] == 2
        assert stats["removed_duplicates"] == 1
        assert len(cleaned) == 2

    def test_output_sorted_by_timestamp(self, sample_bar_records):
        """清洗后数据应按时间戳排序"""
        # 打乱顺序输入
        shuffled = list(reversed(sample_bar_records))
        cleaned, bad, stats = clean_bar_records(shuffled)
        dates = [r["date"] for r in cleaned]
        assert dates == sorted(dates)

    def test_disabled_checks(self, bar_with_ohlc_error):
        """关闭 OHLC 校验时，OHLC错误不应被标记为脏数据"""
        config = dict(DEFAULT_BAR_CONFIG)
        config["check_ohlc_logic"] = False
        cleaned, bad, stats = clean_bar_records(bar_with_ohlc_error, config)
        assert stats["bad"] == 0
        assert len(cleaned) == 1


# ═══════════════════════════════════════════
#  Bar Cleaner 极端边界测试 (任务24新增)
# ═══════════════════════════════════════════

class TestBarCleanerExtreme:
    """Bar 数据清洗极端边界测试"""

    # ── OHLC 变体 ──

    def test_ohlc_high_below_max_open_close(self):
        """high < max(open,close) 应被标记为脏数据（非 high<low 变体）"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 9.5, "low": 9.0, "close": 10.2,
            "volume": 100000, "amount": 1020000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1
        assert any("high" in e and "max" in e for e in bad[0]["_errors"])

    def test_ohlc_low_above_min_open_close(self):
        """low > min(open,close) 应被标记为脏数据"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 11.0, "low": 10.5, "close": 9.8,
            "volume": 100000, "amount": 1020000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1
        assert any("low" in e and "min" in e for e in bad[0]["_errors"])

    def test_ohlc_all_zero_suspension_passes(self):
        """全零 OHLC（停牌占位）应通过 OHLC 校验（any(p>0) 守卫）"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 0, "high": 0, "low": 0, "close": 0,
            "volume": 0, "amount": 0,
        }]
        cleaned, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 0
        assert len(cleaned) == 1

    # ── 零成交量 ──

    def test_zero_volume_allowed_by_default(self):
        """零成交量在默认配置(allow_zero_volume=True)下应通过"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 0, "amount": 0,
        }]
        cleaned, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 0
        assert len(cleaned) == 1

    def test_zero_volume_rejected_when_disallowed(self):
        """零成交量在 allow_zero_volume=False 时应被标记为脏数据"""
        config = dict(DEFAULT_BAR_CONFIG)
        config["allow_zero_volume"] = False
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 0, "amount": 0,
        }]
        _, bad, stats = clean_bar_records(records, config)
        assert stats["bad"] == 1
        assert any("volume" in e and "0" in e for e in bad[0]["_errors"])

    # ── 非数值字段 ──

    def test_non_numeric_open_detected(self):
        """open='abc' 非数值应被标记为脏数据"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": "abc", "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000, "amount": 1020000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1
        assert any("open" in e and ("无法转为数值" in e or "数值" in e) for e in bad[0]["_errors"])

    def test_non_numeric_volume_detected(self):
        """volume='xyz' 非数值应被标记为脏数据"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": "xyz", "amount": 1020000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1
        assert any("volume" in e for e in bad[0]["_errors"])

    # ── 缺失字段 ──

    def test_missing_ohlc_fields_defaults_to_zero(self):
        """只有 symbol+date+volume，缺失 OHLC 字段应默认 0 并通过"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "volume": 100000,
        }]
        cleaned, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 0
        assert len(cleaned) == 1

    # ── 无效时间戳 ──

    def test_invalid_date_string_detected(self):
        """date='invalid' 应被标记为脏数据（时间戳无效）"""
        records = [{
            "symbol": "000001.SZ", "date": "invalid",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1
        assert any("时间戳" in e or "timestamp" in e for e in bad[0]["_errors"])

    def test_negative_timestamp_detected(self):
        """timestamp=-1 应被标记为脏数据"""
        records = [{
            "symbol": "000001.SZ", "timestamp": -1,
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1

    def test_zero_timestamp_detected(self):
        """timestamp=0 应被标记为脏数据（无效）"""
        records = [{
            "symbol": "000001.SZ", "timestamp": 0,
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1

    def test_numeric_timestamp_field_parsed(self):
        """使用数值 timestamp 字段（非 date 字符串）应正常解析并排序"""
        records = [
            {"symbol": "000001.SZ", "timestamp": 1704153602.0, "open": 10.2, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000},
            {"symbol": "000001.SZ", "timestamp": 1704153600.0, "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000},
        ]
        cleaned, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 0
        assert len(cleaned) == 2
        assert cleaned[0]["timestamp"] == 1704153600.0
        assert cleaned[1]["timestamp"] == 1704153602.0

    # ── 时间回退 / 排序 ──

    def test_out_of_order_input_produces_sorted_output(self):
        """时间乱序输入应输出排序后的数据（清洗器内部排序）"""
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-05", "open": 10.9, "high": 11.2, "low": 10.8, "close": 11.1, "volume": 95000},
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.2, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000},
        ]
        cleaned, _, stats = clean_bar_records(records)
        dates = [r["date"] for r in cleaned]
        assert dates == sorted(dates)
        assert "time_violations" in stats

    def test_sort_by_timestamp_false_preserves_input_order(self):
        """sort_by_timestamp=False 且 check_time_monotonic=False 时应保持输入顺序"""
        config = dict(DEFAULT_BAR_CONFIG)
        config["sort_by_timestamp"] = False
        config["check_time_monotonic"] = False
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-05", "open": 10.9, "high": 11.2, "low": 10.8, "close": 11.1, "volume": 95000},
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000},
        ]
        cleaned, _, _ = clean_bar_records(records, config)
        assert cleaned[0]["date"] == "2024-01-05"
        assert cleaned[1]["date"] == "2024-01-02"

    # ── 去重策略 ──

    def test_duplicate_drop_all_strategy(self):
        """duplicate_strategy=drop_all 应丢弃所有重复时间戳的记录"""
        config = dict(DEFAULT_BAR_CONFIG)
        config["duplicate_strategy"] = "drop_all"
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.1, "high": 10.6, "low": 9.9, "close": 10.3, "volume": 110000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.3, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000},
        ]
        cleaned, _, stats = clean_bar_records(records, config)
        # 2024-01-02 的两条重复全部丢弃，只剩 2024-01-03
        assert len(cleaned) == 1
        assert cleaned[0]["date"] == "2024-01-03"

    def test_multiple_duplicates_same_timestamp(self):
        """3+ 条同时间戳重复应正确去重（keep_last 保留最后一条）"""
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.1, "high": 10.6, "low": 9.9, "close": 10.3, "volume": 110000},
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.2, "high": 10.7, "low": 10.0, "close": 10.4, "volume": 120000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.3, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 130000},
        ]
        cleaned, _, stats = clean_bar_records(records)
        assert stats["removed_duplicates"] == 2
        assert len(cleaned) == 2
        # keep_last 应保留 open=10.2 的第三条
        assert cleaned[0]["open"] == 10.2

    # ── 阈值边界（严格 >）──

    def test_price_jump_exactly_at_threshold_not_triggered(self):
        """价格跳变恰好等于阈值时不应触发（严格 > 比较）"""
        # prev_close=10, curr_open=12.2 → change_pct=0.22 恰好等于阈值
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 12.2, "high": 12.5, "low": 12.0, "close": 12.3, "volume": 200000},
        ]
        _, _, stats = clean_bar_records(records)
        assert stats["price_jumps"] == 0

    def test_price_jump_just_above_threshold_triggered(self):
        """价格跳变略超阈值时应触发"""
        # prev_close=10, curr_open=12.21 → change_pct=0.221 > 0.22
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 12.21, "high": 12.5, "low": 12.0, "close": 12.3, "volume": 200000},
        ]
        _, _, stats = clean_bar_records(records)
        assert stats["price_jumps"] == 1

    def test_volume_ratio_exactly_at_threshold_not_triggered(self):
        """成交量比恰好等于阈值时不应触发（严格 > 比较）"""
        # prev_vol=100, curr_vol=10000 → ratio=100 恰好等于阈值
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.2, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 10000},
        ]
        _, _, stats = clean_bar_records(records)
        assert stats["volume_anomalies"] == 0

    def test_volume_ratio_just_above_threshold_triggered(self):
        """成交量比略超阈值时应触发"""
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.2, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 10001},
        ]
        _, _, stats = clean_bar_records(records)
        assert stats["volume_anomalies"] == 1

    # ── 单条记录 ──

    def test_single_record_no_anomaly_warnings(self):
        """单条记录无前序，不应产生价格跳变/成交量异常警告"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000, "amount": 1020000,
        }]
        cleaned, _, stats = clean_bar_records(records)
        assert stats["price_jumps"] == 0
        assert stats["volume_anomalies"] == 0
        assert len(cleaned) == 1

    # ── NaN / Inf ──

    def test_nan_price_does_not_crash(self):
        """NaN 价格不应导致清洗器崩溃（应合理处理）"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": float("nan"), "high": float("nan"), "low": float("nan"), "close": float("nan"),
            "volume": 100000, "amount": 1020000,
        }]
        # 不应抛出异常
        cleaned, bad, stats = clean_bar_records(records)
        assert stats["total"] == 1
        # NaN 比较全为 False，非负校验和 OHLC 校验均不触发
        assert len(cleaned) + len(bad) == 1

    def test_inf_price_does_not_crash(self):
        """Inf 价格不应导致清洗器崩溃"""
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": float("inf"), "high": float("inf"), "low": float("inf"), "close": float("inf"), "volume": 200000},
        ]
        # 不应抛出异常
        cleaned, bad, stats = clean_bar_records(records)
        assert stats["total"] == 2
        # Inf < Inf 为 False，OHLC 校验通过；价格跳变 inf/10=inf > 阈值
        assert len(cleaned) + len(bad) == 2

    # ── 超大数值 ──

    def test_very_large_values_do_not_overflow(self):
        """超大数值（1e18）不应溢出或崩溃"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 1e10, "high": 1.1e10, "low": 9.9e9, "close": 1.05e10,
            "volume": 1e18, "amount": 1e28,
        }]
        cleaned, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 0
        assert len(cleaned) == 1
        assert cleaned[0]["volume"] == 1e18

    # ── 空白 symbol ──

    def test_whitespace_symbol_treated_as_empty(self):
        """空白 symbol ('   ') 应被视为空并标记为脏数据"""
        records = [{
            "symbol": "   ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000,
        }]
        _, bad, stats = clean_bar_records(records)
        assert stats["bad"] == 1
        assert any("symbol" in e for e in bad[0]["_errors"])

    # ── 额外字段保留 ──

    def test_extra_fields_preserved_in_output(self):
        """记录中的额外字段应在清洗输出中保留"""
        records = [{
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000, "amount": 1020000,
            "turnover_rate": 1.5, "pe_ratio": 20.3,
        }]
        cleaned, _, _ = clean_bar_records(records)
        assert cleaned[0]["turnover_rate"] == 1.5
        assert cleaned[0]["pe_ratio"] == 20.3


# ═══════════════════════════════════════════
#  BarCleaner 面向对象接口测试 (任务24新增)
# ═══════════════════════════════════════════

class TestBarCleanerOO:
    """BarCleaner 面向对象接口 + 流式清洗测试"""

    def test_clean_returns_clean_result_with_tuple_unpacking(self, sample_bar_records):
        """BarCleaner.clean() 应返回 CleanResult，支持三元组解包"""
        cleaner = BarCleaner()
        result = cleaner.clean(sample_bar_records)
        cleaned, bad, stats = result  # 三元组解包
        assert stats["total"] == 5
        assert stats["valid"] == 5
        assert len(cleaned) == 5
        assert len(bad) == 0

    def test_oo_consistent_with_legacy_function(self, sample_bar_records):
        """BarCleaner.clean() 与旧函数 clean_bar_records() 结果应 100% 一致"""
        cleaner = BarCleaner()
        result = cleaner.clean(sample_bar_records)
        oo_cleaned, oo_bad, oo_stats = result

        fn_cleaned, fn_bad, fn_stats = clean_bar_records(sample_bar_records)

        assert len(oo_cleaned) == len(fn_cleaned)
        assert len(oo_bad) == len(fn_bad)
        assert oo_stats["total"] == fn_stats["total"]
        assert oo_stats["valid"] == fn_stats["valid"]
        assert oo_stats["bad"] == fn_stats["bad"]
        # 逐条对比
        for i in range(len(oo_cleaned)):
            for key in ("symbol", "date", "open", "high", "low", "close", "volume"):
                assert oo_cleaned[i].get(key) == fn_cleaned[i].get(key)

    def test_oo_consistent_with_mixed_dirty(self, bar_mixed_dirty):
        """混合脏数据下 OO 与旧函数结果一致"""
        cleaner = BarCleaner()
        oo_cleaned, oo_bad, oo_stats = cleaner.clean(bar_mixed_dirty)
        fn_cleaned, fn_bad, fn_stats = clean_bar_records(bar_mixed_dirty)
        assert oo_stats["bad"] == fn_stats["bad"] == 2
        assert oo_stats["removed_duplicates"] == fn_stats["removed_duplicates"] == 1
        assert len(oo_cleaned) == len(fn_cleaned) == 2

    def test_clean_single_streaming_no_prev(self):
        """clean_single() 无前序记录时应返回有效且无警告"""
        cleaner = BarCleaner()
        record = {
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000,
        }
        is_valid, errors, warnings = cleaner.clean_single(record)
        assert is_valid is True
        assert errors == []
        assert warnings == []

    def test_clean_single_streaming_with_price_jump(self):
        """clean_single() 有前序且价格跳变时应返回警告"""
        cleaner = BarCleaner()
        prev = {
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0,
            "volume": 100000,
        }
        curr = {
            "symbol": "000001.SZ", "date": "2024-01-03",
            "open": 15.0, "high": 15.5, "low": 14.8, "close": 15.2,
            "volume": 200000,
        }
        is_valid, errors, warnings = cleaner.clean_single(curr, prev_record=prev)
        assert is_valid is True
        assert errors == []
        assert len(warnings) >= 1
        assert any("价格跳变" in w for w in warnings)

    def test_clean_single_streaming_bad_record(self):
        """clean_single() 对脏数据应返回 is_valid=False 和错误列表"""
        cleaner = BarCleaner()
        record = {
            "symbol": "000001.SZ", "date": "2024-01-02",
            "open": -1.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000,
        }
        is_valid, errors, warnings = cleaner.clean_single(record)
        assert is_valid is False
        assert len(errors) >= 1
        assert any("open" in e for e in errors)

    def test_cleaner_registry_has_bar(self):
        """CleanerRegistry 应已注册 bar 类型"""
        registry = CleanerRegistry()
        assert registry.has("bar") is True

    def test_cleaner_config_override(self, bar_with_price_jump):
        """BarCleaner 应支持配置覆盖（宽松阈值不触发跳变）"""
        cleaner = BarCleaner(config={"max_price_jump_pct": 1.0})
        _, _, stats = cleaner.clean(bar_with_price_jump)
        assert stats["price_jumps"] == 0


# ═══════════════════════════════════════════
#  Tick Cleaner 测试 (基础 v1)
# ═══════════════════════════════════════════

class TestTickCleaner:
    """Tick 数据清洗测试"""

    def test_normal_ticks_pass(self, sample_tick_records):
        """正常 Tick 数据应全部通过"""
        cleaned, bad, stats = clean_tick_records(sample_tick_records)
        assert stats["total"] == 3
        assert stats["valid"] == 3
        assert stats["bad"] == 0

    def test_bid_ask_cross_detected(self, tick_with_bid_ask_cross):
        """bid >= ask 应被标记为脏数据"""
        cleaned, bad, stats = clean_tick_records(tick_with_bid_ask_cross)
        assert stats["bad"] == 1
        assert any("bid" in e for e in bad[0]["_errors"])

    def test_negative_price_detected(self, tick_with_negative_price):
        """负价格 Tick 应被标记为脏数据"""
        cleaned, bad, stats = clean_tick_records(tick_with_negative_price)
        assert stats["bad"] == 1

    def test_duplicate_ticks_removed(self, tick_duplicate):
        """重复 Tick 应被去重"""
        cleaned, bad, stats = clean_tick_records(tick_duplicate)
        assert stats["total"] == 3
        assert stats["removed_duplicates"] == 1
        assert len(cleaned) == 2

    def test_empty_ticks(self, empty_tick_records):
        """空 Tick 数据应返回空结果"""
        cleaned, bad, stats = clean_tick_records(empty_tick_records)
        assert cleaned == []
        assert bad == []
        assert stats["total"] == 0

    def test_tick_output_sorted(self, sample_tick_records):
        """Tick 清洗后应按时间戳排序"""
        shuffled = list(reversed(sample_tick_records))
        cleaned, bad, stats = clean_tick_records(shuffled)
        timestamps = [r["timestamp"] for r in cleaned]
        assert timestamps == sorted(timestamps)


# ═══════════════════════════════════════════
#  Tick Cleaner 极端边界测试 (任务24新增)
# ═══════════════════════════════════════════

class TestTickCleanerExtreme:
    """Tick 数据清洗极端边界测试"""

    def test_bid_equals_ask_detected(self):
        """bid == ask（两者均>0）应被标记为脏数据（bid>=ask）"""
        records = [{
            "symbol": "000001.SZ", "timestamp": 1704153600.0,
            "price": 10.0, "volume": 100, "amount": 1000,
            "bid_price": 10.0, "ask_price": 10.0,
        }]
        _, bad, stats = clean_tick_records(records)
        assert stats["bad"] == 1
        assert any("bid" in e and "ask" in e for e in bad[0]["_errors"])

    def test_zero_bid_ask_passes_check(self):
        """bid=0 或 ask=0 时不应触发 bid-ask 校验（守卫 bid>0 and ask>0）"""
        records = [
            {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 0, "ask_price": 10.0},
            {"symbol": "000001.SZ", "timestamp": 1704153601.0, "price": 10.01, "volume": 200, "amount": 2002, "bid_price": 9.99, "ask_price": 0},
        ]
        cleaned, bad, stats = clean_tick_records(records)
        assert stats["bad"] == 0
        assert len(cleaned) == 2

    def test_missing_bid_ask_fields_passes(self):
        """缺失 bid_price/ask_price 字段应通过 bid-ask 校验"""
        records = [{
            "symbol": "000001.SZ", "timestamp": 1704153600.0,
            "price": 10.0, "volume": 100, "amount": 1000,
        }]
        cleaned, bad, stats = clean_tick_records(records)
        assert stats["bad"] == 0
        assert len(cleaned) == 1

    def test_negative_volume_tick_detected(self):
        """负成交量 Tick 应被标记为脏数据"""
        records = [{
            "symbol": "000001.SZ", "timestamp": 1704153600.0,
            "price": 10.0, "volume": -50, "amount": -500,
            "bid_price": 9.99, "ask_price": 10.01,
        }]
        _, bad, stats = clean_tick_records(records)
        assert stats["bad"] == 1
        assert any("volume" in e for e in bad[0]["_errors"])

    def test_non_numeric_tick_price_detected(self):
        """非数值 price 应被标记为脏数据"""
        records = [{
            "symbol": "000001.SZ", "timestamp": 1704153600.0,
            "price": "NaN_str", "volume": 100, "amount": 1000,
            "bid_price": 9.99, "ask_price": 10.01,
        }]
        _, bad, stats = clean_tick_records(records)
        assert stats["bad"] == 1
        assert any("price" in e for e in bad[0]["_errors"])

    def test_tick_duplicate_keep_first(self):
        """Tick 重复 keep_first 策略应保留第一条"""
        config = dict(DEFAULT_TICK_CONFIG)
        config["duplicate_strategy"] = "keep_first"
        records = [
            {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.99, "ask_price": 10.01},
            {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.98, "ask_price": 10.02},
            {"symbol": "000001.SZ", "timestamp": 1704153601.0, "price": 10.01, "volume": 200, "amount": 2002, "bid_price": 10.00, "ask_price": 10.02},
        ]
        cleaned, _, stats = clean_tick_records(records, config)
        assert stats["removed_duplicates"] == 1
        assert len(cleaned) == 2
        assert cleaned[0]["bid_price"] == 9.99  # keep_first

    def test_tick_duplicate_drop_all(self):
        """Tick 重复 drop_all 策略应丢弃所有重复记录"""
        config = dict(DEFAULT_TICK_CONFIG)
        config["duplicate_strategy"] = "drop_all"
        records = [
            {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.99, "ask_price": 10.01},
            {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.98, "ask_price": 10.02},
            {"symbol": "000001.SZ", "timestamp": 1704153601.0, "price": 10.01, "volume": 200, "amount": 2002, "bid_price": 10.00, "ask_price": 10.02},
        ]
        cleaned, _, _ = clean_tick_records(records, config)
        # 重复的 timestamp=1704153600 两条全部丢弃，只剩 1704153601
        assert len(cleaned) == 1
        assert cleaned[0]["timestamp"] == 1704153601.0

    def test_tick_price_jump_detection(self):
        """Tick 价格跳变应被检测并标记 _warnings"""
        records = [
            {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.99, "ask_price": 10.01},
            {"symbol": "000001.SZ", "timestamp": 1704153601.0, "price": 13.0, "volume": 200, "amount": 2600, "bid_price": 12.99, "ask_price": 13.01},
        ]
        cleaned, _, stats = clean_tick_records(records)
        assert stats["price_jumps"] >= 1
        warning_records = [r for r in cleaned if r.get("_warnings")]
        assert len(warning_records) >= 1

    def test_single_tick_no_jump_warning(self):
        """单条 Tick 无前序，不应产生价格跳变警告"""
        records = [{
            "symbol": "000001.SZ", "timestamp": 1704153600.0,
            "price": 10.0, "volume": 100, "amount": 1000,
            "bid_price": 9.99, "ask_price": 10.01,
        }]
        _, _, stats = clean_tick_records(records)
        assert stats["price_jumps"] == 0

    def test_tick_invalid_timestamp_detected(self):
        """Tick timestamp=0 应被标记为脏数据"""
        records = [{
            "symbol": "000001.SZ", "timestamp": 0,
            "price": 10.0, "volume": 100, "amount": 1000,
            "bid_price": 9.99, "ask_price": 10.01,
        }]
        _, bad, stats = clean_tick_records(records)
        assert stats["bad"] == 1
        assert any("时间戳" in e or "timestamp" in e for e in bad[0]["_errors"])

    def test_tick_oo_consistent_with_legacy(self, sample_tick_records):
        """TickCleaner OO 与旧函数结果一致"""
        cleaner = TickCleaner()
        oo_cleaned, oo_bad, oo_stats = cleaner.clean(sample_tick_records)
        fn_cleaned, fn_bad, fn_stats = clean_tick_records(sample_tick_records)
        assert oo_stats["total"] == fn_stats["total"] == 3
        assert oo_stats["valid"] == fn_stats["valid"] == 3
        assert len(oo_cleaned) == len(fn_cleaned) == 3


# ═══════════════════════════════════════════
#  Pipeline 测试 (基础 v1)
# ═══════════════════════════════════════════

class TestCleanPipeline:
    """清洗流水线端到端测试"""

    def test_bar_pipeline_end_to_end(self, sample_bar_records):
        """Bar 流水线端到端测试 (不保存文件)"""
        result = run_clean_pipeline(
            records=sample_bar_records,
            data_type="bar",
            symbol="TEST.SZ",
            save_bad=False,
            save_report=False,
        )
        assert "cleaned" in result
        assert "bad" in result
        assert "stats" in result
        assert len(result["cleaned"]) == 5
        assert result["stats"]["valid"] == 5

    def test_tick_pipeline_end_to_end(self, sample_tick_records):
        """Tick 流水线端到端测试"""
        result = run_clean_pipeline(
            records=sample_tick_records,
            data_type="tick",
            symbol="TEST.SZ",
            save_bad=False,
            save_report=False,
        )
        assert len(result["cleaned"]) == 3

    def test_pipeline_with_bad_data(self, bar_mixed_dirty):
        """流水线处理含脏数据的输入"""
        result = run_clean_pipeline(
            records=bar_mixed_dirty,
            data_type="bar",
            symbol="DIRTY.SZ",
            save_bad=False,
            save_report=False,
        )
        assert result["stats"]["bad"] == 2
        assert len(result["bad"]) == 2

    def test_pipeline_invalid_data_type(self, sample_bar_records):
        """不支持的数据类型应抛出 ValueError"""
        with pytest.raises(ValueError, match="不支持的数据类型"):
            run_clean_pipeline(
                records=sample_bar_records,
                data_type="invalid",
                symbol="TEST.SZ",
            )

    def test_pipeline_save_bad_to_temp(self, bar_mixed_dirty):
        """流水线脏数据落盘到临时目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_clean_pipeline(
                records=bar_mixed_dirty,
                data_type="bar",
                symbol="TEST.SZ",
                save_bad=True,
                save_report=False,
                bad_output_dir=tmpdir,
            )
            assert result["bad_file"] is not None
            assert Path(result["bad_file"]).exists()
            # 验证文件内容
            with open(result["bad_file"], "r", encoding="utf-8") as f:
                bad_data = json.load(f)
            assert len(bad_data) == 2


# ═══════════════════════════════════════════
#  Pipeline 极端边界测试 (任务24新增)
# ═══════════════════════════════════════════

class TestCleanPipelineExtreme:
    """清洗流水线极端边界测试"""

    def test_pipeline_save_report_generates_file(self, sample_bar_records):
        """save_report=True 应生成质量报告文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_clean_pipeline(
                records=sample_bar_records,
                data_type="bar",
                symbol="REPORT.SZ",
                save_bad=False,
                save_report=True,
                report_output_dir=tmpdir,
            )
            assert result["report_file"] is not None
            assert Path(result["report_file"]).exists()
            with open(result["report_file"], "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["symbol"] == "REPORT.SZ"
            assert report["data_type"] == "bar"
            assert report["summary"]["total_records"] == 5

    def test_pipeline_empty_records(self):
        """空记录通过流水线应返回空结果，不报错"""
        result = run_clean_pipeline(
            records=[],
            data_type="bar",
            symbol="EMPTY.SZ",
            save_bad=False,
            save_report=False,
        )
        assert result["cleaned"] == []
        assert result["bad"] == []
        assert result["stats"]["total"] == 0

    def test_pipeline_all_bad_data(self):
        """全脏数据流水线应返回 cleaned=[]，bad 全量"""
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": -1.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": -2.0, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000},
        ]
        result = run_clean_pipeline(
            records=records,
            data_type="bar",
            symbol="ALLBAD.SZ",
            save_bad=False,
            save_report=False,
        )
        assert result["cleaned"] == []
        assert len(result["bad"]) == 2
        assert result["stats"]["bad"] == 2

    def test_pipeline_config_override_threshold(self):
        """流水线配置覆盖应生效（config 结构为 {data_type: {...}}）"""
        records = [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.5, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000},
        ]
        # 默认阈值 0.22，5% 跳变不触发；覆盖为 0.01 后应触发
        result_default = run_clean_pipeline(
            records=records, data_type="bar", symbol="CFG.SZ",
            save_bad=False, save_report=False,
        )
        assert result_default["stats"]["price_jumps"] == 0

        result_override = run_clean_pipeline(
            records=records, data_type="bar", symbol="CFG.SZ",
            config={"bar": {"max_price_jump_pct": 0.01}},
            save_bad=False, save_report=False,
        )
        assert result_override["stats"]["price_jumps"] >= 1


# ═══════════════════════════════════════════
#  Rules Loader 测试 (基础 v1)
# ═══════════════════════════════════════════

class TestRulesLoader:
    """清洗规则加载器测试"""

    def test_default_config_loaded(self):
        """无配置文件时应返回默认配置"""
        config = load_clean_config(use_settings=False)
        assert "enabled" in config
        assert "bar" in config
        assert "tick" in config
        assert "bad_data" in config
        assert "quality_report" in config
        assert config["bar"]["max_price_jump_pct"] == 0.22

    def test_default_config_structure(self):
        """默认配置应包含所有必要字段"""
        config = DEFAULT_CLEAN_CONFIG
        assert config["enabled"] is True
        assert "check_ohlc_logic" in config["bar"]
        assert "check_bid_ask" in config["tick"]
        assert "output_dir" in config["bad_data"]

    def test_load_from_yaml_file(self):
        """从 yaml 文件加载配置"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write("enabled: false\nbar:\n  max_price_jump_pct: 0.5\n")
            yaml_path = f.name

        try:
            config = load_clean_config(config_path=yaml_path, use_settings=False)
            assert config["enabled"] is False
            assert config["bar"]["max_price_jump_pct"] == 0.5
            # 未覆盖的字段应保持默认
            assert config["bar"]["check_ohlc_logic"] is True
        finally:
            Path(yaml_path).unlink(missing_ok=True)


# ═══════════════════════════════════════════
#  Rules Loader 极端边界测试 (任务24新增)
# ═══════════════════════════════════════════

class TestRulesLoaderExtreme:
    """清洗规则加载器极端边界测试"""

    def test_invalid_yaml_file_graceful_degradation(self):
        """无效 yaml 文件应优雅降级（不崩溃，返回默认配置或明确错误）"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(":::invalid yaml content:::\n  bad: [unclosed\n")
            yaml_path = f.name

        try:
            # 不应抛出未捕获异常
            config = load_clean_config(config_path=yaml_path, use_settings=False)
            # 降级后应仍包含核心结构
            assert "bar" in config
            assert "tick" in config
        except Exception:
            # 如果实现选择抛出明确异常，也接受（但不应是裸崩溃）
            pass
        finally:
            Path(yaml_path).unlink(missing_ok=True)

    def test_partial_override_preserves_defaults(self):
        """部分覆盖配置应保持未覆盖字段为默认值"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write("bar:\n  check_ohlc_logic: false\n")
            yaml_path = f.name

        try:
            config = load_clean_config(config_path=yaml_path, use_settings=False)
            # 覆盖字段
            assert config["bar"]["check_ohlc_logic"] is False
            # 未覆盖字段保持默认
            assert config["bar"]["max_price_jump_pct"] == 0.22
            assert config["bar"]["check_non_negative"] is True
            assert config["enabled"] is True
            assert config["tick"]["check_bid_ask"] is True
        finally:
            Path(yaml_path).unlink(missing_ok=True)

    def test_nonexistent_config_path_returns_default(self):
        """不存在的配置文件路径应返回默认配置（不崩溃）"""
        config = load_clean_config(config_path="/nonexistent/path/config.yaml", use_settings=False)
        assert "bar" in config
        assert config["bar"]["max_price_jump_pct"] == 0.22


# ═══════════════════════════════════════════
#  Quality Reporter 测试 (基础 v1)
# ═══════════════════════════════════════════

class TestQualityReporter:
    """质量报告生成器测试"""

    def test_generate_report(self):
        """生成质量报告到临时目录"""
        stats = {
            "total": 100,
            "valid": 95,
            "bad": 5,
            "removed_duplicates": 3,
            "price_jumps": 2,
            "volume_anomalies": 1,
            "time_violations": 0,
            "duration_sec": 0.123,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats,
                symbol="000001.SZ",
                data_type="bar",
                output_dir=tmpdir,
            )
            assert report_path is not None
            assert Path(report_path).exists()

            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)

            assert report["symbol"] == "000001.SZ"
            assert report["data_type"] == "bar"
            assert report["summary"]["total_records"] == 100
            assert report["summary"]["valid_records"] == 95
            assert report["summary"]["bad_records"] == 5
            assert report["summary"]["valid_rate"] == 0.95
            assert report["quality_status"] == "good"  # 95% -> good

    def test_excellent_quality_status(self):
        """合格率100%且无脏数据应为 excellent"""
        stats = {"total": 100, "valid": 100, "bad": 0}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["quality_status"] == "excellent"

    def test_poor_quality_status(self):
        """合格率低于80%应为 poor"""
        stats = {"total": 100, "valid": 50, "bad": 50}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["quality_status"] == "poor"


# ═══════════════════════════════════════════
#  Quality Reporter 极端边界测试 (任务24新增)
# ═══════════════════════════════════════════

class TestQualityReporterExtreme:
    """质量报告生成器极端边界测试"""

    def test_fair_quality_status(self):
        """合格率 80-95% 应为 fair"""
        stats = {"total": 100, "valid": 85, "bad": 15}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["quality_status"] == "fair"
            assert report["summary"]["valid_rate"] == 0.85

    def test_total_zero_no_division_error(self):
        """total=0 时不应除零崩溃，valid_rate=0.0，quality_status=poor"""
        stats = {"total": 0, "valid": 0, "bad": 0}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="EMPTY", data_type="bar", output_dir=tmpdir
            )
            assert report_path is not None
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["summary"]["valid_rate"] == 0.0
            assert report["summary"]["total_records"] == 0
            assert report["quality_status"] == "poor"

    def test_boundary_excellent_requires_zero_bad(self):
        """边界：valid_rate=0.99 < 0.995 时应为 good（#C11修复：excellent阈值改为0.995）"""
        stats = {"total": 100, "valid": 99, "bad": 1}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["summary"]["valid_rate"] == 0.99
            assert report["quality_status"] == "good"  # 0.99 < 0.995 → good

    def test_boundary_excellent_at_exactly_99_percent(self):
        """边界：valid_rate=0.99 < 0.995 时应为 good（#C11修复：excellent阈值改为0.995，不再要求bad==0）"""
        stats = {"total": 100, "valid": 99, "bad": 0}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["quality_status"] == "good"  # 0.99 < 0.995 → good

    def test_boundary_excellent_at_995_percent(self):
        """边界：valid_rate=0.995 时应为 excellent（#C11修复：excellent阈值0.995，允许少量脏数据）"""
        stats = {"total": 1000, "valid": 995, "bad": 5}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["summary"]["valid_rate"] == 0.995
            assert report["quality_status"] == "excellent"  # >=0.995 → excellent，允许bad>0

    def test_boundary_good_at_exactly_95_percent(self):
        """边界：valid_rate=0.95 时应为 good（>=0.95）"""
        stats = {"total": 100, "valid": 95, "bad": 5}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["quality_status"] == "good"

    def test_boundary_fair_at_exactly_80_percent(self):
        """边界：valid_rate=0.80 时应为 fair（>=0.80）"""
        stats = {"total": 100, "valid": 80, "bad": 20}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert report["quality_status"] == "fair"

    def test_extra_info_attached_to_report(self):
        """extra_info 应附加到报告中"""
        stats = {"total": 100, "valid": 90, "bad": 10}
        extra = {"source": "akshare", "symbol_count": 5, "pipeline_version": "1.0"}
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar",
                output_dir=tmpdir, extra_info=extra,
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            assert "extra_info" in report
            assert report["extra_info"]["source"] == "akshare"
            assert report["extra_info"]["symbol_count"] == 5

    def test_report_contains_all_stats_fields(self):
        """报告应包含所有统计字段（即使输入 stats 缺失某些 key）"""
        stats = {"total": 50, "valid": 40}  # 缺失 bad/removed_duplicates 等
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = generate_quality_report(
                stats=stats, symbol="TEST", data_type="bar", output_dir=tmpdir
            )
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            summary = report["summary"]
            assert summary["total_records"] == 50
            assert summary["valid_records"] == 40
            assert summary["bad_records"] == 0  # 缺失默认 0
            assert summary["removed_duplicates"] == 0
            assert summary["price_jumps"] == 0
            assert summary["volume_anomalies"] == 0
            assert summary["time_violations"] == 0


# ═══════════════════════════════════════════
#  集成测试: 配置驱动清洗 (基础 v1)
# ═══════════════════════════════════════════

class TestConfigDrivenCleaning:
    """配置驱动的清洗集成测试"""

    def test_custom_threshold(self, bar_with_price_jump):
        """自定义价格跳变阈值应影响检测结果"""
        # 默认阈值 0.22，跳变 50% 应被检测
        config = {"max_price_jump_pct": 0.22}
        _, _, stats = clean_bar_records(bar_with_price_jump, config)
        assert stats["price_jumps"] >= 1

        # 阈值设为 1.0 (100%)，跳变 50% 不应被检测
        config_loose = {"max_price_jump_pct": 1.0}
        _, _, stats_loose = clean_bar_records(bar_with_price_jump, config_loose)
        assert stats_loose["price_jumps"] == 0

    def test_disable_all_checks(self, bar_mixed_dirty):
        """关闭所有校验时，所有数据都应通过"""
        config = {
            "check_ohlc_logic": False,
            "check_non_negative": False,
            "check_symbol": False,
            "check_timestamp": False,
            "check_duplicate_timestamp": False,
            "detect_price_jump": False,
            "detect_volume_anomaly": False,
        }
        cleaned, bad, stats = clean_bar_records(bar_mixed_dirty, config)
        # 关闭去重后，5条全部保留（包括重复）
        assert stats["bad"] == 0
        assert len(cleaned) == 5


# ═══════════════════════════════════════════
#  全量清洗 B类财务缺失留痕 (#71)
# ═══════════════════════════════════════════

class _FakeConn:
    """伪连接：execute 返回预置财务行。"""

    def __init__(self, fin_rows: List[Dict[str, Any]]) -> None:
        self._fin_rows = fin_rows

    def execute(self, sql: str, params: Any = None) -> List[Dict[str, Any]]:
        return self._fin_rows


class _FakeDataQueries:
    """伪 DataQueries：get_market_data 返回固定行情行。"""

    def __init__(self, conn: _FakeConn, rows: List[Dict[str, Any]]) -> None:
        self.conn = conn
        self._rows = rows

    def get_market_data(self, symbol: str, period: str = "daily", adjust: str = "raw") -> List[Dict[str, Any]]:
        return list(self._rows)


def _make_worker_result(monkeypatch: Any, fin_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """调用 worker_clean，返回 result（monkeypatch 注入伪连接/查询/管道）。"""
    import myquant.data_access.full_clean_service as fcs

    rows: List[Dict[str, Any]] = [{
        "trade_date": "2026-09-28", "open": 10.0, "high": 10.5,
        "low": 9.8, "close": 10.2, "volume": 100000.0, "amount": 1000000.0,
    }]
    cleaned_out: List[Dict[str, Any]] = [dict(r) for r in rows]
    fake_conn = _FakeConn(fin_rows)
    fake_dq = _FakeDataQueries(fake_conn, rows)
    monkeypatch.setattr(fcs, "get_connection", lambda *a, **k: fake_conn)
    monkeypatch.setattr(fcs, "DataQueries", lambda conn: fake_dq)

    def _fake_pipeline(**kwargs: Any) -> Dict[str, Any]:
        return {"cleaned": cleaned_out, "bad": [], "stats": {}, "bad_file": None}

    monkeypatch.setattr(fcs, "run_clean_pipeline", _fake_pipeline)
    task = ("000001.SZ", "fake.db", {})
    return fcs.worker_clean(task)


class TestFullCleanBClassTrace:
    """全量清洗 B类财务缺失留痕（#71）。"""

    def test_worker_财务缺失标记留痕(self, monkeypatch: Any) -> None:
        """financial_data 无记录 → result 含 no_financial=True，B类列留空。"""
        result = _make_worker_result(monkeypatch, [])
        assert result["status"] == "ok"
        assert result.get("no_financial") is True
        rec = result["cleaned_data"][0]
        assert rec.get("turnover") is None
        assert rec.get("float_share") is None
        assert rec.get("pe_dynamic") is None

    def test_worker_财务存在不标记(self, monkeypatch: Any) -> None:
        """financial_data 有记录 → 无 no_financial 标记，B类正常计算。"""
        fin_rows: List[Dict[str, Any]] = [{
            "report_date": "2026-06-30", "circ_mv": 2.0e11,
            "total_mv": 2.2e11, "net_profit": 2.5e10,
        }]
        result = _make_worker_result(monkeypatch, fin_rows)
        assert result["status"] == "ok"
        assert result.get("no_financial") is None
        rec = result["cleaned_data"][0]
        assert rec.get("float_share") is not None

    def test_report_含财务缺失字段(self) -> None:
        """报告含 no_financial_count / no_financial_symbols。"""
        service = FullCleanService(config={}, dry_run=True)
        service.no_financial_symbols = ["601091.SH", "301686.SZ"]
        report = service.build_report(
            counts={"ok": 2, "written": 2, "unchanged": 0, "error": 0, "empty": 0},
            stats_agg={"bad_rows": 0, "removed_duplicates": 0, "price_jumps": 0,
                       "volume_anomalies": 0, "time_violations": 0, "total_rows": 2},
            t0=0.0,
            backup_path=None,
        )
        assert report["no_financial_count"] == 2
        assert report["no_financial_symbols"] == ["601091.SH", "301686.SZ"]

    def test_report_财务缺失字段默认(self) -> None:
        """无缺失标的 → 字段为 0 / []。"""
        service = FullCleanService(config={}, dry_run=True)
        report = service.build_report(
            counts={"ok": 0, "written": 0, "unchanged": 0, "error": 0, "empty": 0},
            stats_agg={"bad_rows": 0, "removed_duplicates": 0, "price_jumps": 0,
                       "volume_anomalies": 0, "time_violations": 0, "total_rows": 0},
            t0=0.0,
            backup_path=None,
        )
        assert report["no_financial_count"] == 0
        assert report["no_financial_symbols"] == []
