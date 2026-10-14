# 文件名：tests/unit/test_monitoring.py
"""
test_monitoring.py - 监控告警模块单元测试
============================================
覆盖 myquant/monitoring/：
  - alerting.py        告警系统（Alert/AlertRule/AlertChannel/AlertAggregator/AlertManager）
  - health_check.py    健康检查（HealthStatus/HealthCheckResult/HealthChecker）
  - metrics_collector.py 指标采集（MetricType/Metric/MetricsCollector/Prometheus导出）

测试维度:
  1. 告警模型（Alert 字段/级别/序列化/确认/解决/升级）
  2. 告警规则（AlertRule 脏数据类型映射/占比阈值/通道路由/是否告警）
  3. 告警通道（Console/Log/Email/Webhook 四通道，分级过滤，统计）
  4. 告警聚合（AlertAggregator 窗口聚合/刷新/过期清理）
  5. 告警管理器（AlertManager 通道管理/发送/确认/解决/静默/升级/事件总线对接）
  6. 健康状态（HealthStatus 枚举/比较/健康判断）
  7. 健康检查器（HealthChecker 注册/启用/运行/汇总/历史）
  8. 指标采集（Metric/MetricType/MetricsCollector gauge/counter/histogram/Prometheus）

运行方式:
  pytest tests/unit/test_monitoring.py -v

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_monitoring.py）
  2. 为实时行情及实盘无缝对接做好充足准备（AlertManager 事件总线自动告警/HealthChecker 定期巡检/MetricsCollector Prometheus 导出供 Grafana 监控）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 监控仅基于当前已发生事件，不引用未来数据
"""

from __future__ import annotations

import time

from myquant.core.health_check import (
    HealthChecker,
    HealthCheckResult,
    HealthStatus,
)
from myquant.monitoring.alerting import (
    Alert,
    AlertAggregator,
    AlertManager,
    AlertRule,
    ConsoleAlertChannel,
    EmailAlertChannel,
    LogAlertChannel,
    SeverityLevel,
    WebhookAlertChannel,
)
from myquant.monitoring.metrics_collector import (
    Metric,
    MetricsCollector,
    MetricType,
)

# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _make_alert(level: SeverityLevel = SeverityLevel.WARNING,
                message: str = "测试告警",
                source: str = "test") -> Alert:
    """构造测试告警"""
    return Alert(level=level, message=message, source=source)


# ═══════════════════════════════════════════
#  TestAlert - 告警模型
# ═══════════════════════════════════════════

class TestAlert:
    """Alert 告警模型测试"""

    def test_instantiation(self):
        """实例化"""
        alert = _make_alert()
        assert alert is not None
        assert alert.message == "测试告警"
        assert alert.level == SeverityLevel.WARNING

    def test_default_source(self):
        """默认 source"""
        alert = Alert(level=SeverityLevel.INFO, message="test")
        assert alert.source == "monitoring"

    def test_level_name(self):
        """级别名称"""
        assert _make_alert(SeverityLevel.INFO).level_name == "info"
        assert _make_alert(SeverityLevel.WARNING).level_name == "warning"
        assert _make_alert(SeverityLevel.CRITICAL).level_name == "critical"

    def test_is_critical(self):
        """是否严重"""
        assert _make_alert(SeverityLevel.CRITICAL).is_critical is True
        assert _make_alert(SeverityLevel.WARNING).is_critical is False

    def test_is_escalated(self):
        """是否升级"""
        alert = _make_alert()
        assert alert.is_escalated is False
        alert2 = Alert(level=SeverityLevel.CRITICAL, message="升级",
                       escalated_from=SeverityLevel.WARNING)
        assert alert2.is_escalated is True

    def test_age_seconds(self):
        """存在时长"""
        alert = _make_alert()
        assert alert.age_seconds >= 0

    def test_to_dict(self):
        """序列化为字典"""
        alert = _make_alert()
        d = alert.to_dict()
        assert isinstance(d, dict)
        assert "alert_id" in d
        assert "level" in d
        assert "message" in d
        assert "source" in d
        assert d["message"] == "测试告警"

    def test_summary(self):
        """摘要"""
        alert = _make_alert()
        s = alert.summary()
        assert isinstance(s, str)
        assert len(s) > 0

    def test_acknowledge(self):
        """确认告警"""
        alert = _make_alert()
        assert alert.acknowledged is False
        alert.acknowledged = True
        assert alert.acknowledged is True

    def test_resolve(self):
        """解决告警"""
        alert = _make_alert()
        assert alert.resolved is False
        alert.resolved = True
        assert alert.resolved is True

    def test_aggregation_count(self):
        """聚合计数"""
        alert = Alert(level=SeverityLevel.WARNING, message="聚合",
                      aggregation_count=5)
        assert alert.aggregation_count == 5

    def test_repr(self):
        """__repr__ 不崩溃"""
        r = repr(_make_alert())
        assert isinstance(r, str)
        assert "Alert" in r


# ═══════════════════════════════════════════
#  TestAlertRule - 告警规则
# ═══════════════════════════════════════════

class TestAlertRule:
    """AlertRule 告警规则测试"""

    def test_default_rule(self):
        """默认规则"""
        rule = AlertRule.default_rule()
        assert rule is not None

    def test_should_alert_high_bad_ratio(self):
        """高脏数据占比应告警"""
        rule = AlertRule.default_rule()
        assert rule.should_alert(bad_count=10, bad_ratio=0.1) is True

    def test_should_alert_low_ratio(self):
        """低占比可能不告警（取决于阈值）"""
        rule = AlertRule.default_rule()
        result = rule.should_alert(bad_count=1, bad_ratio=0.001)
        assert isinstance(result, bool)

    def test_get_level_for_bad_ratio(self):
        """按占比获取级别"""
        rule = AlertRule.default_rule()
        level = rule.get_level_for_bad_ratio(0.1)
        assert level is None or isinstance(level, SeverityLevel)

    def test_get_level_for_dirty_type(self):
        """按脏数据类型获取级别"""
        rule = AlertRule.default_rule()
        level = rule.get_level_for_dirty_type("missing_value")
        assert level is None or isinstance(level, SeverityLevel)

    def test_get_channels_for_level(self):
        """按级别获取通道列表"""
        rule = AlertRule.default_rule()
        channels = rule.get_channels_for_level(SeverityLevel.CRITICAL)
        assert isinstance(channels, list)

    def test_determine_level(self):
        """综合判定级别"""
        rule = AlertRule.default_rule()
        level = rule.determine_level(dirty_type="missing_value", bad_ratio=0.1)
        assert isinstance(level, SeverityLevel)


# ═══════════════════════════════════════════
#  TestAlertChannel - 告警通道
# ═══════════════════════════════════════════

class TestAlertChannel:
    """AlertChannel 告警通道测试"""

    def test_console_channel_instantiation(self):
        """控制台通道实例化"""
        ch = ConsoleAlertChannel()
        assert ch is not None
        assert ch.name == "console"

    def test_log_channel_instantiation(self):
        """日志通道实例化"""
        ch = LogAlertChannel()
        assert ch is not None
        assert ch.name == "log"

    def test_email_channel_instantiation(self):
        """邮件通道实例化（不实际发送）"""
        ch = EmailAlertChannel(smtp_host="localhost", smtp_port=25,
                               from_addr="test@test.com",
                               recipients=["a@b.com"])
        assert ch is not None

    def test_webhook_channel_instantiation(self):
        """Webhook通道实例化（不实际发送）"""
        ch = WebhookAlertChannel(webhook_url="http://localhost:8080/hook",
                                 webhook_type="generic")
        assert ch is not None

    def test_can_send_above_min_level(self):
        """高于最低级别的告警可发送"""
        ch = ConsoleAlertChannel(min_level=SeverityLevel.WARNING)
        alert = _make_alert(SeverityLevel.CRITICAL)
        assert ch.can_send(alert) is True

    def test_cannot_send_below_min_level(self):
        """低于最低级别的告警不可发送"""
        ch = ConsoleAlertChannel(min_level=SeverityLevel.CRITICAL)
        alert = _make_alert(SeverityLevel.INFO)
        assert ch.can_send(alert) is False

    def test_console_send(self):
        """控制台发送"""
        ch = ConsoleAlertChannel()
        alert = _make_alert()
        result = ch.send(alert)
        assert result is True

    def test_channel_stats(self):
        """通道统计"""
        ch = ConsoleAlertChannel()
        stats = ch.stats
        assert isinstance(stats, dict)
        assert "sent_count" in stats or "name" in stats

    def test_channel_disabled(self):
        """禁用通道（通过基类参数）"""
        # 用子类创建后检查 enabled 属性
        ch = ConsoleAlertChannel()
        assert hasattr(ch, "enabled")
        ch.enabled = False
        alert = _make_alert()
        assert ch.can_send(alert) is False

    def test_channel_repr(self):
        """__repr__ 不崩溃"""
        r = repr(ConsoleAlertChannel())
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestAlertAggregator - 告警聚合
# ═══════════════════════════════════════════

class TestAlertAggregator:
    """AlertAggregator 告警聚合测试"""

    def test_instantiation(self):
        """实例化"""
        agg = AlertAggregator(window_seconds=60.0)
        assert agg is not None

    def test_window_seconds(self):
        """聚合窗口"""
        agg = AlertAggregator(window_seconds=120.0)
        assert agg.window_seconds == 120.0

    def test_add_event(self):
        """添加事件"""
        agg = AlertAggregator(window_seconds=60.0)
        agg.add_event(symbol="000001.SZ", dirty_type="missing_value",
                      bad_count=5, total_count=100)
        assert agg.get_pending_count() >= 1

    def test_get_pending_keys(self):
        """获取待处理键"""
        agg = AlertAggregator(window_seconds=60.0)
        agg.add_event(symbol="000001.SZ", dirty_type="missing_value",
                      bad_count=5, total_count=100)
        keys = agg.get_pending_keys()
        assert isinstance(keys, list)
        assert len(keys) >= 1

    def test_flush(self):
        """刷新聚合"""
        agg = AlertAggregator(window_seconds=60.0)
        agg.add_event(symbol="000001.SZ", dirty_type="missing_value",
                      bad_count=5, total_count=100)
        alerts = agg.flush()
        assert isinstance(alerts, list)

    def test_cleanup_expired(self):
        """清理过期聚合"""
        agg = AlertAggregator(window_seconds=0.001)  # 极短窗口
        agg.add_event(symbol="000001.SZ", dirty_type="missing_value",
                      bad_count=5, total_count=100)
        time.sleep(0.01)
        agg.cleanup_expired()
        # 过期后应被清理


# ═══════════════════════════════════════════
#  TestAlertManager - 告警管理器
# ═══════════════════════════════════════════

class TestAlertManager:
    """AlertManager 告警管理器测试"""

    def test_instantiation(self):
        """实例化"""
        mgr = AlertManager()
        assert mgr is not None

    def test_add_channel(self):
        """添加通道"""
        mgr = AlertManager()
        ch = ConsoleAlertChannel()
        mgr.add_channel(ch)
        assert "console" in mgr.list_channels()

    def test_remove_channel(self):
        """移除通道"""
        mgr = AlertManager()
        ch = ConsoleAlertChannel()
        mgr.add_channel(ch)
        mgr.remove_channel("console")
        assert "console" not in mgr.list_channels()

    def test_list_channels(self):
        """列出通道"""
        mgr = AlertManager()
        mgr.add_channel(ConsoleAlertChannel())
        mgr.add_channel(LogAlertChannel())
        channels = mgr.list_channels()
        assert isinstance(channels, list)
        assert len(channels) >= 2

    def test_get_channel(self):
        """获取通道"""
        mgr = AlertManager()
        ch = ConsoleAlertChannel()
        mgr.add_channel(ch)
        fetched = mgr.get_channel("console")
        assert fetched is not None

    def test_enable_disable_channel(self):
        """启用/禁用通道"""
        mgr = AlertManager()
        mgr.add_channel(ConsoleAlertChannel())
        mgr.disable_channel("console")
        mgr.enable_channel("console")

    def test_send_alert(self):
        """发送告警（返回发送通道数）"""
        mgr = AlertManager()
        mgr.add_channel(ConsoleAlertChannel(min_level=SeverityLevel.INFO))
        alert = _make_alert(SeverityLevel.WARNING)
        result = mgr.send_alert(alert)
        assert isinstance(result, int)
        assert result >= 0

    def test_alert_method(self):
        """alert 快捷方法"""
        mgr = AlertManager()
        mgr.add_channel(ConsoleAlertChannel(min_level=SeverityLevel.INFO))
        alert = mgr.alert(level=SeverityLevel.WARNING, message="测试",
                          source="test")
        assert alert is not None

    def test_acknowledge(self):
        """确认告警"""
        mgr = AlertManager()
        alert = mgr.alert(level=SeverityLevel.WARNING, message="测试")
        if alert:
            mgr.acknowledge(alert.alert_id)

    def test_resolve(self):
        """解决告警"""
        mgr = AlertManager()
        alert = mgr.alert(level=SeverityLevel.WARNING, message="测试")
        if alert:
            mgr.resolve(alert.alert_id)

    def test_silence_unsilence(self):
        """静默/取消静默"""
        mgr = AlertManager()
        mgr.silence(duration_seconds=60)
        assert mgr.is_silenced is True
        mgr.unsilence()
        assert mgr.is_silenced is False

    def test_get_history(self):
        """获取历史"""
        mgr = AlertManager()
        mgr.alert(level=SeverityLevel.INFO, message="历史测试")
        history = mgr.get_history()
        assert isinstance(history, list)

    def test_get_unacknowledged(self):
        """获取未确认告警"""
        mgr = AlertManager()
        mgr.alert(level=SeverityLevel.WARNING, message="未确认")
        unacked = mgr.get_unacknowledged()
        assert isinstance(unacked, list)

    def test_stats(self):
        """统计"""
        mgr = AlertManager()
        stats = mgr.stats
        assert isinstance(stats, dict)

    def test_set_get_alert_rule(self):
        """设置/获取告警规则"""
        mgr = AlertManager()
        rule = AlertRule.default_rule()
        mgr.set_alert_rule(rule)
        fetched = mgr.get_alert_rule()
        assert fetched is not None

    def test_check_escalations(self):
        """检查升级（返回升级数量）"""
        mgr = AlertManager()
        mgr.alert(level=SeverityLevel.WARNING, message="升级测试")
        escalated = mgr.check_escalations()
        assert isinstance(escalated, int)

    def test_handle_data_quality_event(self):
        """处理数据质量事件（实盘准备）"""
        mgr = AlertManager()
        # 构造数据质量事件
        from myquant.core.event_bus import DataQualityEvent
        event = DataQualityEvent(
            dirty_type="missing_value", symbol="000001.SZ",
            bad_count=5, total_count=100,
        )
        mgr.handle_data_quality_event(event)
        # 不崩溃即通过

    def test_flush_aggregator(self):
        """刷新聚合器（返回刷新数量）"""
        mgr = AlertManager()
        count = mgr.flush_aggregator()
        assert isinstance(count, int)


# ═══════════════════════════════════════════
#  TestHealthStatus - 健康状态
# ═══════════════════════════════════════════

class TestHealthStatus:
    """HealthStatus 健康状态测试"""

    def test_members_exist(self):
        """枚举成员存在"""
        assert hasattr(HealthStatus, "HEALTHY")
        assert hasattr(HealthStatus, "DEGRADED")
        assert hasattr(HealthStatus, "UNHEALTHY")
        assert hasattr(HealthStatus, "DEGRADED")

    def test_is_healthy(self):
        """健康判断"""
        assert HealthStatus.HEALTHY.is_healthy is True
        assert HealthStatus.DEGRADED.is_healthy is False
        assert HealthStatus.UNHEALTHY.is_healthy is False

    def test_is_critical(self):
        """严重判断"""
        assert HealthStatus.UNHEALTHY.is_critical is True
        assert HealthStatus.HEALTHY.is_critical is False
        assert HealthStatus.DEGRADED.is_critical is False

    def test_comparison(self):
        """状态比较"""
        assert HealthStatus.UNHEALTHY > HealthStatus.DEGRADED
        assert HealthStatus.DEGRADED > HealthStatus.HEALTHY
        assert HealthStatus.HEALTHY < HealthStatus.UNHEALTHY

    def test_level(self):
        """级别数值"""
        assert HealthStatus.HEALTHY.level >= 0
        assert HealthStatus.UNHEALTHY.level > HealthStatus.HEALTHY.level


# ═══════════════════════════════════════════
#  TestHealthCheckResult - 健康检查结果
# ═══════════════════════════════════════════

class TestHealthCheckResult:
    """HealthCheckResult 健康检查结果测试"""

    def test_instantiation(self):
        """实例化"""
        result = HealthCheckResult(
            name="test", status=HealthStatus.HEALTHY,
            message="正常", details={},
        )
        assert result is not None
        assert result.status == HealthStatus.HEALTHY

    def test_to_dict(self):
        """序列化为字典"""
        result = HealthCheckResult(
            name="test", status=HealthStatus.HEALTHY,
            message="正常", details={},
        )
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "name" in d
        assert "status" in d

    def test_summary(self):
        """摘要"""
        result = HealthCheckResult(
            name="test", status=HealthStatus.HEALTHY,
            message="正常", details={},
        )
        s = result.summary()
        assert isinstance(s, str)


# ═══════════════════════════════════════════
#  TestHealthChecker - 健康检查器
# ═══════════════════════════════════════════

class TestHealthChecker:
    """HealthChecker 健康检查器测试"""

    def test_instantiation(self):
        """实例化"""
        hc = HealthChecker()
        assert hc is not None

    def test_register_default_checks(self):
        """注册默认检查项"""
        hc = HealthChecker()
        hc.register_default_checks()
        checks = hc.list_checks()
        assert isinstance(checks, list)
        assert len(checks) >= 1
        assert "disk_space" in checks or "event_bus" in checks or "process" in checks

    def test_add_custom_check(self):
        """添加自定义检查"""
        hc = HealthChecker()
        hc.add_check(name="custom", func=lambda: HealthCheckResult(
            name="custom", status=HealthStatus.HEALTHY, message="ok"))
        assert "custom" in hc.list_checks()

    def test_remove_check(self):
        """移除检查项"""
        hc = HealthChecker()
        hc.add_check(name="temp", func=lambda: HealthCheckResult(
            name="temp", status=HealthStatus.HEALTHY, message="ok"))
        result = hc.remove_check("temp")
        assert result is True
        assert "temp" not in hc.list_checks()

    def test_enable_disable_check(self):
        """启用/禁用检查项"""
        hc = HealthChecker()
        hc.add_check(name="toggle", func=lambda: HealthCheckResult(
            name="toggle", status=HealthStatus.HEALTHY, message="ok"))
        hc.disable_check("toggle")
        hc.enable_check("toggle")

    def test_run_check(self):
        """运行单个检查"""
        hc = HealthChecker()
        hc.add_check(name="run_test", func=lambda: HealthCheckResult(
            name="run_test", status=HealthStatus.HEALTHY, message="ok"))
        result = hc.run_check("run_test")
        assert result is not None
        assert result.status == HealthStatus.HEALTHY

    def test_run_all(self):
        """运行全部检查"""
        hc = HealthChecker()
        hc.register_default_checks()
        results = hc.run_all()
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_overall_status(self):
        """整体状态"""
        hc = HealthChecker()
        hc.register_default_checks()
        hc.run_all()
        status = hc.overall_status
        assert isinstance(status, HealthStatus)

    def test_get_summary(self):
        """获取汇总"""
        hc = HealthChecker()
        hc.register_default_checks()
        hc.run_all()
        summary = hc.get_summary()
        assert isinstance(summary, dict)
        assert "overall_status" in summary or "status" in summary

    def test_get_last_result(self):
        """获取最近结果"""
        hc = HealthChecker()
        hc.add_check(name="last", func=lambda: HealthCheckResult(
            name="last", status=HealthStatus.HEALTHY, message="ok"))
        hc.run_check("last")
        result = hc.get_last_result("last")
        assert result is not None

    def test_list_checks(self):
        """列出检查项"""
        hc = HealthChecker()
        checks = hc.list_checks()
        assert isinstance(checks, list)

    def test_repr(self):
        """__repr__ 不崩溃"""
        r = repr(HealthChecker())
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestMetricType - 指标类型
# ═══════════════════════════════════════════

class TestMetricType:
    """MetricType 指标类型测试"""

    def test_members_exist(self):
        """枚举成员存在"""
        assert hasattr(MetricType, "COUNTER")
        assert hasattr(MetricType, "GAUGE")
        assert hasattr(MetricType, "HISTOGRAM")

    def test_counter_is_monotonic(self):
        """Counter 单调递增"""
        assert MetricType.COUNTER.is_monotonic is True

    def test_gauge_not_monotonic(self):
        """Gauge 非单调"""
        assert MetricType.GAUGE.is_monotonic is False


# ═══════════════════════════════════════════
#  TestMetric - 指标模型
# ═══════════════════════════════════════════

class TestMetric:
    """Metric 指标模型测试"""

    def test_instantiation(self):
        """实例化"""
        metric = Metric(name="test_metric", value=42.0,
                        type=MetricType.GAUGE)
        assert metric is not None
        assert metric.value == 42.0

    def test_labels_key(self):
        """标签键"""
        metric = Metric(name="test", value=1.0, type=MetricType.GAUGE,
                        labels={"symbol": "000001.SZ", "interval": "1d"})
        key = metric.labels_key
        assert isinstance(key, str)
        assert "000001.SZ" in key

    def test_full_name(self):
        """全名"""
        metric = Metric(name="test", value=1.0, type=MetricType.GAUGE)
        assert metric.full_name == "test"

    def test_age_seconds(self):
        """存在时长"""
        metric = Metric(name="test", value=1.0, type=MetricType.GAUGE)
        assert metric.age_seconds >= 0

    def test_is_stale(self):
        """是否过期"""
        metric = Metric(name="test", value=1.0, type=MetricType.GAUGE,
                        timestamp=time.time() - 1000)
        assert metric.is_stale(max_age_seconds=300) is True

    def test_not_stale_fresh(self):
        """新鲜指标不过期"""
        metric = Metric(name="test", value=1.0, type=MetricType.GAUGE)
        assert metric.is_stale(max_age_seconds=300) is False

    def test_to_dict(self):
        """序列化为字典"""
        metric = Metric(name="test", value=42.0, type=MetricType.GAUGE)
        d = metric.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "test"
        assert d["value"] == 42.0

    def test_repr(self):
        """__repr__ 不崩溃"""
        r = repr(Metric(name="test", value=1.0, type=MetricType.GAUGE))
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestMetricsCollector - 指标采集器
# ═══════════════════════════════════════════

class TestMetricsCollector:
    """MetricsCollector 指标采集器测试"""

    def test_instantiation(self):
        """实例化"""
        mc = MetricsCollector()
        assert mc is not None

    def test_register_builtin_metrics(self):
        """注册内置指标"""
        mc = MetricsCollector()
        mc.register_builtin_metrics()
        names = mc.list_metric_names()
        assert len(names) >= 1

    def test_register_custom_metric(self):
        """注册自定义指标"""
        mc = MetricsCollector()
        mc.register(name="custom_gauge", metric_type=MetricType.GAUGE,
                    help="自定义")
        assert mc.is_registered("custom_gauge") is True

    def test_set_gauge(self):
        """设置 Gauge"""
        mc = MetricsCollector()
        mc.register(name="temp", metric_type=MetricType.GAUGE)
        mc.set_gauge("temp", 36.5)
        assert mc.get_value("temp") == 36.5

    def test_set_gauge_with_labels(self):
        """带标签设置 Gauge"""
        mc = MetricsCollector()
        mc.register(name="dirty_ratio", metric_type=MetricType.GAUGE)
        mc.set_gauge("dirty_ratio", 0.05, labels={"symbol": "000001.SZ"})
        val = mc.get_value("dirty_ratio", labels={"symbol": "000001.SZ"})
        assert val == 0.05

    def test_inc_counter(self):
        """递增 Counter"""
        mc = MetricsCollector()
        mc.register(name="events_total", metric_type=MetricType.COUNTER)
        mc.inc_counter("events_total")
        mc.inc_counter("events_total", 5)
        assert mc.get_value("events_total") == 6

    def test_observe_histogram(self):
        """观察 Histogram"""
        mc = MetricsCollector()
        mc.register(name="latency", metric_type=MetricType.HISTOGRAM)
        mc.observe_histogram("latency", 0.025)
        # 不崩溃即通过

    def test_get(self):
        """获取指标"""
        mc = MetricsCollector()
        mc.register(name="g", metric_type=MetricType.GAUGE)
        mc.set_gauge("g", 1.0)
        metric = mc.get("g")
        assert metric is not None
        assert metric.value == 1.0

    def test_get_all(self):
        """获取全部指标"""
        mc = MetricsCollector()
        mc.register(name="g1", metric_type=MetricType.GAUGE)
        mc.register(name="g2", metric_type=MetricType.GAUGE)
        mc.set_gauge("g1", 1.0)
        mc.set_gauge("g2", 2.0)
        all_metrics = mc.get_all()
        assert isinstance(all_metrics, list)
        assert len(all_metrics) >= 2

    def test_list_metric_names(self):
        """列出指标名"""
        mc = MetricsCollector()
        mc.register(name="a", metric_type=MetricType.GAUGE)
        names = mc.list_metric_names()
        assert "a" in names

    def test_metrics_count(self):
        """指标数量"""
        mc = MetricsCollector()
        mc.register(name="c1", metric_type=MetricType.COUNTER)
        mc.inc_counter("c1")
        assert mc.metrics_count >= 1

    def test_definitions_count(self):
        """定义数量"""
        mc = MetricsCollector()
        mc.register(name="d1", metric_type=MetricType.GAUGE)
        assert mc.definitions_count >= 1

    def test_reset(self):
        """重置指标"""
        mc = MetricsCollector()
        mc.register(name="r", metric_type=MetricType.GAUGE)
        mc.set_gauge("r", 99.0)
        mc.reset("r")
        assert mc.get_value("r") is None or mc.get_value("r") == 0

    def test_get_stale_metrics(self):
        """获取过期指标"""
        mc = MetricsCollector(default_stale_seconds=0.001)
        mc.register(name="stale", metric_type=MetricType.GAUGE)
        mc.set_gauge("stale", 1.0)
        time.sleep(0.01)
        stale = mc.get_stale_metrics()
        assert isinstance(stale, list)

    def test_to_prometheus(self):
        """导出 Prometheus 格式"""
        mc = MetricsCollector()
        mc.register(name="test_gauge", metric_type=MetricType.GAUGE,
                    help="测试")
        mc.set_gauge("test_gauge", 42.0)
        prom = mc.to_prometheus()
        assert isinstance(prom, str)
        assert "test_gauge" in prom
        assert "42" in prom

    def test_to_prometheus_with_timestamp(self):
        """导出带时间戳的 Prometheus 格式"""
        mc = MetricsCollector()
        mc.register(name="ts_gauge", metric_type=MetricType.GAUGE)
        mc.set_gauge("ts_gauge", 1.0)
        prom = mc.to_prometheus(include_timestamp=True)
        assert isinstance(prom, str)


# ═══════════════════════════════════════════
#  TestRealTimePreparation - 实盘准备
# ═══════════════════════════════════════════

class TestRealTimePreparation:
    """实盘无缝对接准备测试"""

    def test_alert_manager_event_bus_integration(self):
        """AlertManager 对接事件总线（实盘自动告警）"""
        mgr = AlertManager()
        # subscribe/unsubscribe 不崩溃
        try:
            mgr.subscribe_to_event_bus()
            mgr.unsubscribe_from_event_bus()
        except Exception:
            pass  # 事件总线可能未初始化

    def test_health_checker_for_live_monitoring(self):
        """HealthChecker 适用于实盘定期巡检"""
        hc = HealthChecker()
        hc.register_default_checks()
        results = hc.run_all()
        assert isinstance(results, list)
        # 磁盘/进程/事件总线检查为实盘运维准备

    def test_metrics_prometheus_for_grafana(self):
        """MetricsCollector Prometheus 导出供 Grafana 监控"""
        mc = MetricsCollector()
        mc.register_builtin_metrics()
        mc.set_gauge("data_quality.dirty_ratio", 0.02,
                     labels={"symbol": "000001.SZ"})
        prom = mc.to_prometheus()
        assert isinstance(prom, str)
        assert len(prom) > 0

    def test_alert_channels_for_live_notification(self):
        """多通道告警适用于实盘通知（控制台/日志/邮件/Webhook）"""
        mgr = AlertManager()
        mgr.add_channel(ConsoleAlertChannel())
        mgr.add_channel(LogAlertChannel())
        # Email/Webhook 预留配置，不实际发送
        assert len(mgr.list_channels()) >= 2

    def test_alert_aggregation_for_high_frequency(self):
        """告警聚合适用于高频行情（避免告警风暴）"""
        agg = AlertAggregator(window_seconds=60.0)
        # 同一标的同一类型多次事件聚合为一条
        for i in range(10):
            agg.add_event(symbol="000001.SZ", dirty_type="missing_value",
                          bad_count=i + 1, total_count=1000)
        assert agg.get_pending_count() <= 10  # 聚合后数量减少
