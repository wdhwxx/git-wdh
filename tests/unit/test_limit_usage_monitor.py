# 文件名：tests/unit/test_limit_usage_monitor.py
"""
test_limit_usage_monitor.py - 限额使用率监控单元测试（B-3，回写#223）
==========================================================================
验收口径（施工总清单 B-3 行）：限额使用率指标进入 /api/metrics，超阈值分级告警；
阈值取自 P0-2 锁定表（usage >= 80% 预警、usage > 95% 强告警，LOCKED）。

本测试用注入快照 + 假告警/假指标验证：
  - 0.5 → 不告警；0.85 → WARNING(P2)；0.97 → ERROR(P1)
  - 阈值真源断言：monkeypatch slo.risk 配置，行为跟随配置
  - 未配置维度（limit=None）→ 不告警、不编造数值、指标 enabled=false
  - 指标经 set_gauge 写入 collector，get_all 可查
  - 防未来：只消费传入快照，不引用未来数据

密钥无关，不碰 quant.db。
"""

from __future__ import annotations

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.metrics_collector import MetricsCollector
from myquant.risk.rules import (
    DIM_CONCENTRATION,
    DIM_LEVERAGE,
    DIM_SINGLE_STOCK,
    METRIC_LIMIT_USAGE,
    LimitUsageMonitor,
    get_limit_usage_metrics_registration,
)


# ─────────────────────────────────────────────
#  夹具
# ─────────────────────────────────────────────
class _FakeAlert:
    """记录 alert() 调用的假告警管理器。"""

    def __init__(self) -> None:
        self.calls: list = []

    def alert(self, **kw):
        self.calls.append(kw)
        return 1


def _snap(max_single: float, total_assets: float = 1_000_000.0,
          equity: float = 1_000_000.0):
    """
    构造账户+持仓快照。

    总资产/净资产 = 100 万；最大单票 S1 市值=max_single，S2=10 万（更小不反超）；
    总市值 = max_single + 10 万。故集中度/单票 exposure = max_single/100万。
    """
    other = 100_000.0
    account = {
        "total_assets": total_assets,
        "market_value": max_single + other,
        "equity": equity,
    }
    positions = [
        {"symbol": "S1", "market_value": max_single},
        {"symbol": "S2", "market_value": other},
    ]
    return account, positions


def _make_monitor(am: _FakeAlert, met=None, **kw) -> LimitUsageMonitor:
    """
    构造监控器：默认 warn=0.80 / alert=0.95，三维度限额均给显式值。
    （杠杆限额给 1.0，使本夹具下杠杆 usage < 0.8 不干扰分级断言）
    """
    defaults = dict(
        warn_threshold=0.80,
        alert_threshold=0.95,
        concentration_limit=0.30,
        leverage_limit=1.0,
        single_stock_limit=0.30,
        alert_manager=am,
        metrics_collector=met,
    )
    defaults.update(kw)
    return LimitUsageMonitor(**defaults)


def _usage_of(max_single: float) -> float:
    """集中度/单票使用率：(max_single/1e6)/0.30。"""
    return (max_single / 1_000_000.0) / 0.30


# ─────────────────────────────────────────────
#  分级判定：0.5 无告警 / 0.85 WARNING / 0.97 ERROR
# ─────────────────────────────────────────────
class TestGrading:
    """usage 分级告警（warn=0.80 / alert=0.95）。"""

    def test_usage_05_不告警(self) -> None:
        am = _FakeAlert()
        mon = _make_monitor(am)
        acct, pos = _snap(150_000.0)  # usage=0.5
        result = mon.check(account=acct, positions=pos)
        assert _usage_of(150_000.0) == pytest.approx(0.5, abs=1e-6)
        assert am.calls == []
        assert result["fired"] == {}

    def test_usage_085_触发WARNING(self) -> None:
        am = _FakeAlert()
        mon = _make_monitor(am)
        acct, pos = _snap(255_000.0)  # usage=0.85
        result = mon.check(account=acct, positions=pos)
        warns = [c for c in am.calls if c["level"] == SeverityLevel.WARNING]
        assert len(warns) >= 1
        assert result["fired"][DIM_CONCENTRATION] == "WARNING"
        # details 含 usage/limit 及配置来源
        detail = warns[0]["details"]
        assert detail["usage"] == pytest.approx(0.85, abs=1e-3)
        assert detail["limit"] == 0.30
        assert detail["configured_from"].endswith("monitoring.slo.risk")
        # dedup_key 前缀 limit:<dimension>、category=risk
        assert warns[0]["deduplication_key"].startswith("limit:concentration")
        assert warns[0]["category"] == "risk"

    def test_usage_097_触发ERROR(self) -> None:
        am = _FakeAlert()
        mon = _make_monitor(am)
        acct, pos = _snap(291_000.0)  # usage=0.97
        result = mon.check(account=acct, positions=pos)
        errs = [c for c in am.calls if c["level"] == SeverityLevel.ERROR]
        assert len(errs) >= 1
        assert result["fired"][DIM_CONCENTRATION] == "ERROR"
        assert errs[0]["deduplication_key"].endswith(":error")

    def test_边界_095_为WARNING而非ERROR(self) -> None:
        """语义以 >95% 为强告警：usage==0.95 仍 WARNING，不升 ERROR。"""
        am = _FakeAlert()
        mon = _make_monitor(am)
        # max_single=285000 → exposure=0.285 → usage=0.95
        acct, pos = _snap(285_000.0)
        mon.check(account=acct, positions=pos)
        errs = [c for c in am.calls if c["level"] == SeverityLevel.ERROR]
        warns = [c for c in am.calls if c["level"] == SeverityLevel.WARNING]
        assert errs == []
        assert len(warns) >= 1


# ─────────────────────────────────────────────
#  阈值真源断言：行为跟随 slo.risk 配置
# ─────────────────────────────────────────────
class TestConfigSource:
    """阈值/限额均来自 slo.risk（R-04）；未显式注入时读配置。"""

    def test_构造不同slo配置行为跟随(self, monkeypatch) -> None:
        """
        monkeypatch get_monitoring_config 返回不同 slo.risk：
        warn=0.5 / alert=0.8，concentration_limit=0.20 →
        同一快照 usage=0.75 应触发 ERROR（按配置阈值判定，而非硬编码 0.8/0.95）。
        """
        fake_cfg = {
            "slo": {
                "risk": {
                    "position_limit_usage_warn": 0.5,
                    "position_limit_usage_alert": 0.8,
                    "concentration_limit": 0.20,
                    "leverage_limit": 1.0,
                    "single_stock_position_limit": 0.20,
                }
            }
        }
        monkeypatch.setattr("myquant.risk.rules.get_monitoring_config", lambda: fake_cfg)
        am = _FakeAlert()
        mon = LimitUsageMonitor(alert_manager=am)  # 不显式传阈值/限额 → 全读配置
        assert mon._warn == 0.5
        assert mon._alert == 0.8
        assert mon._limits[DIM_CONCENTRATION] == 0.20
        # max_single=150000 → exposure=0.15 → usage=0.15/0.20=0.75 > 0.8? 否；0.75>=0.5 且 <=0.8 → WARNING
        acct, pos = _snap(150_000.0)
        result = mon.check(account=acct, positions=pos)
        assert result["fired"][DIM_CONCENTRATION] == "WARNING"
        # usage=0.75 > alert(0.8)? 否 → 不 ERROR
        errs = [c for c in am.calls if c["level"] == SeverityLevel.ERROR]
        assert errs == []

    def test_配置值_09_触发ERROR(self, monkeypatch) -> None:
        """同配置下 usage=0.90 > alert(0.8) → ERROR。"""
        fake_cfg = {
            "slo": {
                "risk": {
                    "position_limit_usage_warn": 0.5,
                    "position_limit_usage_alert": 0.8,
                    "concentration_limit": 0.20,
                    "leverage_limit": 1.0,
                    "single_stock_position_limit": 0.20,
                }
            }
        }
        monkeypatch.setattr("myquant.risk.rules.get_monitoring_config", lambda: fake_cfg)
        am = _FakeAlert()
        mon = LimitUsageMonitor(alert_manager=am)
        # max_single=180000 → exposure=0.18 → usage=0.18/0.20=0.90 > 0.8
        acct, pos = _snap(180_000.0)
        result = mon.check(account=acct, positions=pos)
        assert result["fired"][DIM_CONCENTRATION] == "ERROR"

    def test_显式注入优先于配置(self, monkeypatch) -> None:
        """显式构造参数优先于 slo 配置。"""
        fake_cfg = {"slo": {"risk": {"position_limit_usage_warn": 0.5,
                                     "concentration_limit": 0.20}}}
        monkeypatch.setattr("myquant.risk.rules.get_monitoring_config", lambda: fake_cfg)
        am = _FakeAlert()
        mon = LimitUsageMonitor(warn_threshold=0.80, alert_threshold=0.95,
                                concentration_limit=0.30,
                                leverage_limit=1.0, single_stock_limit=0.30,
                                alert_manager=am)
        assert mon._warn == 0.80  # 显式覆盖配置 0.5
        acct, pos = _snap(150_000.0)  # usage=0.5 < 0.8 → 不告警
        result = mon.check(account=acct, positions=pos)
        assert am.calls == []
        assert result["fired"] == {}


# ─────────────────────────────────────────────
#  未配置维度：limit=None → 不告警、不编造
# ─────────────────────────────────────────────
class TestDisabledDimension:
    """PENDING_CAL 维度（限额=None）→ 未启用：不告警、指标 enabled=false。"""

    def test_未配置维度不告警(self) -> None:
        am = _FakeAlert()
        mon = _make_monitor(am, concentration_limit=None, single_stock_limit=None)
        acct, pos = _snap(291_000.0)  # 集中度/单票本会超 0.97
        result = mon.check(account=acct, positions=pos)
        conc_calls = [c for c in am.calls if c["details"]["dimension"] == DIM_CONCENTRATION]
        single_calls = [c for c in am.calls if c["details"]["dimension"] == DIM_SINGLE_STOCK]
        assert conc_calls == []
        assert single_calls == []
        assert result["enabled"][DIM_CONCENTRATION] is False
        assert result["enabled"][DIM_SINGLE_STOCK] is False
        assert result["usages"][DIM_CONCENTRATION] is None
        assert result["usages"][DIM_SINGLE_STOCK] is None

    def test_未配置不编造数值(self) -> None:
        """未配置维度限额=None，状态查询不得编造阈值数值。"""
        mon = LimitUsageMonitor(concentration_limit=None, leverage_limit=None,
                                single_stock_limit=None)
        status = mon.get_status()
        assert status["limits"][DIM_CONCENTRATION] is None
        assert status["limits"][DIM_LEVERAGE] is None
        assert status["limits"][DIM_SINGLE_STOCK] is None
        assert status["enabled"][DIM_CONCENTRATION] is False

    def test_非法限额回退未启用(self) -> None:
        """slo 限额为 ~ 或非数 → 视为未启用，不抛错不编造。"""
        mon = LimitUsageMonitor(concentration_limit=0.0, leverage_limit=-1.0,
                                single_stock_limit=None)
        assert mon._limits[DIM_CONCENTRATION] is None
        assert mon._limits[DIM_LEVERAGE] is None

    def test_仅启用杠杆维度(self) -> None:
        """只配 leverage_limit 时，仅杠杆维度参与告警。"""
        am = _FakeAlert()
        mon = _make_monitor(am, concentration_limit=None, single_stock_limit=None,
                            leverage_limit=0.30)
        # market_value=max_single+10万；max_single=20万 → 总市值=30万，equity=100万
        # leverage exposure=0.30 → usage=0.30/0.30=1.0 > 0.95 → ERROR
        acct, pos = _snap(200_000.0)
        result = mon.check(account=acct, positions=pos)
        assert result["fired"][DIM_LEVERAGE] == "ERROR"


# ─────────────────────────────────────────────
#  指标写入 collector 可查
# ─────────────────────────────────────────────
class TestMetrics:
    """set_gauge 写入 MetricsCollector，get_all 可见。"""

    def test_真实collector写入后get_all可查(self) -> None:
        am = _FakeAlert()
        col = MetricsCollector()
        mon = _make_monitor(am, met=col)
        acct, pos = _snap(255_000.0)  # usage=0.85
        mon.check(account=acct, positions=pos)
        names = {m.name for m in col.get_all()}
        assert METRIC_LIMIT_USAGE[DIM_CONCENTRATION] in names
        assert METRIC_LIMIT_USAGE[DIM_LEVERAGE] in names
        assert METRIC_LIMIT_USAGE[DIM_SINGLE_STOCK] in names
        conc = [m for m in col.get_all() if m.name == METRIC_LIMIT_USAGE[DIM_CONCENTRATION]]
        assert conc and conc[0].value == pytest.approx(0.85, abs=1e-3)

    def test_未启用维度指标enabled_false(self) -> None:
        am = _FakeAlert()
        col = MetricsCollector()
        mon = _make_monitor(am, met=col, concentration_limit=None)
        acct, pos = _snap(255_000.0)
        mon.check(account=acct, positions=pos)
        conc = [m for m in col.get_all() if m.name == METRIC_LIMIT_USAGE[DIM_CONCENTRATION]]
        assert conc
        # 未启用：value=0.0 + labels enabled=false（输出未配置语义）
        assert conc[0].value == 0.0
        assert conc[0].labels.get("enabled") == "false"

    def test_指标注册清单(self) -> None:
        reg = get_limit_usage_metrics_registration()
        assert len(reg) == 3
        names = {r["name"] for r in reg}
        assert names == set(METRIC_LIMIT_USAGE.values())
        assert all(r["type"] == "GAUGE" for r in reg)
        assert all("dimension" in r["labels"] for r in reg)


# ─────────────────────────────────────────────
#  快照源装配 / 防未来 / 防御式
# ─────────────────────────────────────────────
class TestSnapshotAndDefensive:
    """getter 装配、空快照防御、防未来。"""

    def test_getter快照源自动取用(self) -> None:
        acct, pos = _snap(255_000.0)

        class _PB:
            def get_account(self):
                return acct

            def get_positions_snapshot(self):
                return pos

        pb = _PB()
        am = _FakeAlert()
        mon = _make_monitor(am)
        mon.attach_snapshot_source(pb.get_account, pb.get_positions_snapshot)
        result = mon.check()  # 不传快照 → 自动调 getter
        assert result["fired"][DIM_CONCENTRATION] == "WARNING"

    def test_空账户不编造不告警(self) -> None:
        """总资产=0/空持仓 → exposure=0，不告警、不除零。"""
        am = _FakeAlert()
        mon = _make_monitor(am)
        result = mon.check(account={"total_assets": 0.0, "market_value": 0.0, "equity": 0.0},
                           positions=[])
        assert am.calls == []
        assert result["fired"] == {}

    def test_持仓dict缺市值用数量价格兜底(self) -> None:
        """持仓无 market_value 字段时，用 |quantity|*market_price 兜底。"""
        am = _FakeAlert()
        mon = _make_monitor(am)
        account = {"total_assets": 1_000_000.0, "market_value": 355_000.0, "equity": 1_000_000.0}
        positions = [
            {"symbol": "S1", "quantity": 1000.0, "market_price": 255.0},  # =255000
            {"symbol": "S2", "quantity": 1000.0, "market_price": 100.0},  # =100000
        ]
        result = mon.check(account=account, positions=positions)
        # max_single=255000 → usage=0.85 → WARNING
        assert result["fired"][DIM_CONCENTRATION] == "WARNING"

    def test_非法阈值抛ValueError(self) -> None:
        raised = False
        try:
            LimitUsageMonitor(warn_threshold=0.95, alert_threshold=0.80)
        except ValueError:
            raised = True
        assert raised is True

    def test_防未来只消费传入快照(self) -> None:
        """相同快照两次 check 结果一致（不依赖墙钟/未来数据）。"""
        am = _FakeAlert()
        mon = _make_monitor(am)
        acct, pos = _snap(255_000.0)
        r1 = mon.check(account=acct, positions=pos)
        am.calls.clear()
        r2 = mon.check(account=acct, positions=pos)
        assert r1["usages"] == r2["usages"]
        # 同一快照重复判定 fired 一致（幂等，不随时间漂移）
        assert r1["fired"] == r2["fired"]
