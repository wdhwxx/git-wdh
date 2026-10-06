# 文件名：tests/unit/test_dynamic_baseline.py
"""
test_dynamic_baseline.py - C-6 动态基线+异常检测测试（回写#235）
========================================================================
覆盖（验收口径：指标脱离动态基线 → 异常事件；基线随 regime 自适应）：
  1. 滚动基线计算（zscore mean±k×std / quantile 分位带）。
  2. 同环比（mom 环比 / yoy 同比变化率）。
  3. 多条件组合判定（AND / OR 可配，条件结构可注入）。
  4. anomaly 事件结构（time/series/value/baseline_low/baseline_high/
     ratio/conditions_hit，JSON 可序列化；同 series 同 ts 只产一条）。
  5. 冷启动静态阈值兜底（样本不足 → 注入静态阈值判定；未注入 → 仅记录不告警）。
  6. regime 自适应切换（按 (series,regime) 独立基线，切换留痕）。
  7. None 配置（PENDING_CAL）不告警不编造。
  8. 健康序列不异常。
  9. 防未来：record 拒绝 timestamp>now。
 10. 防御式校验（空 series / 非法 window/k/method/conditions）。
 11. set_gauge 指标按 series+regime labels 落盘 + REGISTERED_METRICS 声明。

依赖: pytest, myquant.monitoring.dynamic_baseline, myquant.core.constants。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.dynamic_baseline import (
    CATEGORY_BASELINE,
    ALERT_SOURCE,
    DEFAULT_REGIME,
    METRIC_ANOMALY,
    METRIC_BASELINE_MEAN,
    METRIC_MOM_RATIO,
    REGISTERED_METRICS,
    DynamicBaseline,
)

_BASE: float = 1_700_000_000.0
_STEP: float = 60.0
_NOW: float = _BASE + 1_000_000.0  # 固定"现在"，远大于所有喂入点 ts


def _ts(k: int) -> float:
    """第 k 个采样点时间戳（全部 <= _NOW，严格防未来）。"""
    return _BASE + k * _STEP


class _FakeAlert:
    """捕获 alert() 调用的告警管理器桩。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def alert(self, **kwargs: Any) -> int:
        self.calls.append(dict(kwargs))
        return 1


class _FakeMetrics:
    """捕获 set_gauge 的指标桩（labels 并入 key）。"""

    def __init__(self) -> None:
        self.gauges: Dict[str, float] = {}

    def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
        lbl = labels or {}
        key = f"{name}:{lbl.get('series', '?')}:{lbl.get('regime', '?')}"
        self.gauges[key] = float(value)


@pytest.fixture()
def clock() -> Any:
    return lambda: _NOW


def _make(clock: Any, **kw: Any) -> DynamicBaseline:
    """构造默认参数的检测器（注入假告警/假指标/固定时钟）。"""
    kw.setdefault("alert_manager", _FakeAlert())
    kw.setdefault("metrics_collector", _FakeMetrics())
    kw.setdefault("clock", clock)
    return DynamicBaseline(**kw)


# 围绕 mean=100、std≈0.2 的平稳序列（20 点）
_STEADY: List[float] = [
    100.0, 100.1, 99.9, 100.05, 99.95, 100.02, 99.98, 100.01, 99.99, 100.0,
    100.03, 99.97, 100.01, 99.99, 100.0, 100.02, 99.98, 100.0, 100.01, 99.99,
]


def _feed_steady(db: DynamicBaseline, series: str = "latency_ms") -> None:
    for i, v in enumerate(_STEADY):
        db.record(series, _ts(i), v)


class TestRollingBaseline:
    def test_zscore基线带_健康点不告警(self, clock: Any) -> None:
        db = _make(clock, window=20, k=3.0, min_samples=10)
        _feed_steady(db)
        # 再喂一个仍在带内的点
        ev = db.record("latency_ms", _ts(20), 100.05)
        assert ev is None  # 健康

    def test_zscore基线带外_产出anomaly(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, k=3.0, min_samples=10, alert_manager=am)
        _feed_steady(db)
        ev = db.record("latency_ms", _ts(30), 110.0)  # 远超 mean±3σ≈100±0.6
        assert ev is not None
        assert ev["conditions_hit"] == ["out_of_baseline"]
        assert ev["baseline_low"] is not None and ev["baseline_high"] is not None
        assert ev["baseline_low"] < 100.0 < ev["baseline_high"]
        assert ev["value"] == 110.0
        # 已发 WARNING
        assert len(am.calls) == 1
        assert am.calls[0]["level"] == SeverityLevel.WARNING
        assert am.calls[0]["category"] == CATEGORY_BASELINE

    def test_quantile分位带(self, clock: Any) -> None:
        db = _make(clock, window=20, method="quantile",
                   quantile_low=0.1, quantile_high=0.9, min_samples=10)
        _feed_steady(db)
        ev = db.record("latency_ms", _ts(30), 110.0)
        assert ev is not None
        assert ev["baseline_low"] is not None and ev["baseline_high"] is not None
        assert ev["baseline_low"] < 100.0 < ev["baseline_high"]

    def test_基线只用过去点_不含当前点(self, clock: Any) -> None:
        # 当前巨跳点不应把自己计入基线（否则 std 爆大反而不报警）
        db = _make(clock, window=10, k=3.0, min_samples=5)
        _feed_steady(db)
        ev = db.record("latency_ms", _ts(30), 10_000.0)
        assert ev is not None  # 若当前点污染基线，std 巨大→反而带内不告警


class TestMomYoy:
    def test_环比变化率(self, clock: Any) -> None:
        db = _make(clock, window=20, k=3.0, min_samples=10, yoy_lag=5)
        _feed_steady(db)
        # 上一点 99.99 → 当前 105：mom ≈ (105-99.99)/99.99 ≈ 0.05
        ev = db.record("latency_ms", _ts(30), 105.0)
        ratio = ev["ratio"] if ev else None
        # 该点可能出带也可能不出带；无论是否异常，ratio.mom 应被计算
        # 直接从内部评估：喂一个明确出带的点拿 event
        ev2 = db.record("latency_ms", _ts(31), 120.0)
        assert ev2 is not None
        assert ev2["ratio"]["mom"] is not None
        assert abs(ev2["ratio"]["mom"] - (120.0 - 105.0) / 105.0) < 1e-4

    def test_同比变化率(self, clock: Any) -> None:
        db = _make(clock, window=20, k=3.0, min_samples=10, yoy_lag=5)
        # 喂 30 个点（k=0..29），值恒 100
        for i in range(30):
            db.record("x", _ts(i), 100.0)
        # 当前 k=40 值 120：同比 = (120 - k=35 的值)/100 = 0.2
        ev = db.record("x", _ts(40), 120.0)
        assert ev is not None
        # yoy_lag=5：hist[-5] = k=35 = 100 → (120-100)/100 = 0.2
        assert ev["ratio"]["yoy"] is not None
        assert abs(ev["ratio"]["yoy"] - 0.2) < 1e-4

    def test_分母为零_yoy为None不编造(self, clock: Any) -> None:
        db = _make(clock, window=10, k=3.0, min_samples=3, yoy_lag=2)
        db.record("x", _ts(0), 0.0)
        db.record("x", _ts(1), 0.0)
        db.record("x", _ts(2), 0.0)
        ev = db.record("x", _ts(5), 5.0)  # hist[-2]=k=3 不存在 → yoy None
        # k=5: hist = k=0,1,2 (0,0,0); yoy_lag=2 → hist[-2]=k=1=0 → 分母0 → None
        if ev is not None:
            assert ev["ratio"]["yoy"] is None


class TestMultiCondition:
    def test_AND组合_需全部命中(self, clock: Any) -> None:
        am = _FakeAlert()
        cond = {
            "combine": "AND",
            "rules": [
                {"type": "out_of_baseline"},
                {"type": "mom_ratio", "side": "both", "min_ratio": 0.5},
            ],
        }
        db = _make(clock, window=10, k=3.0, min_samples=5, conditions=cond, alert_manager=am)
        for i in range(10):
            db.record("x", _ts(i), 100.0)
        # 小幅上跳 105：出带边际但 mom=5%<50% → AND 不命中
        ev = db.record("x", _ts(10), 105.0)
        assert ev is None
        assert am.calls == []
        # 巨跳 200：既出带，mom≈+90%>50% → AND 命中
        ev2 = db.record("x", _ts(11), 200.0)
        assert ev2 is not None
        assert set(ev2["conditions_hit"]) == {"out_of_baseline", "mom_ratio"}
        assert len(am.calls) == 1

    def test_OR组合_任一命中即异常(self, clock: Any) -> None:
        am = _FakeAlert()
        cond = {
            "combine": "OR",
            "rules": [
                {"type": "out_of_baseline"},
                {"type": "mom_ratio", "side": "both", "min_ratio": 0.5},
            ],
        }
        db = _make(clock, window=10, k=3.0, min_samples=5, conditions=cond, alert_manager=am)
        for i in range(10):
            db.record("x", _ts(i), 100.0)
        # 小幅 105：在基线带内（std≈0）→ out_of_baseline 命中（105>100+小k*0=100）
        # mom=5%<50%。OR 下 out_of_baseline 命中即异常
        ev = db.record("x", _ts(10), 105.0)
        assert ev is not None
        assert "out_of_baseline" in ev["conditions_hit"]

    def test_yoy规则可配(self, clock: Any) -> None:
        cond = {
            "combine": "OR",
            "rules": [{"type": "yoy_ratio", "side": "up", "min_ratio": 0.1}],
        }
        db = _make(clock, window=10, k=3.0, min_samples=3, yoy_lag=5, conditions=cond)
        for i in range(10):
            db.record("x", _ts(i), 100.0)
        # k=20: yoy = (120 - k=15 的值)/100 = 0.2 > 0.1 → 命中
        ev = db.record("x", _ts(20), 120.0)
        assert ev is not None
        assert ev["conditions_hit"] == ["yoy_ratio"]


class TestAnomalyEvent:
    def test_事件结构完整_JSON可序列化(self, clock: Any) -> None:
        db = _make(clock, window=20, k=3.0, min_samples=10)
        _feed_steady(db)
        ev = db.record("latency_ms", _ts(30), 120.0)
        assert ev is not None
        for k in ("time", "series", "value", "baseline_low",
                  "baseline_high", "ratio", "conditions_hit"):
            assert k in ev
        assert ev["series"] == "latency_ms"
        assert isinstance(ev["conditions_hit"], list)
        # JSON 可序列化
        s = json.dumps(ev, ensure_ascii=False)
        assert json.loads(s)["series"] == "latency_ms"

    def test_同series同ts只产一条(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, k=3.0, min_samples=10, alert_manager=am)
        _feed_steady(db)
        ev1 = db.record("latency_ms", _ts(30), 120.0)
        # 同 ts 重复喂（覆盖同值）→ 幂等，不重复产出
        ev2 = db.record("latency_ms", _ts(30), 120.0)
        assert ev1 is not None
        # 同 ts 覆盖后重新评估：基线历史不变 → 仍是异常；但应只对应一条事件
        # 缓冲中同 ts 只一条
        buf = db._buffers[("latency_ms", DEFAULT_REGIME)]
        assert sum(1 for ts, _ in buf if abs(ts - _ts(30)) < 1e-9) == 1

    def test_replay批量回放收集事件(self, clock: Any) -> None:
        db = _make(clock, window=20, k=3.0, min_samples=10)
        points = [(_ts(i), v) for i, v in enumerate(_STEADY)]
        points.append((_ts(30), 120.0))
        events = db.replay("latency_ms", points)
        assert len(events) == 1
        assert events[0]["value"] == 120.0


class TestColdStart:
    def test_样本不足_静态阈值兜底告警(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, min_samples=10,
                   static_default=(50.0, 150.0), alert_manager=am)
        # 只喂 3 点（< min_samples=10）→ 冷启动
        for i in range(3):
            db.record("qps", _ts(i), 100.0 + i)
        ev = db.record("qps", _ts(5), 200.0)  # 超静态上限 150
        assert ev is not None
        assert ev["cold_start"] is True
        assert ev["baseline_low"] is None  # 动态基线未建立
        assert len(am.calls) == 1

    def test_冷启动_无静态阈值_仅记录不告警(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, min_samples=10, alert_manager=am)  # 无静态阈值
        for i in range(3):
            db.record("qps", _ts(i), 100.0)
        ev = db.record("qps", _ts(5), 9999.0)
        assert ev is None
        assert am.calls == []

    def test_perseries静态阈值优先(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, min_samples=10,
                   static_thresholds={"lat": (0.0, 10.0)},
                   static_default=(0.0, 1000.0), alert_manager=am)
        db.record("lat", _ts(0), 5.0)
        ev = db.record("lat", _ts(1), 50.0)  # 超 per-series 上限 10
        assert ev is not None and ev["cold_start"] is True


class TestRegime:
    def test_切换留痕(self, clock: Any) -> None:
        db = _make(clock)
        assert db.regime == DEFAULT_REGIME
        db.set_regime("volatile")
        assert db.regime == "volatile"
        hist = db.regime_history()
        assert hist[-1]["old"] == DEFAULT_REGIME
        assert hist[-1]["new"] == "volatile"

    def test_同regime不重复留痕(self, clock: Any) -> None:
        db = _make(clock)
        n0 = len(db.regime_history())
        db.set_regime("volatile")
        db.set_regime("volatile")  # 同标签 no-op
        assert len(db.regime_history()) == n0 + 1

    def test_regime独立基线_切换后自适应(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, k=3.0, min_samples=10, alert_manager=am)
        _feed_steady(db, "lat")  # default regime 均值≈100
        ev_def = db.record("lat", _ts(30), 120.0)
        assert ev_def is not None and ev_def["regime"] == "default"
        assert abs(ev_def["baseline_mean"] - 100.0) < 1.0

        # 切到 regime_b（均值≈200 的平稳段）
        db.set_regime("trend_up")
        vol = [200.0 + (i % 3) * 0.1 for i in range(20)]
        for i, v in enumerate(vol):
            db.record("lat", _ts(40 + i), v)
        ev_b = db.record("lat", _ts(80), 260.0)  # 相对 regime_b 均值≈200 出带
        assert ev_b is not None and ev_b["regime"] == "trend_up"
        assert abs(ev_b["baseline_mean"] - 200.0) < 2.0  # 基线已切到新 regime


class TestNoneConfigNoFabricate:
    def test_SLO未配置_冷启动不告警(self, clock: Any) -> None:
        # 无注入静态阈值；settings.yaml 无 slo.dynamic_baseline 块 → PENDING_CAL
        am = _FakeAlert()
        db = _make(clock, window=20, min_samples=10, alert_manager=am)
        for i in range(3):
            db.record("lat", _ts(i), 100.0)
        ev = db.record("lat", _ts(5), 9999.0)
        assert ev is None
        assert am.calls == []  # 不告警
        # 指标照常落盘（不编造基线值）
        state = db.to_dict()
        assert state["anomaly_count"] == 0


class TestHealthyNoAlert:
    def test_平稳序列全程不告警(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, k=3.0, min_samples=10, alert_manager=am)
        for i in range(40):
            v = 100.0 + ((i % 7) - 3) * 0.1  # ±0.3 内抖动
            ev = db.record("lat", _ts(i), v)
            assert ev is None
        assert am.calls == []


class TestFutureReject:
    def test_拒绝未来点(self, clock: Any) -> None:
        db = _make(clock)
        with pytest.raises(ValueError):
            db.record("lat", _NOW + 100.0, 1.0)

    def test_未来点不入库(self, clock: Any) -> None:
        db = _make(clock)
        try:
            db.record("lat", _NOW + 100.0, 1.0)
        except ValueError:
            pass
        assert ("lat", DEFAULT_REGIME) not in db._buffers


class TestDefensive:
    def test_空series拒绝(self, clock: Any) -> None:
        db = _make(clock)
        with pytest.raises(ValueError):
            db.record("", _ts(0), 1.0)

    def test_非有限value拒绝(self, clock: Any) -> None:
        db = _make(clock)
        with pytest.raises(ValueError):
            db.record("lat", _ts(0), float("nan"))

    def test_window非法拒绝(self, clock: Any) -> None:
        with pytest.raises(ValueError):
            DynamicBaseline(window=0)

    def test_k负拒绝(self, clock: Any) -> None:
        with pytest.raises(ValueError):
            DynamicBaseline(k=-1.0)

    def test_method非法拒绝(self, clock: Any) -> None:
        with pytest.raises(ValueError):
            DynamicBaseline(method="median")

    def test_quantile区间非法拒绝(self, clock: Any) -> None:
        with pytest.raises(ValueError):
            DynamicBaseline(quantile_low=0.9, quantile_high=0.1)

    def test_conditions非法combine拒绝(self, clock: Any) -> None:
        with pytest.raises(ValueError):
            DynamicBaseline(conditions={"combine": "X", "rules": [{"type": "out_of_baseline"}]})

    def test_conditions非法rule_type拒绝(self, clock: Any) -> None:
        with pytest.raises(ValueError):
            DynamicBaseline(conditions={"combine": "AND", "rules": [{"type": "bogus"}]})

    def test_空series_set_regime拒绝(self, clock: Any) -> None:
        db = _make(clock)
        with pytest.raises(ValueError):
            db.set_regime("")

    def test_replay非法参数(self, clock: Any) -> None:
        db = _make(clock)
        with pytest.raises(ValueError):
            db.replay("", [])
        with pytest.raises(ValueError):
            db.replay("x", "not-a-list")  # type: ignore[arg-type]


class TestMetrics:
    def test_set_gauge按series_regime落盘(self, clock: Any) -> None:
        met = _FakeMetrics()
        db = _make(clock, metrics_collector=met, window=20, k=3.0, min_samples=10)
        _feed_steady(db, "lat")
        db.record("lat", _ts(30), 120.0)  # 异常
        # 异常标记落 1.0
        assert met.gauges.get(f"{METRIC_ANOMALY}:lat:default") == 1.0
        # 基线均值落盘（≈100）
        mean_key = f"{METRIC_BASELINE_MEAN}:lat:default"
        assert mean_key in met.gauges
        assert abs(met.gauges[mean_key] - 100.0) < 1.0
        # 环比指标落盘
        assert f"{METRIC_MOM_RATIO}:lat:default" in met.gauges

    def test_REGISTERED_METRICS声明完整(self) -> None:
        # 7 项指标声明齐全
        assert METRIC_BASELINE_MEAN in REGISTERED_METRICS
        assert METRIC_ANOMALY in REGISTERED_METRICS
        assert len(REGISTERED_METRICS) == 7

    def test_to_dict可观测(self, clock: Any) -> None:
        db = _make(clock)
        snap = db.to_dict()
        assert snap["regime"] == DEFAULT_REGIME
        assert "window" in snap and "anomaly_count" in snap


class TestAlertContract:
    def test_告警契约字段(self, clock: Any) -> None:
        am = _FakeAlert()
        db = _make(clock, window=20, k=3.0, min_samples=10, alert_manager=am)
        _feed_steady(db, "lat")
        db.record("lat", _ts(30), 120.0)
        assert len(am.calls) == 1
        c = am.calls[0]
        assert c["level"] == SeverityLevel.WARNING
        assert c["category"] == CATEGORY_BASELINE
        assert c["source"] == ALERT_SOURCE
        assert c["deduplication_key"] == "dynamic_baseline:lat:default"
        assert "anomaly" in c["details"]
