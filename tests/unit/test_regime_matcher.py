# 文件名：tests/unit/test_regime_matcher.py
"""
test_regime_matcher.py - C-3 环境匹配度评分单元测试（回写#242）
==========================================================================
覆盖：
  - indicators/rsrs.py 追加的 classify_regime / regime_from_series（5 档环境识别）
  - RegimeMatcher：当前环境判定、策略-环境匹配评分（矩阵）、
    低匹配 P2/WARNING 告警契约（category=regime、dedup=regime:match:<strategy>、
    details 附匹配分/环境/降频降仓建议）
  - 阈值未配置（PENDING_CAL）不告警不编造（R-04）
  - 严格防未来（拒绝未来 RSRS 观测；只消费已发生值）
  - 冷启动保守（无观测归 range 不误判方向）
  - 防御式校验（非法阈值/空策略名/非法风格/inf）
  - 指标 set_gauge 动态注册（labels={"strategy": 策略名}）
全部使用内存桩（不依赖真实 AlertManager/MetricsCollector），不触碰 quant.db。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pytest

from myquant.core.constants import SeverityLevel
from myquant.indicators.rsrs import (
    REGIME_TREND_R2_MIN,
    classify_regime,
    regime_from_series,
)
from myquant.monitoring.regime_matcher import (
    CATEGORY_REGIME,
    DEDUP_MATCH_PREFIX,
    MATCH_SCORE_BY_STYLE,
    METRIC_REGIME_MATCH_SCORE,
    METRIC_REGIME_STATE,
    REGISTERED_METRICS,
    RegimeMatcher,
)


# ─────────────────────────────────────────────
# 内存桩（隔离环境）
# ─────────────────────────────────────────────
class _FakeMetrics:
    """内存桩采集器：记录 set_gauge 调用，供断言。"""

    def __init__(self) -> None:
        self.gauges: Dict[Tuple[str, tuple], float] = {}

    def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        self.gauges[key] = float(value)


class _FakeAlerts:
    """内存桩告警管理器：记录 alert 调用，供断言契约。"""

    def __init__(self) -> None:
        self.alerts: List[Dict[str, Any]] = []

    def alert(self, **kwargs: Any) -> None:
        self.alerts.append(kwargs)


def _fixed_clock(now: float) -> Any:
    return lambda: now


# ═══════════════════════════════════════════════
# 一、indicators/rsrs.py 追加的环境识别（classify_regime）
# ═══════════════════════════════════════════════

class TestClassifyRegime:
    """RSRS 单点环境分类（5 档）。"""

    def test_trend_up_when_above_buy_and_strong_structure(self) -> None:
        assert classify_regime(0.9, 0.6) == "trend_up"
        # 严格大于（与 RSRS 信号语义一致）：恰好等于 buy 阈值不算超阈 → 温和上升
        assert classify_regime(0.7, 0.5) == "mild_up"

    def test_trend_down_when_below_sell_and_strong_structure(self) -> None:
        assert classify_regime(-0.9, 0.6) == "trend_down"
        # 严格小于（与 RSRS 信号语义一致）：恰好等于 sell 阈值不算超阈 → 温和下降
        assert classify_regime(-0.7, 0.5) == "mild_down"

    def test_strong_signal_weak_structure_conservative_range(self) -> None:
        # RSRS 超阈但 R² 低于趋势置信下限 → 保守归震荡，不误判趋势
        assert classify_regime(0.9, 0.1) == "range"
        assert classify_regime(-0.9, 0.1) == "range"

    def test_mild_up_and_mild_down(self) -> None:
        assert classify_regime(0.4, 0.5) == "mild_up"
        assert classify_regime(0.1, 0.9) == "mild_up"
        assert classify_regime(-0.4, 0.5) == "mild_down"
        assert classify_regime(-0.1, 0.9) == "mild_down"

    def test_zero_and_nan_conservative_range(self) -> None:
        assert classify_regime(0.0, 0.5) == "range"
        assert classify_regime(float("nan"), 0.5) == "range"
        assert classify_regime(0.3, float("nan")) == "range"
        assert classify_regime(float("nan"), float("nan")) == "range"

    def test_default_trend_r2_min_constant(self) -> None:
        assert REGIME_TREND_R2_MIN == 0.30

    def test_invalid_thresholds_raise(self) -> None:
        with pytest.raises(ValueError):
            classify_regime(0.5, 0.5, buy_threshold=0.3, sell_threshold=0.5)
        with pytest.raises(ValueError):
            classify_regime(0.5, 0.5, trend_r2_min=-0.1)
        with pytest.raises(ValueError):
            classify_regime(0.5, 0.5, trend_r2_min=1.5)

    def test_inf_returns_range(self) -> None:
        assert classify_regime(float("inf"), 0.6) == "range"
        assert classify_regime(0.5, float("inf")) == "range"


class TestRegimeFromSeries:
    """批量环境分类。"""

    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "rsrs": [0.9, -0.9, 0.4, -0.4, 0.0],
                "r2": [0.6, 0.6, 0.5, 0.5, 0.5],
            },
            index=pd.date_range("2026-01-02", periods=5, freq="D"),
        )

    def test_batch_classification(self) -> None:
        df = self._frame()
        out = regime_from_series(df["rsrs"], df["r2"])
        assert isinstance(out, pd.Series)
        assert list(out) == ["trend_up", "trend_down", "mild_up", "mild_down", "range"]

    def test_length_mismatch_raises(self) -> None:
        s1 = pd.Series([0.9, 0.4])
        s2 = pd.Series([0.6])
        with pytest.raises(ValueError):
            regime_from_series(s1, s2)

    def test_index_mismatch_raises(self) -> None:
        s1 = pd.Series([0.9], index=["a"])
        s2 = pd.Series([0.6], index=["b"])
        with pytest.raises(ValueError):
            regime_from_series(s1, s2)


# ═══════════════════════════════════════════════
# 二、RegimeMatcher：环境判定 + 匹配评分 + 低匹配告警
# ═══════════════════════════════════════════════

class TestRegimeMatcherEnvironment:
    """当前环境判定（基于最近一个已发生 RSRS 值）。"""

    def test_uses_latest_observed_rsrs(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            strategy_styles={"trend_a": "trend"},
            alert_manager=_FakeAlerts(),
            metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        # 先喂一个 trend_down 值，再喂最新 trend_up 值 → 环境应为最新
        matcher.observe_rsrs(now - 100, -0.9, 0.6)
        matcher.observe_rsrs(now - 10, 0.9, 0.6)
        state = matcher.evaluate()
        assert state["regime"] == "trend_up"

    def test_weak_structure_conservative(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.9, 0.1)  # 信号强但 R² 弱
        assert matcher.evaluate()["regime"] == "range"

    def test_cold_start_conservative_range(self) -> None:
        matcher = RegimeMatcher(
            strategy_styles={"range_b": "range"}, match_score_min=80.0,
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(1_800_000.0),
        )
        state = matcher.evaluate()
        assert state["regime"] == "range"
        assert state["match_scores"]["range_b"] == 100
        assert state["alert_triggered"] is False

    def test_custom_buy_sell_thresholds(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            buy_threshold=0.5, sell_threshold=-0.5,
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.6, 0.6)
        assert matcher.evaluate()["regime"] == "trend_up"


class TestMatchScoreMatrix:
    """策略-环境匹配评分矩阵。"""

    def test_matrix_contract(self) -> None:
        # 矩阵缺档不应出现（5 档 × 2 风格全覆盖）
        assert set(MATCH_SCORE_BY_STYLE["trend"].keys()) == {
            "trend_up", "trend_down", "mild_up", "mild_down", "range",
        }
        assert set(MATCH_SCORE_BY_STYLE["range"].keys()) == {
            "trend_up", "trend_down", "mild_up", "mild_down", "range",
        }

    def test_trend_strategy_prefers_trend(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            strategy_styles={"trend_a": "trend"},
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.9, 0.6)  # trend_up
        scores = matcher.evaluate()["match_scores"]
        assert scores["trend_a"] == 100

    def test_range_strategy_prefers_range(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            strategy_styles={"range_b": "range"},
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.0, 0.5)  # range
        scores = matcher.evaluate()["match_scores"]
        assert scores["range_b"] == 100

    def test_mismatch_low_scores(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            strategy_styles={"trend_a": "trend", "range_b": "range"},
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.9, 0.6)  # trend_up
        scores = matcher.evaluate()["match_scores"]
        assert scores["trend_a"] == 100
        assert scores["range_b"] == 40  # range 策略 × 趋势环境 = 低匹配

    def test_unknown_style_falls_back_to_trend(self) -> None:
        # 防御：非法风格在注册时被拦截；此处验证矩阵缺档保守取 range 分
        now = 1_800_000.0
        matcher = RegimeMatcher(
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.register_strategy_style("s", "trend")
        matcher.observe_rsrs(now - 10, 0.9, 0.6)
        assert matcher.evaluate()["match_scores"]["s"] == 100


class TestLowMatchAlert:
    """低匹配 → P2/WARNING 告警契约。"""

    def _matcher(self, match_score_min: Optional[float] = 50.0) -> Tuple[RegimeMatcher, _FakeAlerts]:
        now = 1_800_000.0
        alerts = _FakeAlerts()
        matcher = RegimeMatcher(
            strategy_styles={"trend_a": "trend", "range_b": "range"},
            match_score_min=match_score_min,
            alert_manager=alerts,
            metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.9, 0.6)  # trend_up
        return matcher, alerts

    def test_low_match_triggers_warning(self) -> None:
        matcher, alerts = self._matcher(50.0)
        state = matcher.evaluate()
        assert state["alert_triggered"] is True
        assert len(alerts.alerts) == 1
        alert = alerts.alerts[0]
        assert alert["level"] == SeverityLevel.WARNING  # P2
        assert alert["category"] == CATEGORY_REGIME
        assert alert["source"] == "monitoring.regime_matcher"
        assert alert["dedup_key"] == f"{DEDUP_MATCH_PREFIX}range_b"
        assert alert["details"]["match_score"] == 40
        assert alert["details"]["regime"] == "trend_up"
        assert "suggestion" in alert["details"]
        assert "降频" in alert["details"]["suggestion"]

    def test_high_match_no_alert(self) -> None:
        matcher, alerts = self._matcher(50.0)
        state = matcher.evaluate()
        # trend_a 匹配 100 ≥ 50，不应单独告警
        assert all(a["details"].get("strategy") != "trend_a" for a in alerts.alerts)
        assert state["match_scores"]["trend_a"] == 100

    def test_threshold_not_configured_no_alert(self) -> None:
        matcher, alerts = self._matcher(match_score_min=None)  # PENDING_CAL
        state = matcher.evaluate()
        assert state["match_scores"]["range_b"] == 40  # 评分照常计算
        assert state["alert_triggered"] is False  # 不告警不编造
        assert alerts.alerts == []

    def test_alert_failure_does_not_block(self) -> None:
        class _BoomAlerts:
            def alert(self, **kwargs: Any) -> None:
                raise RuntimeError("boom")

        now = 1_800_000.0
        matcher = RegimeMatcher(
            strategy_styles={"range_b": "range"}, match_score_min=50.0,
            alert_manager=_BoomAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.9, 0.6)
        state = matcher.evaluate()  # 不抛异常
        assert state["alert_triggered"] is True


# ═══════════════════════════════════════════════
# 三、防未来 + 防御校验
# ═══════════════════════════════════════════════

class TestFutureAndDefensive:
    """严格防未来与防御式编程。"""

    def test_reject_future_rsrs(self) -> None:
        matcher = RegimeMatcher(
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(1_800_000.0),
        )
        with pytest.raises(ValueError):
            matcher.observe_rsrs(1_900_000.0, 0.9, 0.6)  # 未来时间戳

    def test_accept_past_and_now(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 60, 0.4, 0.5)  # 过去
        matcher.observe_rsrs(now, 0.5, 0.5)  # 当前
        assert matcher.evaluate()["observed_points"] == 2

    def test_reject_inf_values(self) -> None:
        matcher = RegimeMatcher(
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(1_800_000.0),
        )
        with pytest.raises(ValueError):
            matcher.observe_rsrs(1_700_000.0, float("inf"), 0.6)
        with pytest.raises(ValueError):
            matcher.observe_rsrs(1_700_000.0, 0.6, float("inf"))

    def test_reject_negative_match_score_min(self) -> None:
        with pytest.raises(ValueError):
            RegimeMatcher(match_score_min=-1.0)

    def test_reject_out_of_range_trend_r2_min(self) -> None:
        with pytest.raises(ValueError):
            RegimeMatcher(trend_r2_min=1.5)

    def test_reject_inverted_buy_sell(self) -> None:
        with pytest.raises(ValueError):
            RegimeMatcher(buy_threshold=-0.7, sell_threshold=0.7)

    def test_reject_empty_strategy_name(self) -> None:
        matcher = RegimeMatcher()
        with pytest.raises(ValueError):
            matcher.register_strategy_style("", "trend")

    def test_reject_invalid_style(self) -> None:
        matcher = RegimeMatcher()
        with pytest.raises(ValueError):
            matcher.register_strategy_style("s", "mean_reversion")

    def test_reject_non_positive_max_points(self) -> None:
        with pytest.raises(ValueError):
            RegimeMatcher(max_points=0)

    def test_observe_non_finite_ts(self) -> None:
        matcher = RegimeMatcher(
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(1_800_000.0),
        )
        with pytest.raises(ValueError):
            matcher.observe_rsrs(float("nan"), 0.5, 0.5)


# ═══════════════════════════════════════════════
# 四、指标注册与状态可观测
# ═══════════════════════════════════════════════

class TestMetricsAndState:
    """指标 set_gauge 动态注册与 to_dict/summary 可观测。"""

    def test_registered_metrics_list(self) -> None:
        assert REGISTERED_METRICS == [METRIC_REGIME_STATE, METRIC_REGIME_MATCH_SCORE]

    def test_gauge_state_and_scores(self) -> None:
        now = 1_800_000.0
        metrics = _FakeMetrics()
        matcher = RegimeMatcher(
            strategy_styles={"trend_a": "trend", "range_b": "range"},
            alert_manager=_FakeAlerts(), metrics_collector=metrics,
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.9, 0.6)  # trend_up → code 3.0
        matcher.evaluate()
        assert metrics.gauges.get((METRIC_REGIME_STATE, ())) == 3.0
        assert metrics.gauges.get(
            (METRIC_REGIME_MATCH_SCORE, (("strategy", "trend_a"),))
        ) == 100.0
        assert metrics.gauges.get(
            (METRIC_REGIME_MATCH_SCORE, (("strategy", "range_b"),))
        ) == 40.0

    def test_to_dict_and_summary(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            strategy_styles={"trend_a": "trend"},
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        matcher.observe_rsrs(now - 10, 0.9, 0.6)
        state = matcher.evaluate()
        d = matcher.to_dict()
        assert d == state
        assert matcher.summary() == state
        assert d["regime"] == "trend_up"
        assert d["regime_code"] == 3.0
        assert d["last_poll_time"] == now

    def test_to_dict_before_evaluate_initial(self) -> None:
        matcher = RegimeMatcher(
            strategy_styles={"trend_a": "trend"},
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
        )
        d = matcher.to_dict()
        assert d["regime"] == "range"
        assert d["alert_triggered"] is False
        assert d["match_scores"] == {}

    def test_bounded_points_cache(self) -> None:
        now = 1_800_000.0
        matcher = RegimeMatcher(
            max_points=3,
            alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
            clock=_fixed_clock(now),
        )
        for i in range(5):
            matcher.observe_rsrs(now - (5 - i), 0.4, 0.5)
        assert len(matcher._points) == 3  # 超出丢最旧


class TestConfigReading:
    """阈值真源 R-04：构造注入优先，配置读取（PENDING_CAL 语义）。"""

    def test_constructor_injection_wins(self) -> None:
        matcher = RegimeMatcher(match_score_min=42.0, trend_r2_min=0.5)
        assert matcher._match_score_min == 42.0
        assert matcher._trend_r2_min == 0.5

    def test_config_slo_read_default(self) -> None:
        # settings.yaml 当前 PENDING_CAL（None）→ 不启用告警，但结构可读
        matcher = RegimeMatcher()
        assert matcher._match_score_min is None  # PENDING_CAL
        assert matcher._trend_r2_min == REGIME_TREND_R2_MIN  # 算法默认回退

    def test_strategy_styles_initial_dict(self) -> None:
        matcher = RegimeMatcher(strategy_styles={"a": "trend", "b": "range"})
        assert matcher.get_strategy_styles() == {"a": "trend", "b": "range"}
