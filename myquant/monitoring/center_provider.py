# 文件名：myquant/monitoring/center_provider.py
"""
center_provider.py - 监控中心数据提供者
==========================================
A-11 监控中心数据提供者（MonitorCenterDataProvider）。

G1 EX-15A：自 myquant.web.dashboard 下沉至 monitoring（L5），消除 L5→L6 反向依赖；
web 层经 ``from myquant.web.dashboard import MonitorCenterDataProvider`` 保持 re-export 兼容。

聚合四类只读数据（全部消费"当前已发生"状态，防未来）：
  1. 健康总览（系统级 overall + 组件级 checks，复用 HealthChecker 报告结构）
  2. 告警列表（scope/level/category 筛选，走 AlertManager；details 脱敏）
  3. 实盘指标卡片（MetricsCollector 已采集指标）
  4. 交易连续状态（行情活性/引擎心跳/券商连接；指标缺失则 unknown，不伪造）

依赖全部可注入（测试用 fake）；未注入时懒加载 monitoring 全局单例。

依赖: typing, myquant.core.logger, myquant.core.constants（函数内）, myquant.monitoring（函数内）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("monitoring.center_provider")

# 告警列表默认返回条数
_DEFAULT_ALERT_LIMIT: int = 20


# ─────────────────────────────────────────────
#  A-11 敏感字段脱敏（P0-7 §3，看板/告警防泄露）
#  （G1 EX-15A：随 MonitorCenterDataProvider 自 web.dashboard 下沉）
# ─────────────────────────────────────────────

# 敏感字段名模式（命中即按 P0-7 规则脱敏；小写子串匹配）
_ACCOUNT_KEY_PATTERNS: tuple = ("account", "broker_account", "fund_account", "cash_account")
_CREDENTIAL_KEY_PATTERNS: tuple = (
    "api_key", "apikey", "secret", "token", "password", "passwd", "credential", "private_key",
)
_BALANCE_KEY_PATTERNS: tuple = (
    "balance", "equity", "capital", "position_value", "position_amount",
    "available_cash", "total_assets", "frozen_cash", "pnl",
)


def _lower_key(key: str) -> str:
    """归一化字段名（小写，去空白）。"""
    return (key or "").strip().lower()


def mask_sensitive_fields(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    告警详情敏感字段脱敏（P0-7 §3，看板/告警防泄露）。

    规则（对齐 docs/监控档案/安全合规.md §3）：
      - 券商账号类（account/broker_account...）：仅保留后 4 位
      - API 密钥/凭证类（api_key/secret/token/password...）：全部掩码 `****`
      - 资金/持仓类（balance/equity/capital/position_value...）：聚合展示，
        明细值掩码为 `****`（明细查看需更高权限，v1 看板默认脱敏视图）
      - 其余字段：原样透传

    防未来：脱敏只作用于当前已落库的告警 details，不产生任何预测。

    Parameters
    ----------
    details : dict | None
        告警 details 原始字典（None 返回空 dict）

    Returns
    -------
    dict : 脱敏后的新字典（不修改入参）
    """
    out: Dict[str, Any] = {}
    if not isinstance(details, dict):
        return out
    for key, value in details.items():
        k = _lower_key(str(key))
        if any(p in k for p in _CREDENTIAL_KEY_PATTERNS):
            out[key] = "****"
        elif any(p in k for p in _ACCOUNT_KEY_PATTERNS):
            s = str(value) if value is not None else ""
            out[key] = ("****" + s[-4:]) if len(s) >= 4 else "****"
        elif any(p in k for p in _BALANCE_KEY_PATTERNS):
            out[key] = "****"
        elif isinstance(value, dict):
            out[key] = mask_sensitive_fields(value)  # 嵌套字典递归脱敏
        else:
            out[key] = value
    return out


# 交易连续状态候选指标名子串（A-4/A-5/A-6 完成后消费其指标；未完成则 graceful=unknown）
_MARKET_ACTIVITY_HINTS: tuple = ("market_activity", "quote_recv", "tick_recv", "data_feed")
_ENGINE_HEARTBEAT_HINTS: tuple = ("engine_heartbeat", "scheduler_heartbeat", "loop_iteration")
_BROKER_CONN_HINTS: tuple = ("broker_connection", "broker_connected", "trade_connection")


class MonitorCenterDataProvider:
    """
    A-11 监控中心数据提供者。

    聚合四类只读数据（全部消费"当前已发生"状态，防未来）：
      1. 健康总览（系统级 overall + 组件级 checks，复用 HealthChecker 报告结构）
      2. 告警列表（scope/level/category 筛选，走 AlertManager；details 脱敏）
      3. 实盘指标卡片（MetricsCollector 已采集指标）
      4. 交易连续状态（行情活性/引擎心跳/券商连接；指标缺失则 unknown，不伪造）

    依赖全部可注入（测试用 fake）；未注入时懒加载全局单例。

    Usage:
        provider = MonitorCenterDataProvider()
        overview = provider.get_health_overview()
        alerts = provider.get_alerts(scope="unacknowledged")
    """

    def __init__(
        self,
        alert_manager: Optional[Any] = None,
        health_checker: Optional[Any] = None,
        metrics_collector: Optional[Any] = None,
        metrics_history_store: Optional[Any] = None,
        account_reconciler: Optional[Any] = None,
        limit_usage_monitor: Optional[Any] = None,
        execution_stats_monitor: Optional[Any] = None,
    ):
        """
        Parameters
        ----------
        alert_manager : AlertManager | None
            告警管理器（None 懒加载全局单例）
        health_checker : HealthChecker | None
            健康检查器（None 则系统级状态回退为 unknown，不崩溃）
        metrics_collector : MetricsCollector | None
            指标采集器（None 懒加载全局单例）
        metrics_history_store : MetricsHistoryStore | None
            指标时序库（B-2 权益曲线/PnL 回放；None 懒加载默认库）
        account_reconciler : AccountReconciler | None
            账户对账器（B-2/B-8 对账状态；None 则对账状态回退全局单例）
        limit_usage_monitor : LimitUsageMonitor | None
            限额监控（B-8 限额使用视图配置；当前使用率读 collector 指标）
        execution_stats_monitor : ExecutionStatsMonitor | None
            执行统计监控（B-8 执行质量视图 summary()）
        """
        self._alert_manager = alert_manager
        self._health_checker = health_checker
        self._metrics_collector = metrics_collector
        self._history_store = metrics_history_store
        self._account_reconciler = account_reconciler
        self._limit_usage_monitor = limit_usage_monitor
        self._execution_stats_monitor = execution_stats_monitor

    # ── 依赖懒加载 ──

    def _get_alert_manager(self) -> Any:
        """获取告警管理器（注入优先，否则全局单例）。"""
        if self._alert_manager is None:
            from myquant.monitoring import get_alert_manager as _g
            self._alert_manager = _g()
        return self._alert_manager

    def _get_collector(self) -> Any:
        """获取指标采集器（注入优先，否则全局单例）。"""
        if self._metrics_collector is None:
            from myquant.monitoring import get_metrics_collector as _g
            self._metrics_collector = _g()
        return self._metrics_collector

    # ── 1. 健康总览（系统+组件两级）──

    def get_health_overview(self) -> Dict[str, Any]:
        """
        健康总览：系统级 overall + 组件级 checks。

        复用 HealthChecker.get_summary() 报告结构（overall_status/healthy/degraded/
        unhealthy/results）；健康检查器未注入 → 系统级 unknown，组件列表为空（不谎报）。

        Returns
        -------
        dict : {"system": {"status", "checks_count", "healthy", "degraded", "unhealthy"},
                "components": [{"name", "status", "message"}]}
        """
        if self._health_checker is None:
            return {
                "system": {"status": "unknown", "checks_count": 0,
                           "healthy": 0, "degraded": 0, "unhealthy": 0},
                "components": [],
            }
        try:
            summary = self._health_checker.get_summary()
        except Exception as e:  # 健康检查器异常：降级为 unknown，不阻断看板
            logger.error(f"[web] 健康总览采集失败: {e}")
            return {
                "system": {"status": "unknown", "checks_count": 0,
                           "healthy": 0, "degraded": 0, "unhealthy": 0},
                "components": [],
            }
        components = [
            {
                "name": r.get("name", ""),
                "status": r.get("status", "unknown"),
                "message": r.get("message", ""),
            }
            for r in (summary.get("results") or [])
        ]
        return {
            "system": {
                "status": summary.get("overall_status", "unknown"),
                "checks_count": summary.get("checks_count", len(components)),
                "healthy": summary.get("healthy_count", 0),
                "degraded": summary.get("degraded_count", 0),
                "unhealthy": summary.get("unhealthy_count", 0),
            },
            "components": components,
        }

    # ── 2. 告警列表（筛选 + 脱敏）──

    def get_alerts(
        self,
        scope: str = "all",
        level: str = "",
        category: str = "",
        limit: int = _DEFAULT_ALERT_LIMIT,
    ) -> Dict[str, Any]:
        """
        告警列表（含统计；details 脱敏）。

        【防未来】只返回 AlertManager 中已发生的告警，不预测未来。

        Parameters
        ----------
        scope : str
            all / unacknowledged / escalated
        level : str
            info/warning/error/critical（空=不限）
        category : str
            告警分类（空=不限）
        limit : int
            返回条数（1..500）

        Returns
        -------
        dict : {"count", "alerts": [...], "summary": {...}}
        """
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到 {limit}")
        am = self._get_alert_manager()
        try:
            if scope == "unacknowledged":
                alerts = am.get_unacknowledged(limit=limit)
            elif scope == "escalated":
                alerts = am.get_escalated(limit=limit)
            else:
                level_enum = None
                if level:
                    from myquant.core.constants import SeverityLevel
                    try:
                        level_enum = SeverityLevel(level)
                    except ValueError:
                        level_enum = None
                alerts = am.get_history(
                    level=level_enum, category=category or None, limit=limit,
                )
        except Exception as e:
            logger.error(f"[web] 告警列表采集失败: {e}")
            return {"count": 0, "alerts": [], "summary": {}}

        rows: List[Dict[str, Any]] = []
        for a in alerts:
            d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
            d["details"] = mask_sensitive_fields(d.get("details"))
            rows.append(d)
        try:
            summary = am.stats
        except Exception:
            summary = {}
        return {"count": len(rows), "alerts": rows, "summary": summary}

    # ── 3. 实盘指标卡片 ──

    def get_metric_cards(self, limit: int = 12) -> List[Dict[str, Any]]:
        """
        实盘指标卡片（MetricsCollector 已采集指标的当前值）。

        【防未来】只展示采集器中已存在的数据点，不做趋势预测。

        Returns
        -------
        list[dict] : [{"name", "value", "labels", "age_seconds"}]
        """
        collector = self._get_collector()
        try:
            points = collector.get_all()
        except Exception as e:
            logger.error(f"[web] 指标卡片采集失败: {e}")
            return []
        cards: List[Dict[str, Any]] = []
        for m in points:
            to_dict = getattr(m, "to_dict", None)
            if callable(to_dict):
                d = to_dict()
            elif isinstance(m, dict):
                d = m
            else:
                continue
            cards.append({
                "name": d.get("name", ""),
                "value": d.get("value", 0.0),
                "labels": d.get("labels", {}) or {},
                "timestamp": d.get("timestamp", 0.0),
            })
        return cards[: max(1, limit)]

    # ── 4. 交易连续状态（行情活性/引擎心跳/券商连接）──

    def _continuity_status(self, hints: tuple) -> Dict[str, Any]:
        """
        按指标名子串探测一类连续状态；无匹配指标 → unknown（graceful，不伪造）。

        Returns
        -------
        dict : {"state": "ok"|"stale"|"unknown", "metric": name|None, "value": float}
        """
        collector = self._get_collector()
        try:
            points = collector.get_all()
        except Exception as e:
            logger.error(f"[web] 交易连续状态采集失败: {e}")
            return {"state": "unknown", "metric": None, "value": 0.0}
        for m in points:
            to_dict = getattr(m, "to_dict", None)
            d = to_dict() if callable(to_dict) else (m if isinstance(m, dict) else {})
            name = str(d.get("name", "")).lower()
            if any(h in name for h in hints):
                return {"state": "ok", "metric": d.get("name"), "value": float(d.get("value", 0.0))}
        return {"state": "unknown", "metric": None, "value": 0.0}

    def get_trading_continuity(self) -> Dict[str, Any]:
        """
        交易连续状态三连：行情活性 / 引擎心跳 / 券商连接。

        A-4/A-5/A-6 完成后可直接消费其指标；未完成时对应项为 unknown（
        诚实降级，绝不伪造"已连接/心跳正常"）。

        Returns
        -------
        dict : {"market_activity": {...}, "engine_heartbeat": {...}, "broker_connection": {...}}
        """
        return {
            "market_activity": self._continuity_status(_MARKET_ACTIVITY_HINTS),
            "engine_heartbeat": self._continuity_status(_ENGINE_HEARTBEAT_HINTS),
            "broker_connection": self._continuity_status(_BROKER_CONN_HINTS),
        }

    # ── 聚合：监控中心一页数据（API 与看板同源）──

    def build_overview_payload(self, alert_limit: int = _DEFAULT_ALERT_LIMIT) -> Dict[str, Any]:
        """
        监控中心一页聚合数据（健康+告警+指标+连续状态）。

        Returns
        -------
        dict : 监控中心完整只读视图（供 /api/monitor/overview 与看板复用同源数据）
        """
        return {
            "health": self.get_health_overview(),
            "alerts": self.get_alerts(limit=alert_limit),
            "metric_cards": self.get_metric_cards(),
            "trading_continuity": self.get_trading_continuity(),
        }

    # ═══════════════════════════════════════════
    #  B-2：PnL 快照 + 对账状态 + 当日权益曲线（与 /api/monitor 同源）
    # ═══════════════════════════════════════════

    def get_reconciliation_status(self) -> Dict[str, Any]:
        """
        账户对账状态（B-2/B-8 对账视图）。

        数据源 AccountReconciler.to_dict；未注入对账器 → unknown（不谎报一致）。
        【防未来】只读当前已发生的对账状态，不预测。

        Returns
        -------
        dict : {account_id/consistent/frozen/in_flight/last_check_ts/last_diff/...}
               未注入时返回 {"available": False}。
        """
        recon = self._account_reconciler
        if recon is None:
            try:
                from myquant.monitoring.account_reconciler import get_account_reconciler
                recon = get_account_reconciler()
                self._account_reconciler = recon
            except Exception as e:  # noqa: BLE001
                logger.error(f"[web] 对账器获取失败: {e}")
                return {"available": False}
        try:
            return recon.to_dict()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[web] 对账状态读取失败: {e}")
            return {"available": False}

    def get_pnl_snapshot(self) -> Dict[str, Any]:
        """
        最新 PnL 快照（B-2 资金 PnL 视图；与 /api/monitor/pnl 同源）。

        时序库最新一点的总资产/可用/持仓市值/已实现/未实现 PnL；冻结期间
        不更新（防脏门）。无数据时各项 None（不伪造）。

        Returns
        -------
        dict : {"pnl": {...}, "ts", "account_id", "reconciliation"}
        """
        from myquant.monitoring.metrics_collector import get_latest_pnl_snapshot
        store = self._history_store
        recon = self._account_reconciler
        try:
            return get_latest_pnl_snapshot(store=store, reconciler=recon)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[web] PnL 快照读取失败: {e}")
            return {"pnl": {}, "ts": None, "account_id": None, "reconciliation": None}

    def get_intraday_equity_curve(self) -> List[Dict[str, Any]]:
        """
        当日权益曲线（B-2：时序回放，升序）。

        Returns
        -------
        list[dict] : [{"ts": epoch秒, "equity": 总资产}]；无数据返回 []
        """
        from myquant.monitoring.metrics_collector import replay_intraday_equity_curve
        store = self._history_store
        try:
            return replay_intraday_equity_curve(store=store)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[web] 当日权益曲线回放失败: {e}")
            return []

    # ═══════════════════════════════════════════
    #  B-8：业务驾驶舱四视图（与 /api/monitor/cockpit 同源）
    # ═══════════════════════════════════════════

    def _metric_value(self, name: str) -> Optional[float]:
        """从 collector 取某指标当前值（多标签组合取最新；无则 None）。"""
        collector = self._get_collector()
        try:
            points = collector.get_all(name=name)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[web] 指标 {name} 读取失败: {e}")
            return None
        values: List[float] = []
        for m in points:
            to_dict = getattr(m, "to_dict", None)
            d = to_dict() if callable(to_dict) else (m if isinstance(m, dict) else {})
            if d.get("name") == name:
                try:
                    values.append(float(d.get("value", 0.0)))
                except (TypeError, ValueError):
                    continue
        return values[-1] if values else None

    def get_limit_usage_view(self) -> Dict[str, Any]:
        """
        限额使用视图（B-8 视图③）：各维度当前使用率 + 配置阈值。

        当前使用率读 MetricsCollector 的 limit_usage_* GAUGE（与 /api/metrics
        同源）；阈值/限额读 LimitUsageMonitor.get_status()（注入时）。未配置维度
        enabled=false（PENDING_CAL，不编造数值）。

        Returns
        -------
        dict : {"usages": {dim: {"value": float|None, "enabled": bool}},
                "config": {...}|None}
        """
        from myquant.monitoring.metrics_collector import BuiltinMetrics
        usage_metrics = {
            "concentration": BuiltinMetrics.LIMIT_USAGE_CONCENTRATION,
            "leverage": BuiltinMetrics.LIMIT_USAGE_LEVERAGE,
            "single_stock": BuiltinMetrics.LIMIT_USAGE_SINGLE_STOCK,
        }
        usages: Dict[str, Any] = {}
        for dim, metric_name in usage_metrics.items():
            val = self._metric_value(metric_name)
            usages[dim] = {"value": val, "enabled": val is not None}
        config: Optional[Dict[str, Any]] = None
        if self._limit_usage_monitor is not None:
            try:
                config = self._limit_usage_monitor.get_status()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[web] 限额配置读取失败: {e}")
                config = None
        return {"usages": usages, "config": config}

    def get_execution_quality_view(self) -> Dict[str, Any]:
        """
        执行质量视图（B-8 视图④）：延迟 P99 / 滑点 / 拒单率。

        注入 ExecutionStatsMonitor 时直接消费其 summary()（滑点买/卖分列、
        延迟四段 P99、拒单率+累计计数）；未注入则从 collector GAUGE 取当前值兜底，
        无样本时数值为 None（不伪造）。

        Returns
        -------
        dict : {"summary": {...}|None, "gauges": {...}, "available": bool}
        """
        summary: Optional[Dict[str, Any]] = None
        if self._execution_stats_monitor is not None:
            try:
                summary = self._execution_stats_monitor.summary()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[web] 执行统计 summary 读取失败: {e}")
                summary = None
        # collector 兜底（与 /api/metrics 同源）
        gauges: Dict[str, Optional[float]] = {
            "ack_p99_ms": self._metric_value("execution_ack_p99_ms"),
            "signal_to_fill_p99_ms": self._metric_value("execution_signal_to_fill_p99_ms"),
            "slippage_buy_pct": self._metric_value("slippage_buy_pct"),
            "slippage_sell_pct": self._metric_value("slippage_sell_pct"),
            "order_reject_rate": self._metric_value("order_reject_rate"),
            "orders_submitted_total": self._metric_value("orders_submitted_total"),
            "orders_rejected_total": self._metric_value("orders_rejected_total"),
        }
        available = summary is not None or any(v is not None for v in gauges.values())
        return {"summary": summary, "gauges": gauges, "available": available}

    def build_cockpit_payload(self) -> Dict[str, Any]:
        """
        业务驾驶舱四视图聚合（B-8；API 与看板同源，数据一致）。

        四视图：
          1. reconciliation 对账状态（consistent/frozen/last_diff）
          2. pnl 资金 PnL（B-2 快照 + 当日曲线点数）
          3. limit_usage 限额使用（各维度使用率 + 阈值配置）
          4. execution 执行质量（滑点/拒单/延迟）

        Returns
        -------
        dict : 四键视图；任一视图内部异常均 graceful 降级，不整体抛错。
        """
        return {
            "reconciliation": self.get_reconciliation_status(),
            "pnl": self.get_pnl_snapshot(),
            "limit_usage": self.get_limit_usage_view(),
            "execution": self.get_execution_quality_view(),
        }
# ═══════════════════════════════════════════════
#  DataQualityExplorer 已迁移到 myquant.monitoring.data_quality_explorer
#  （#S2修复：消除API层依赖Web层的架构倒置）
#  本文件通过顶部 from myquant.monitoring.data_quality_explorer import DataQualityExplorer 保持向后兼容
# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
#  Web看板
# ═══════════════════════════════════════════════
