# 文件名：tests/unit/test_monitoring_config.py
"""
test_monitoring_config.py - BASE-3 监控配置集中化单元测试
===========================================================
覆盖：
  1. _build_monitoring_config：空配置→全默认 / 配置覆盖 / 非法值回退默认 /
     深拷贝不污染 DEFAULT_MONITORING
  2. get_monitoring_config：从真实 config/settings.yaml 读取（BASE-3 验收：
     slo 块与 P0-2 锁定表关键 LOCKED 值一致；alerts 字段生效）
  3. _apply_alert_config：按 monitoring.alerts 装配 AlertManager
     （store_enabled / dedup_window / AlertRule 聚合升级参数）
  4. 配置读取异常 → 装配不阻断（告警主功能优先）

运行方式:
  pytest tests/unit/test_monitoring_config.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from myquant.core.settings import DEFAULT_MONITORING, _build_monitoring_config
from myquant.monitoring.alerting import AlertManager, configure_notification_channels


# ═══════════════════════════════════════════
#  1. _build_monitoring_config 纯函数
# ═══════════════════════════════════════════

class TestBuildMonitoringConfig:
    def test_empty_returns_defaults(self) -> None:
        cfg = _build_monitoring_config({})
        assert cfg["patrol_interval"] == DEFAULT_MONITORING["patrol_interval"]
        assert cfg["alerts"]["store_enabled"] is True
        assert cfg["alerts"]["dedup_window_seconds"] == 300.0
        assert cfg["watchdog"]["enabled"] is False

    def test_none_returns_defaults(self) -> None:
        cfg = _build_monitoring_config(None)  # type: ignore[arg-type]
        assert cfg["patrol_interval"] == 60

    def test_config_override_effective(self) -> None:
        cfg = _build_monitoring_config(
            {
                "patrol_interval": 120,
                "alerts": {
                    "store_enabled": False,
                    "dedup_window_seconds": 90.0,
                    "aggregation_window_seconds": 30.0,
                    "escalation_time_seconds": 600.0,
                    "escalation_enabled": False,
                },
                "watchdog": {"enabled": True, "bar_timeout_seconds": 600},
            }
        )
        assert cfg["patrol_interval"] == 120
        assert cfg["alerts"]["store_enabled"] is False
        assert cfg["alerts"]["dedup_window_seconds"] == 90.0
        assert cfg["alerts"]["aggregation_window_seconds"] == 30.0
        assert cfg["alerts"]["escalation_time_seconds"] == 600.0
        assert cfg["alerts"]["escalation_enabled"] is False
        assert cfg["watchdog"]["enabled"] is True
        assert cfg["watchdog"]["bar_timeout_seconds"] == 600.0
        # 未覆盖项仍取默认
        assert cfg["watchdog"]["session_grace_minutes"] == 5.0

    def test_invalid_values_fallback_to_default(self) -> None:
        cfg = _build_monitoring_config(
            {
                "patrol_interval": -5,
                "alerts": {"store_enabled": "not-bool", "dedup_window_seconds": "x"},
            }
        )
        assert cfg["patrol_interval"] == 60          # 负数回退
        assert cfg["alerts"]["store_enabled"] is True  # 非法布尔回退
        assert cfg["alerts"]["dedup_window_seconds"] == 300.0  # 非法数值回退

    def test_slo_non_dict_falls_back_empty(self) -> None:
        cfg = _build_monitoring_config({"slo": "bad"})
        assert cfg["slo"] == {}

    def test_defaults_not_polluted(self) -> None:
        """深拷贝隔离：构造配置不得污染 DEFAULT_MONITORING 常量"""
        _build_monitoring_config(
            {"alerts": {"store_enabled": False, "dedup_window_seconds": 999.0}}
        )
        assert DEFAULT_MONITORING["alerts"]["store_enabled"] is True
        assert DEFAULT_MONITORING["alerts"]["dedup_window_seconds"] == 300.0


# ═══════════════════════════════════════════
#  2. 真实 config/settings.yaml 验收（BASE-3）
# ═══════════════════════════════════════════

class TestRealSettingsYaml:
    def test_settings_yaml_loaded(self) -> None:
        """config/settings.yaml 存在且 monitoring 块被 settings 加载"""
        from myquant.core.settings import get_settings
        raw = get_settings().get_dict("monitoring", {})
        assert isinstance(raw, dict) and "alerts" in raw

    def test_slo_matches_p0_2_locked_values(self) -> None:
        """验收：slo 块与 P0-2 锁定表一致（LOCKED 项原样生效，R-04）"""
        from myquant.core.settings import get_monitoring_config
        slo = get_monitoring_config()["slo"]
        assert slo["data"]["freshness_max_seconds"] == 60     # 数据新鲜度 <1min LOCKED
        assert slo["data"]["gap_allowed"] == 0                # 数据缺口 =0 LOCKED
        assert slo["reconciliation"]["realtime_diff_tolerance"] == 0  # 对账容差 =0 LOCKED
        assert slo["reconciliation"]["t1_diff_tolerance"] == 0
        assert slo["risk"]["position_limit_usage_warn"] == 0.80
        assert slo["risk"]["position_limit_usage_alert"] == 0.95
        assert slo["engine_heartbeat"]["hang_judge_window_seconds"] == 60
        assert slo["engine_heartbeat"]["consecutive_misses"] == 5

    def test_alerts_defaults_from_yaml(self) -> None:
        from myquant.core.settings import get_monitoring_config
        alerts = get_monitoring_config()["alerts"]
        assert alerts["store_enabled"] is True
        assert alerts["dedup_window_seconds"] == 300.0
        assert alerts["aggregation_window_seconds"] == 60.0
        assert alerts["escalation_time_seconds"] == 300.0

    def test_patrol_interval_from_yaml(self) -> None:
        from myquant.core.settings import get_monitoring_config
        assert get_monitoring_config()["patrol_interval"] == 60


# ═══════════════════════════════════════════
#  3. AlertManager 装配集成（_apply_alert_config）
# ═══════════════════════════════════════════

class _FakeSettings:
    """伪装 get_settings：只暴露 get_dict/get 供配置读取"""

    def __init__(self, monitoring: dict, notification: dict):
        self._monitoring = monitoring
        self._notification = notification

    def get_dict(self, key: str, default=None):
        if key == "monitoring":
            return self._monitoring
        return default or {}

    def get(self, key: str, default=None):
        if key == "notification":
            return self._notification
        return default


class TestApplyAlertConfig:
    def test_apply_from_config(self, monkeypatch) -> None:
        """monitoring.alerts 装配：落库开关/去重窗口/AlertRule 三参数生效"""
        fake = _FakeSettings(
            monitoring={
                "alerts": {
                    "store_enabled": False,
                    "dedup_window_seconds": 120.0,
                    "aggregation_window_seconds": 30.0,
                    "escalation_time_seconds": 600.0,
                    "escalation_enabled": False,
                }
            },
            notification={"enabled": False, "channels": {}},
        )
        # get_monitoring_config 内部与 configure_notification_channels 内均从
        # myquant.core.settings 导入，patch 该模块属性即可（alerting 模块无 get_settings 属性）
        monkeypatch.setattr("myquant.core.settings.get_settings", lambda: fake)

        manager = AlertManager()
        configure_notification_channels(manager)

        assert manager._store_enabled is False          # 落库关闭
        assert manager._dedup_window == 120.0           # 去重窗口
        assert manager._alert_rule.aggregation_window == 30.0
        assert manager._alert_rule.escalation_time == 600.0
        assert manager._alert_rule.escalation_enabled is False

    def test_default_when_config_missing(self, monkeypatch) -> None:
        """monitoring 配置缺失 → 应用默认告警行为（dedup=300/落库开/默认规则）"""
        fake = _FakeSettings(monitoring={}, notification={})
        monkeypatch.setattr("myquant.core.settings.get_settings", lambda: fake)

        manager = AlertManager()
        configure_notification_channels(manager)
        assert manager._store_enabled is True
        assert manager._dedup_window == 300.0        # 默认去重窗口
        assert manager._alert_rule.aggregation_window == 60.0  # 默认规则保留

    def test_config_read_failure_does_not_block(self, monkeypatch) -> None:
        """配置读取异常 → 装配跳过，不阻断通道装配与告警功能"""
        def _boom(*args, **kwargs):
            raise RuntimeError("config down")

        monkeypatch.setattr("myquant.core.settings.get_monitoring_config", _boom)
        manager = AlertManager()
        configure_notification_channels(manager)  # 不应抛异常
        assert manager._store_enabled is True
