# 文件名：myquant/live/emergency_liquidation.py
"""
emergency_liquidation.py - 紧急一键平仓服务（任务47 / P2）
================================================================
应急预案核心工具：系统崩溃 / 网络中断 / QMT 无法连接等异常场景下，
人工一键清空全部持仓（配合《实盘前最终验收与硬性准备实施细则》应急
预案一节，报告可直接打印放于手边）。

设计原则:
  1. 独立运行 —— 不依赖引擎/策略/风控初始化，直接操作券商适配器
     （系统崩溃后引擎可能起不来，平仓工具必须能单独跑）。
  2. 顺序固定 —— 先撤全部挂单（释放冻结资金、避免重复委托），
     再查询持仓，最后逐仓市价全仓卖出。
  3. 默认预览 —— dry_run 只输出平仓计划不真实下单；真实执行必须
     显式 confirm=True（LIVE 模式误触防护）。
  4. 防御式 —— 单标的失败不阻断其余；连接/查询失败在报告中明确
     标注，绝不静默；每步结果进结构化报告。
  5. 防未来函数 —— 只操作"已查询到的当前持仓与挂单"，不使用任何
     预测数据；成交价由券商市价撮合决定，不假设。

使用方式（薄入口）:
  python scripts/一键平仓.py --config config/live_config.yaml          # 预览（默认）
  python scripts/一键平仓.py --config config/live_config.yaml --confirm # 真实平仓
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from myquant.core.event_bus import Event, EventType
from myquant.core.logger import get_logger
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType

logger = get_logger("live.emergency_liquidation")


class EmergencyLiquidationService:
    """
    紧急一键平仓服务

    直接操作券商适配器完成"撤挂单 + 全仓市价卖出"，输出可打印报告。

    Parameters
    ----------
    broker : Any
        券商适配器实例（PaperBroker / QmtBroker / PtradeBroker，需具备
        is_connected / connect / get_active_orders / cancel_order /
        get_positions / submit_order 能力）
    strategy_id : str
        平仓订单归属策略标识（默认 emergency_liquidation，便于审计区分）
    event_bus : Any
        可选事件总线（B-6 联动）：真实全平（executed=True）后发布
        EventType.EMERGENCY_LIQUIDATION 事件，供告警/熔断联动消费。None 则不发布。
    """

    def __init__(
        self,
        broker: Any,
        strategy_id: str = "emergency_liquidation",
        event_bus: Any = None,
    ) -> None:
        self._broker = broker
        self._strategy_id = strategy_id
        self._event_bus = event_bus

    # ─────────────────────────────────────────────
    #  主入口
    # ─────────────────────────────────────────────

    def execute(
        self, confirm: bool = False, symbol: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        执行一键平仓

        流程: 连接检查 → 撤全部挂单 → 查询持仓 → 逐仓市价全仓卖出。
        默认 dry_run（只生成计划不真实下单）；confirm=True 才真实执行。

        Parameters
        ----------
        confirm : bool
            True=真实下单；False=预览计划（默认）
        symbol : str | None
            可选，仅平仓指定标的

        Returns
        -------
        Dict[str, Any] : 结构化报告
            {
              "ok": bool, "executed": bool,
              "mode": "DRY_RUN" | "LIVE_EXEC",
              "started_at": float, "finished_at": float, "elapsed_ms": float,
              "connect": {"ok": bool, "broker": str, "error": str},
              "cancelled_orders": [{"order_id", "symbol", "ok"}],
              "liquidation": {
                  "total_symbols": n, "success": n, "failed": n,
                  "skipped": n, "planned": n, "total_quantity": n, "estimated_value": n,
                  "items": [{"symbol", "quantity", "price", "status",
                             "reason", "order_id", "estimated_value"}],
              },
              "errors": [str],
            }
        """
        started = time.time()
        report: Dict[str, Any] = {
            "ok": False,
            "executed": False,  # KP-M02: 是否真实执行（dry-run 恒 False）
            "mode": "LIVE_EXEC" if confirm else "DRY_RUN",
            "started_at": started,
            "finished_at": 0.0,
            "elapsed_ms": 0.0,
            "connect": {"ok": False, "broker": str(getattr(self._broker, "broker_name", type(self._broker).__name__)), "error": ""},
            "cancelled_orders": [],
            "liquidation": {
                "total_symbols": 0,
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "planned": 0,  # KP-M02: dry-run 计划项计数（不冒充 success）
                "total_quantity": 0.0,
                "estimated_value": 0.0,
                "items": [],
            },
            "errors": [],
        }

        try:
            # 1. 连接检查（失败则安全返回，不继续）
            if not self._connect(report):
                report["finished_at"] = time.time()
                report["elapsed_ms"] = (report["finished_at"] - started) * 1000
                return report

            # 2. 撤全部挂单（dry_run 只记录计划）
            self._cancel_pending_orders(report, confirm, symbol)

            # 3. 查询持仓 → 逐仓市价全仓卖出
            self._liquidate_positions(report, confirm, symbol)

            # 4. 汇总
            report["ok"] = report["liquidation"]["failed"] == 0 and not report["errors"]
            if report["liquidation"]["total_symbols"] == 0:
                report["ok"] = True  # 无持仓：平仓本身成功（空仓）
            # KP-M02: dry-run 即使 ok 也绝不视为真实执行
            if not confirm:
                report["executed"] = False
            report["finished_at"] = time.time()
            report["elapsed_ms"] = (report["finished_at"] - started) * 1000
            logger.info(
                f"[emergency_liquidation] 一键平仓完成: mode={report['mode']} "
                f"ok={report['ok']} executed={report['executed']} "
                f"标的={report['liquidation']['total_symbols']} "
                f"成功={report['liquidation']['success']} "
                f"计划={report['liquidation']['planned']} "
                f"失败={report['liquidation']['failed']}"
            )
            # B-6 联动：真实全平（executed=True）后发布紧急强平事件，
            # 供告警（A-8 → CRITICAL）/ 熔断联动消费；dry_run 不发布。
            if report["executed"]:
                self._publish_emergency_event(report)
        except Exception as e:  # noqa: BLE001 - 顶层兜底：任何异常都进报告不崩溃
            report["errors"].append(f"一键平仓执行异常: {e}")
            report["ok"] = False
            report["finished_at"] = time.time()
            report["elapsed_ms"] = (report["finished_at"] - started) * 1000
            logger.error(f"[emergency_liquidation] 执行异常: {e}")
        return report

    # ─────────────────────────────────────────────
    #  步骤1：连接检查
    # ─────────────────────────────────────────────

    def _connect(self, report: Dict[str, Any]) -> bool:
        broker = self._broker
        try:
            connected = bool(broker.is_connected)
            if not connected:
                logger.warning("[emergency_liquidation] 券商未连接，尝试重连...")
                broker.connect()
                connected = bool(broker.is_connected)
            report["connect"]["ok"] = connected
            if not connected:
                report["connect"]["error"] = "券商连接失败（重连后仍不可用）"
                report["errors"].append(report["connect"]["error"])
                logger.error(f"[emergency_liquidation] {report['connect']['error']}")
            return connected
        except Exception as e:  # noqa: BLE001 - 连接异常按失败处理
            report["connect"]["error"] = f"券商连接异常: {e}"
            report["errors"].append(report["connect"]["error"])
            logger.error(f"[emergency_liquidation] 连接异常: {e}")
            return False

    # ─────────────────────────────────────────────
    #  B-6 联动：发布紧急强平事件
    # ─────────────────────────────────────────────

    def _publish_emergency_event(self, report: Dict[str, Any]) -> None:
        """
        真实全平后发布 EMERGENCY_LIQUIDATION 事件（B-6 联动）。

        供告警闭环（A-8 → CRITICAL）与熔断线消费。事件 data 只携带本次
        已发生的平仓结果（防未来）；发布失败记 error 不影响平仓报告。

        Parameters
        ----------
        report : Dict[str, Any]
            execute() 生成的结构化报告
        """
        if self._event_bus is None:
            return
        try:
            event = Event(
                event_type=EventType.EMERGENCY_LIQUIDATION,
                data={
                    "ok": report.get("ok"),
                    "mode": report.get("mode"),
                    "executed": report.get("executed"),
                    "total_symbols": report.get("liquidation", {}).get("total_symbols"),
                    "success": report.get("liquidation", {}).get("success"),
                    "failed": report.get("liquidation", {}).get("failed"),
                    "cancelled_orders": len(report.get("cancelled_orders", [])),
                    "reason": "熔断线/人工紧急全平",
                },
                source="emergency_liquidation",
            )
            self._event_bus.publish(event)
            logger.info("[emergency_liquidation] 已发布 EMERGENCY_LIQUIDATION 事件")
        except Exception as e:  # noqa: BLE001 - 发布失败不阻断平仓主流程
            logger.error(f"[emergency_liquidation] 发布强平事件失败: {e}")

    # ─────────────────────────────────────────────
    #  步骤2：撤全部挂单
    # ─────────────────────────────────────────────

    def _cancel_pending_orders(
        self, report: Dict[str, Any], confirm: bool, symbol: Optional[str]
    ) -> None:
        """撤销全部活跃挂单（释放冻结资金、避免与平仓卖出重复委托）"""
        broker = self._broker
        get_active = getattr(broker, "get_active_orders", None)
        if not callable(get_active):
            return  # 券商不支持活跃单查询：跳过撤单步骤（报告无该环节）
        try:
            active = list(get_active(symbol) or [])
        except Exception as e:  # noqa: BLE001 - 查询失败记录并继续（不阻断平仓）
            report["errors"].append(f"活跃挂单查询失败: {e}")
            logger.warning(f"[emergency_liquidation] 活跃挂单查询失败: {e}")
            return
        for order in active:
            entry = {
                "order_id": str(getattr(order, "order_id", "")),
                "symbol": str(getattr(order, "symbol", "")),
                "ok": False,
            }
            if confirm:
                try:
                    cancel_result = broker.cancel_order(order.order_id)
                    entry["ok"] = bool(cancel_result)
                    if entry["ok"]:
                        report["executed"] = True  # KP-M02: 真实撤单算已执行
                except Exception as e:  # noqa: BLE001 - 单笔撤单失败记录继续
                    entry["reason"] = str(e)
                    report["errors"].append(f"撤单失败 {order.order_id}: {e}")
                    logger.warning(f"[emergency_liquidation] 撤单失败 {order.order_id}: {e}")
            else:
                entry["ok"] = True  # dry_run 标记计划成功（不真实执行）
                entry["dry_run"] = True
            report["cancelled_orders"].append(entry)

    # ─────────────────────────────────────────────
    #  步骤3：逐仓市价全仓卖出
    # ─────────────────────────────────────────────

    def _liquidate_positions(
        self, report: Dict[str, Any], confirm: bool, symbol: Optional[str]
    ) -> None:
        """查询持仓并逐仓市价卖出（可卖数量 = extra.available_qty 或全仓）"""
        broker = self._broker
        try:
            positions = list(broker.get_positions() or [])
        except Exception as e:  # noqa: BLE001 - 持仓查询失败：报告错误并返回
            report["errors"].append(f"持仓查询失败: {e}")
            logger.error(f"[emergency_liquidation] 持仓查询失败: {e}")
            return

        items: List[Dict[str, Any]] = []
        total_quantity = 0.0
        estimated_value = 0.0
        success = failed = skipped = planned = 0
        for pos in positions:
            sym = str(getattr(pos, "symbol", "") or "")
            if symbol and sym != symbol:
                continue
            quantity = self._sellable_quantity(pos)
            if quantity <= 0:
                skipped += 1
                items.append({
                    "symbol": sym, "quantity": 0.0,
                    "price": self._market_price(pos), "status": "SKIPPED",
                    "reason": "可卖数量为0（T+1冻结或已清空）", "order_id": "",
                    "estimated_value": 0.0,
                })
                continue
            price = self._market_price(pos)
            value = quantity * price
            order_id = ""
            status = "PLANNED"  # dry_run
            reason = "预览：未真实下单（加 --confirm 执行）"
            if confirm:
                order = Order(
                    symbol=sym,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=quantity,
                    price=0.0,  # 市价单：价格由券商撮合决定（防未来：不假设成交价）
                    strategy_id=self._strategy_id,
                )
                order.extra["emergency_liquidation"] = True
                try:
                    result = broker.submit_order(order)
                    order_id = str(getattr(result, "order_id", "") or "")
                    st = getattr(result, "status", None)
                    if st is OrderStatus.REJECTED:
                        status = "REJECTED"
                        result_extra = getattr(result, "extra", None) or {}
                        reason = str(result_extra.get("reject_reason", "") or "") or "券商拒绝"
                        failed += 1
                        report["errors"].append(f"平仓被拒 {sym}: {reason}")
                        logger.error(f"[emergency_liquidation] 平仓被拒 {sym}: {reason}")
                    elif st in (OrderStatus.SUBMITTED, OrderStatus.PENDING,
                                OrderStatus.PARTIAL_FILLED):
                        status = "SUBMITTED"
                        reason = ""
                        success += 1
                        report["executed"] = True  # KP-M02: 真实提交平仓单算已执行
                    else:
                        status = "UNKNOWN"
                        reason = f"状态异常: {st}"
                        failed += 1
                        report["errors"].append(f"平仓状态异常 {sym}: {st}")
                except Exception as e:  # noqa: BLE001 - 单标的失败不阻断其余
                    status = "ERROR"
                    reason = str(e)
                    failed += 1
                    report["errors"].append(f"平仓异常 {sym}: {e}")
                    logger.warning(f"[emergency_liquidation] 平仓异常 {sym}: {e}")
            else:
                planned += 1  # KP-M02: dry_run 计划项计入 planned，不冒充 success
            total_quantity += quantity
            estimated_value += value
            items.append({
                "symbol": sym, "quantity": quantity,
                "price": price, "status": status, "reason": reason,
                "order_id": order_id, "estimated_value": round(value, 2),
            })

        liq = report["liquidation"]
        liq["items"] = items
        liq["total_symbols"] = len(items)
        liq["success"] = success
        liq["failed"] = failed
        liq["skipped"] = skipped
        liq["planned"] = planned
        liq["total_quantity"] = total_quantity
        liq["estimated_value"] = round(estimated_value, 2)

    # ─────────────────────────────────────────────
    #  辅助
    # ─────────────────────────────────────────────

    @staticmethod
    def _sellable_quantity(pos: Any) -> float:
        """
        可卖数量：优先取持仓 extra.available_qty（QMT 映射写入 can_use_volume；
        含 0 值——可卖 0 就是 0，不得回退全仓）；无该字段才回退持仓总量
        （券商侧 T+1 校验兜底）。
        """
        extra = getattr(pos, "extra", None) or {}
        if "available_qty" in extra:
            return max(0.0, float(extra.get("available_qty", 0.0) or 0.0))
        return abs(float(getattr(pos, "quantity", 0.0) or 0.0))

    @staticmethod
    def _market_price(pos: Any) -> float:
        """最新市场价（持仓快照价，用于估算市值；成交价由券商市价撮合决定）"""
        return float(getattr(pos, "market_price", 0.0) or 0.0)
