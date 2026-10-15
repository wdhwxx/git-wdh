# 文件名：tests/unit/test_light_lookahead_guard.py
"""
P3-2（v1.2 §6.4 ROI 2）：core/light_lookahead_guard.py 覆盖率补测（34.3%→70%）

覆盖范围：
  1. _to_seconds：int/float/datetime/date/ISO 字符串/None/bool(ValueError)/空串/非法串/类型
  2. is_future_data / is_lookahead_execution（含 allow_same_day / 未知时刻）
  3. LightLookaheadGuard 构造与属性（显式 vs 环境变量 / setter）
  4. on_bar_close：推进 / 时钟倒退违规 / 禁用 / None
  5. check_data_access：合法 / 未来 / 时间线未推进 / 禁用
  6. check_indicator_window：合法 / 含当日 / 未来数据 / 未来时刻计算 / 禁用
  7. check_signal_execution：T+1 合法 / 同日 / 早于信号 / allow_same_day / 禁用
  8. assert_pass：通过 / 违规抛 LookaheadError（details 汇总）
  9. report / reset / stats / violations 副本 / __repr__
  10. strict 模式立即抛 / 全局单例 get/reset
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from myquant.core.exceptions import LookaheadError
from myquant.core.light_lookahead_guard import (
    ENV_ENABLED,
    ENV_STRICT,
    RULE_CLOCK_REGRESS,
    RULE_FUTURE_DATA,
    RULE_INDICATOR_WINDOW,
    RULE_SIGNAL_EXECUTION,
    LightLookaheadGuard,
    LightLookaheadViolation,
    _to_seconds,
    get_light_lookahead_guard,
    is_future_data,
    is_lookahead_execution,
    reset_light_lookahead_guard,
)

# ─────────────────────────────────────────────
# _to_seconds
# ─────────────────────────────────────────────

class TestToSeconds:
    def test_none(self) -> None:
        assert _to_seconds(None) is None

    def test_float_passthrough(self) -> None:
        assert _to_seconds(1705305600.5) == 1705305600.5

    def test_int_to_float(self) -> None:
        assert _to_seconds(1705305600) == 1705305600.0

    def test_datetime(self) -> None:
        d = datetime(2024, 1, 16, 10, 0)
        assert _to_seconds(d) == d.timestamp()

    def test_date(self) -> None:
        d = date(2024, 1, 16)
        assert _to_seconds(d) == datetime(2024, 1, 16).timestamp()

    def test_iso_datetime_str(self) -> None:
        assert _to_seconds("2024-01-16 10:00:00") == datetime(2024, 1, 16, 10, 0).timestamp()

    def test_iso_date_str(self) -> None:
        assert _to_seconds("2024-01-16") == datetime(2024, 1, 16).timestamp()

    def test_bool_raises(self) -> None:
        with pytest.raises(ValueError, match="不支持布尔值"):
            _to_seconds(True)

    def test_empty_str_raises(self) -> None:
        with pytest.raises(ValueError, match="时间字符串为空"):
            _to_seconds("   ")

    def test_invalid_str_raises(self) -> None:
        with pytest.raises(ValueError, match="无法解析时间字符串"):
            _to_seconds("not-a-date")

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError, match="不支持的时间类型"):
            _to_seconds([1, 2])  # type: ignore[arg-type]


# ─────────────────────────────────────────────
# 模块级判定函数
# ─────────────────────────────────────────────

class TestIsFutureData:
    def test_future(self) -> None:
        assert is_future_data("2024-01-16", "2024-01-15") is True

    def test_same_day_not_future(self) -> None:
        assert is_future_data("2024-01-15", "2024-01-15") is False

    def test_past_not_future(self) -> None:
        assert is_future_data("2024-01-14", "2024-01-15") is False

    def test_unknown_ts_false(self) -> None:
        assert is_future_data(None, "2024-01-15") is False
        assert is_future_data("2024-01-15", None) is False

    def test_unix_and_datetime_mixed(self) -> None:
        assert is_future_data(datetime(2024, 1, 16), 1705219200.0) is True


class TestIsLookaheadExecution:
    def test_exec_before_signal(self) -> None:
        assert is_lookahead_execution("2024-01-16", "2024-01-15") is True

    def test_same_day_default_lookahead(self) -> None:
        assert is_lookahead_execution("2024-01-15", "2024-01-15") is True

    def test_same_day_allowed(self) -> None:
        assert is_lookahead_execution("2024-01-15", "2024-01-15", allow_same_day=True) is False

    def test_t_plus_one_ok(self) -> None:
        assert is_lookahead_execution("2024-01-15", "2024-01-16") is False

    def test_unknown_signal_false(self) -> None:
        assert is_lookahead_execution(None, "2024-01-15") is False

    def test_unknown_exec_false(self) -> None:
        assert is_lookahead_execution("2024-01-15", None) is False


# ─────────────────────────────────────────────
# LightLookaheadViolation
# ─────────────────────────────────────────────

class TestViolation:
    def test_to_dict(self) -> None:
        v = LightLookaheadViolation(rule="r", message="m", symbol="s", ts=1.0, source="src")
        d = v.to_dict()
        assert d["rule"] == "r" and d["message"] == "m"
        assert d["symbol"] == "s" and d["ts"] == 1.0 and d["source"] == "src"

    def test_str(self) -> None:
        v = LightLookaheadViolation(rule="r", message="m")
        assert str(v) == "[r] m"


# ─────────────────────────────────────────────
# 构造与属性
# ─────────────────────────────────────────────

class TestConstructor:
    def test_explicit_enabled_strict(self) -> None:
        g = LightLookaheadGuard(enabled=True, strict=False)
        assert g.enabled is True and g.strict is False

    def test_env_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_ENABLED, raising=False)
        monkeypatch.delenv(ENV_STRICT, raising=False)
        g = LightLookaheadGuard()
        assert g.enabled is True  # 默认开启
        assert g.strict is False  # 默认仅记录

    def test_env_enabled_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_ENABLED, "false")
        g = LightLookaheadGuard()
        assert g.enabled is False

    def test_env_strict_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_STRICT, "true")
        g = LightLookaheadGuard()
        assert g.strict is True

    def test_property_setters(self) -> None:
        g = LightLookaheadGuard(enabled=True, strict=False)
        g.enabled = False
        g.strict = True
        assert g.enabled is False and g.strict is True

    def test_initial_state(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.current_ts is None
        assert g.violations == []
        assert g.violation_count == 0

    def test_stats(self) -> None:
        g = LightLookaheadGuard(enabled=True, strict=False, allow_same_day=True)
        s = g.stats
        assert s["enabled"] is True and s["strict"] is False
        assert s["allow_same_day"] is True and s["violations"] == 0

    def test_repr(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert "LightLookaheadGuard" in repr(g)


# ─────────────────────────────────────────────
# on_bar_close
# ─────────────────────────────────────────────

class TestOnBarClose:
    def test_advance(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.current_ts == datetime(2024, 1, 15).timestamp()

    def test_regress_records_violation(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        g.on_bar_close("2024-01-14")
        assert g.violation_count == 1
        assert g.violations[0].rule == RULE_CLOCK_REGRESS

    def test_regress_advances_anyway(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        g.on_bar_close("2024-01-14")
        assert g.current_ts == datetime(2024, 1, 14).timestamp()

    def test_disabled_does_not_advance(self) -> None:
        g = LightLookaheadGuard(enabled=False)
        g.on_bar_close("2024-01-15")
        assert g.current_ts is None

    def test_invalid_ts_raises(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        with pytest.raises(ValueError):
            g.on_bar_close("not-a-date")


# ─────────────────────────────────────────────
# check_data_access
# ─────────────────────────────────────────────

class TestCheckDataAccess:
    def test_historical_ok(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.check_data_access("2024-01-14", source="strategy") is True

    def test_current_day_ok(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.check_data_access("2024-01-15") is True

    def test_future_data_blocked(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.check_data_access("2024-01-16", source="factor", symbol="000001") is False
        v = g.violations[0]
        assert v.rule == RULE_FUTURE_DATA
        assert v.source == "factor" and v.symbol == "000001"

    def test_no_timeline_returns_true(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.check_data_access("2024-01-16") is True

    def test_disabled_returns_true(self) -> None:
        g = LightLookaheadGuard(enabled=False)
        assert g.check_data_access("2099-01-01") is True


# ─────────────────────────────────────────────
# check_indicator_window
# ─────────────────────────────────────────────

class TestCheckIndicatorWindow:
    def test_legal_window(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.check_indicator_window("2024-01-15", "2024-01-14", indicator="RSRS") is True

    def test_data_end_equals_calc_day_blocked(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.check_indicator_window("2024-01-15", "2024-01-15", indicator="RSRS") is False
        assert g.violations[-1].rule == RULE_INDICATOR_WINDOW

    def test_data_end_future_blocked(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.check_indicator_window("2024-01-15", "2024-01-16") is False

    def test_calc_in_future_blocked(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        assert g.check_indicator_window("2024-01-16", "2024-01-15") is False

    def test_unknown_times_return_true(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.check_indicator_window("2024-01-15", None) is True  # type: ignore[arg-type]

    def test_disabled_true(self) -> None:
        g = LightLookaheadGuard(enabled=False)
        assert g.check_indicator_window("2024-01-15", "2024-01-15") is True


# ─────────────────────────────────────────────
# check_signal_execution
# ─────────────────────────────────────────────

class TestCheckSignalExecution:
    def test_t_plus_one_ok(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.check_signal_execution("2024-01-15", "2024-01-16", price_type="OPEN") is True

    def test_same_day_blocked(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.check_signal_execution("2024-01-15", "2024-01-15", price_type="CLOSE") is False
        assert g.violations[-1].rule == RULE_SIGNAL_EXECUTION

    def test_exec_before_signal_blocked(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.check_signal_execution("2024-01-16", "2024-01-15", price_type="OPEN") is False

    def test_allow_same_day_passes(self) -> None:
        g = LightLookaheadGuard(enabled=True, allow_same_day=True)
        assert g.check_signal_execution("2024-01-15", "2024-01-15", price_type="CLOSE") is True

    def test_disabled_true(self) -> None:
        g = LightLookaheadGuard(enabled=False)
        assert g.check_signal_execution("2024-01-16", "2024-01-15") is True

    def test_unknown_signal_true(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.check_signal_execution(None, "2024-01-15") is True  # type: ignore[arg-type]


# ─────────────────────────────────────────────
# assert_pass / report / reset
# ─────────────────────────────────────────────

class TestAssertPass:
    def test_clean_passes(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        assert g.assert_pass() is True

    def test_violation_raises_with_details(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        g.check_data_access("2024-01-16")
        with pytest.raises(LookaheadError) as ei:
            g.assert_pass()
        assert "共发现 1 处违规" in str(ei.value)
        assert RULE_FUTURE_DATA in str(ei.value)

    def test_disabled_passes(self) -> None:
        g = LightLookaheadGuard(enabled=False)
        g.on_bar_close("2024-01-15")
        g.check_data_access("2099-01-01")
        assert g.assert_pass() is True


class TestReportAndReset:
    def test_report_content(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        g.check_data_access("2024-01-16", source="factor")
        text = g.report()
        assert "轻量防未来函数校验报告" in text
        assert RULE_FUTURE_DATA in text
        assert "违规总数: 1" in text

    def test_report_clean(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        text = g.report()
        assert "（无违规，通过）" in text

    def test_reset_clears(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        g.check_data_access("2024-01-16")
        g.reset()
        assert g.current_ts is None
        assert g.violations == []

    def test_violations_is_copy(self) -> None:
        g = LightLookaheadGuard(enabled=True)
        g.on_bar_close("2024-01-15")
        g.check_data_access("2024-01-16")
        vs = g.violations
        vs.clear()
        assert g.violation_count == 1  # 内部列表未被外部修改


# ─────────────────────────────────────────────
# strict 模式 / 单例
# ─────────────────────────────────────────────

class TestStrictAndSingleton:
    def test_strict_raises_immediately(self) -> None:
        g = LightLookaheadGuard(enabled=True, strict=True)
        g.on_bar_close("2024-01-15")
        with pytest.raises(LookaheadError):
            g.check_data_access("2024-01-16")

    def test_strict_does_not_record_after_raise(self) -> None:
        # strict 抛异常后违规仍已记录（_report 先 append 再 raise）
        g = LightLookaheadGuard(enabled=True, strict=True)
        g.on_bar_close("2024-01-15")
        with pytest.raises(LookaheadError):
            g.check_data_access("2024-01-16")
        assert g.violation_count == 1

    def test_singleton_identity(self) -> None:
        reset_light_lookahead_guard()
        g1 = get_light_lookahead_guard()
        g2 = get_light_lookahead_guard()
        assert g1 is g2
        reset_light_lookahead_guard()
        g3 = get_light_lookahead_guard()
        assert g3 is not g1
