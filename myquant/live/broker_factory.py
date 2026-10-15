# 文件名：myquant/live/broker_factory.py
"""
broker_factory.py - 券商适配器装配工厂（回写#289）
==========================================================================
从配置创建券商适配器（paper/sim / qmt / ptrade）。

定位：**应急链可独立复用的装配工厂**（0.3/R-5）——build_broker_from_config
原驻 engine.py，但《一键平仓》应急脚本 import engine 会把应急链绑死在引擎上
（引擎模块异常则应急平仓失效）。本模块只依赖 myquant.live.brokers，
**不依赖 engine / risk / guardian / circuit_breaker / 门禁**：
系统崩溃、装配被破坏时应急链仍可独立装配券商并平仓。

依赖: myquant.live.brokers（PaperBroker / QmtBroker / PtradeBroker）
"""

from __future__ import annotations

from typing import Any, Dict


def build_broker_from_config(
    broker_cfg: Dict[str, Any],
    execution_cfg: Dict[str, Any],
    initial_capital: float,
) -> Any:
    """
    从配置创建券商适配器（H2修复；原 engine.py:2669 迁入，逻辑不变）

    支持: paper/sim（本地模拟撮合）/ qmt（迅投QMT）/ ptrade（恒生Ptrade）。
    xtp 等未实现适配器明确报错，引导经 pyproject 入口点 myquant.brokers 扩展。
    券商适配器只对接券商原始接口，不做清洗与策略逻辑（蓝图纪律）。

    Parameters
    ----------
    broker_cfg : Dict
        live_config.yaml 的 broker 段
    execution_cfg : Dict
        live_config.yaml 的 execution 段（手续费/滑点/交易规则，与回测一致）
    initial_capital : float
        初始资金（模拟模式用）
    """
    from myquant.live.brokers import PaperBroker, PtradeBroker, QmtBroker

    broker_type = str(broker_cfg.get("type", "paper")).lower()
    exec_params = {
        "commission_rate": float(execution_cfg.get("commission_rate", 0.0002)),
        "min_commission": float(execution_cfg.get("min_commission", 5.0)),
        "stamp_tax_rate": float(execution_cfg.get("stamp_tax_rate", 0.0005)),
        "transfer_fee_rate": float(execution_cfg.get("transfer_fee_rate", 0.00001)),
        "slippage_mode": str(execution_cfg.get("slippage_mode", "ratio")),
        "slippage_value": float(execution_cfg.get("slippage_value", 0.0002)),
        "enable_t1": bool(execution_cfg.get("enable_t1", True)),
        "lot_size": int(execution_cfg.get("lot_size", 100)),
        "price_limit_pct": float(execution_cfg.get("price_limit_pct", 0.10)),
    }

    if broker_type in ("paper", "sim", "simulation"):
        return PaperBroker(initial_capital=initial_capital, **exec_params)
    if broker_type == "qmt":
        return QmtBroker(
            qmt_path=str(broker_cfg.get("qmt_path", "")),
            account_id=str(broker_cfg.get("account", "")),
            mode=str(broker_cfg.get("mode", "SIM")).upper(),
            initial_capital=initial_capital,
            **exec_params,
        )
    if broker_type == "ptrade":
        return PtradeBroker(
            ptrade_path=str(broker_cfg.get("ptrade_path", "")),
            account_id=str(broker_cfg.get("account", "")),
            mode=str(broker_cfg.get("mode", "SIM")).upper(),
            initial_capital=initial_capital,
            **exec_params,
        )
    raise ValueError(
        f"未实现的券商适配器: {broker_type} "
        f"（支持: paper/sim/qmt/ptrade；其他请经 pyproject 入口点 myquant.brokers 扩展）"
    )
