# 文件名：myquant/backtest/config_builder.py
"""
回测配置构建模块（L4修复：业务逻辑从 run_回测入口.py 下沉）
====================================================================
将"配置字典 + 命令行参数 → BacktestConfig"的构建逻辑从根入口脚本
run_回测入口.py 迁移至 myquant 包，修复 scripts/生成回测报告.py 反向导入
根入口脚本的分层违规（评估报告 L4：破坏"入口→包"单向依赖）。

核心函数:
  - load_stock_pool_from_csv  从CSV加载股票池
  - apply_exclude_prefixes    按代码前缀过滤标的
  - build_backtest_config     配置字典+CLI参数 → BacktestConfig

依赖: typing, pathlib, argparse, pandas, myquant.models.backtest
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List


def load_stock_pool_from_csv(filepath: str) -> List[str]:
    """
    从CSV文件加载股票池

    支持列名: code / symbol / 股票代码 / codes

    参数:
        filepath: CSV文件路径

    返回:
        List[str]: 股票代码列表
    """
    import pandas as pd

    df = pd.read_csv(filepath)
    for col in ("code", "symbol", "股票代码", "codes", "Code"):
        if col in df.columns:
            return df[col].astype(str).str.strip().tolist()
    raise ValueError(
        f"股票池文件 {filepath} 未找到代码列。"
        f"支持列名: code/symbol/股票代码/codes，实际列: {list(df.columns)}"
    )


def apply_exclude_prefixes(symbols: List[str], exclude_prefixes: List[str]) -> List[str]:
    """
    按代码前缀过滤标的列表（剔除不想要的市场/板块）。

    参数:
        symbols: 原始标的代码列表
        exclude_prefixes: 要排除的代码前缀列表（如 ['30', '688', '92']）

    返回:
        过滤后的标的代码列表
    """
    if not exclude_prefixes:
        return symbols
    result = []
    for sym in symbols:
        code = sym.split('.')[0] if '.' in sym else sym
        if any(code.startswith(p) for p in exclude_prefixes):
            continue
        result.append(sym)
    return result


def build_backtest_config(
    config_dict: Dict[str, Any],
    args: argparse.Namespace,
    project_root: Path,
) -> Any:
    """
    将配置字典 + 命令行参数 转为 BacktestConfig 对象

    参数:
        config_dict: 从YAML加载的配置字典
        args: 命令行参数
        project_root: 项目根目录

    返回:
        BacktestConfig: 回测配置对象
    """
    from myquant.models.backtest import (
        BacktestConfig,
        CommissionModel,
        CommissionType,
        SlippageModel,
    )

    bt_cfg = config_dict.get("backtest", {})
    strategy_cfg = config_dict.get("strategy", {})
    sim_broker_cfg = config_dict.get("sim_broker", {})
    metrics_cfg = config_dict.get("metrics", {})
    stock_pool_cfg = config_dict.get("stock_pool", {})

    # ── 提取标的列表 ──
    symbols: List[str] = []
    exclude_prefixes = stock_pool_cfg.get("exclude_prefixes", []) or []

    # 优先级1: 命令行 --symbols
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 优先级2: 命令行 --stock-pool 文件
    if not symbols and getattr(args, "stock_pool", None):
        pool_path = args.stock_pool
        if not Path(pool_path).is_absolute():
            pool_path = str(project_root / pool_path)
        symbols = load_stock_pool_from_csv(pool_path)

    # 优先级3: 配置文件 stock_pool.codes
    if not symbols:
        pool_type = stock_pool_cfg.get("type", "custom")
        if pool_type in ("custom", "preset_filtered"):
            # custom = 手动指定代码列表
            # preset_filtered = 标的预筛选结果（由 scripts/标的预筛选.py 或窗口生成）
            symbols = stock_pool_cfg.get("codes", []) or []
            if pool_type == "preset_filtered" and not symbols:
                raise ValueError(
                    "stock_pool.type=preset_filtered 但 codes 为空。"
                    "请先运行 scripts/标的预筛选.py 或在窗口点击「预筛选标的」按钮生成标的清单。"
                )
        elif pool_type == "three_factor":
            # KP-P1-08：three_factor 三因子交集选股尚未接线，不再静默回退 codes
            # （文档声明与实现一致，未实现即显式失败，不假装成功）
            raise ValueError(
                "stock_pool.type=three_factor 三因子交集选股尚未接线，"
                "请使用 custom/preset_filtered 类型并在 codes 中指定股票列表，"
                "或使用 --symbols 参数"
            )
        elif pool_type == "all":
            # 全市场：需要从数据目录扫描所有标的（当前阶段不推荐，回测慢）
            print(
                "[警告] stock_pool.type=all 全市场回测，将扫描数据目录所有标的，"
                "建议使用 preset_filtered 预筛选以提升速度"
            )
            # TODO: 从 HistoricalDataProvider 扫描全市场标的
            symbols = stock_pool_cfg.get("codes", []) or []

    if not symbols:
        raise ValueError(
            "未指定股票池。请在配置文件 stock_pool.codes 中指定，"
            "或使用 --symbols / --stock-pool 参数"
        )

    # ── 应用代码前缀过滤（剔除创业板/科创板/北交所等）──
    before_count = len(symbols)
    symbols = apply_exclude_prefixes(symbols, exclude_prefixes)
    after_count = len(symbols)
    if exclude_prefixes and before_count != after_count:
        print(
            f"[标的过滤] 排除前缀 {exclude_prefixes}："
            f"{before_count} → {after_count} 只（剔除 {before_count - after_count} 只）"
        )

    # ── 手续费模型 ──
    commission = CommissionModel(
        type=CommissionType.RATIO,
        buy_rate=sim_broker_cfg.get("commission_rate", 0.0002),
        sell_rate=sim_broker_cfg.get("commission_rate", 0.0002),
        min_commission=sim_broker_cfg.get("min_commission", 5.0),
        stamp_tax=sim_broker_cfg.get("stamp_tax_rate", 0.0005),
        transfer_fee=sim_broker_cfg.get("transfer_fee_rate", 0.00001),
    )

    # ── 滑点模型 ──
    slippage_mode = sim_broker_cfg.get("slippage_mode", "ratio")
    slippage_value = sim_broker_cfg.get("slippage_value", 0.0002)
    if slippage_mode == "ratio":
        # ratio模式: slippage_value 是比例(如0.0002=万2)，转为基点
        slippage = SlippageModel(
            type="fixed_bps",
            fixed_bps=slippage_value * 10000,
        )
    elif slippage_mode == "none":
        # 显式零滑点（诚实披露；realism manifest 报 is_zero_cost=True 由门禁判）
        slippage = SlippageModel(type="none")
    else:
        # P0-03 卡本体收尾（#347）：消灭静默零滑点通道——未知 mode 显式报错
        # （与 P0-05 backtest.py SlippageModel.calc 语义一致，不静默回退 type="none"）
        raise ValueError(
            f"slippage_mode 未知值: {slippage_mode!r}"
            f"（合法值: ratio/none）"
        )

    # ── 构建 BacktestConfig ──
    # P0-03 卡本体收尾（#347）：从 YAML sim_broker 段读新键给显式字段赋值，
    # 替换死键 enforce_limit_up_down（代码读 enforce_limit，旧键永不生效）。
    # 此前恒用类默认（enforce_limit=False/price_limit_pct=0.10）→ 配置→行为断链。
    # T3-2：capital_base 统一注入——策略仓位计算基准必须与账户资金一致。
    # 实盘 engine.py:2908 已 setdefault("capital_base", initial_capital)；
    # 回测此前只透传 yaml params、不含该键 → 落策略类默认 100 万。
    # 此处显式注入回测 initial_capital，保证回测/实盘两侧 capital_base==账户资金。
    # 用户若要两侧数值一致，需把 live_config.yaml 的 initial_capital 也改成同值。
    _strategy_params = dict(strategy_cfg.get("params", {}) or {})
    _initial_capital = float(args.capital or bt_cfg.get("initial_capital", 1_000_000))
    _strategy_params.setdefault("capital_base", _initial_capital)

    config = BacktestConfig(
        strategy_name=args.strategy or strategy_cfg.get("name", "default"),
        symbols=symbols,
        start_date=args.start or bt_cfg.get("start_date", ""),
        end_date=args.end or bt_cfg.get("end_date", ""),
        frequency=bt_cfg.get("time_frame", "1d"),
        initial_capital=_initial_capital,
        commission=commission,
        slippage=slippage,
        benchmark=bt_cfg.get("benchmark", "000300.SH"),
        risk_free_rate=metrics_cfg.get("risk_free_rate", 0.02),
        params=_strategy_params,
        # P0-03 约束真可控：sim_broker 段新键（enforce_limit 替换旧键 enforce_limit_up_down）
        price_limit_pct=float(sim_broker_cfg.get("price_limit_pct", 0.10)),
        enforce_limit=bool(sim_broker_cfg.get("enforce_limit", False)),
        enable_t1=bool(sim_broker_cfg.get("enable_t1", True)),
        lot_size=int(sim_broker_cfg.get("lot_size", 100)),
        participation_rate=float(sim_broker_cfg.get("participation_rate", 1.0)),
        # P0-01 端到端（#345）：真实性门禁配置透传（YAML 顶层段；缺省 {} = 审计档）
        realism_gate=config_dict.get("realism_gate", {}) or {},
    )

    # 配置校验
    errors = config.validate()
    if errors:
        raise ValueError(f"回测配置校验失败: {'; '.join(errors)}")

    return config
