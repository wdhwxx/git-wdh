# 文件名：run_回测入口.py
"""
回测入口脚本（薄入口）
======================

功能：
    解析命令行参数，加载配置，初始化日志，从 HistoricalDataProvider 加载数据，
    实例化策略，创建 BacktestEngine 执行回测，防未来函数校验，生成报告。

    本脚本是薄入口，不包含任何回测业务逻辑，所有业务逻辑在 myquant 包内实现。

使用方法：
    # 使用配置文件运行回测
    python run_回测入口.py --config config/backtest_config.yaml

    # 命令行参数覆盖配置
    python run_回测入口.py --config config/backtest_config.yaml --start 2024-01-01 --end 2024-12-31

    # 列出所有已注册策略
    python run_回测入口.py --list-strategies

    # 指定股票池文件
    python run_回测入口.py --config config/backtest_config.yaml --stock-pool datasets/features/stock_pool.csv

设计原则（薄入口，规则2/规则3/规则4）：
    - 本脚本只做：参数解析 → 日志初始化 → 配置加载 → 数据加载 → 策略实例化
                 → 引擎创建 → 运行回测 → 防未来函数校验 → 报告生成
    - 所有回测业务逻辑（指标计算、策略信号、订单执行、绩效统计）在 myquant 包内
    - 回测与实盘共用同一套策略类、指标计算、风控规则（规则2：实盘无缝对接）
    - 数据层抽象：使用 HistoricalDataProvider，实盘替换为 RealtimeDataProvider 零修改
    - 防未来函数校验在回测结束时强制调用 LookaheadGuard.assert_pass()（规则4，不可跳过）

防未来函数（规则4）：
    - 回测开始前：guard.reset() 清空历史记录
    - 数据加载：HistoricalDataProvider 严格按 config.end_date 过滤，不返回未来数据
    - 回测引擎：T日信号T+1开盘成交，市价单以开盘价成交
    - 回测结束后：guard.assert_pass() 强制校验，不通过则返回码3并终止
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# L4修复：配置构建业务逻辑下沉 myquant.backtest.config_builder（入口薄化）
from myquant.backtest.config_builder import (
    build_backtest_config,
)


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    max_depth = 20
    depth = 0
    while current != current.parent and depth < max_depth:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        depth += 1
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="run_回测入口",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 回测入口（薄入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python run_回测入口.py --config config/backtest_config.yaml
  python run_回测入口.py --config config/backtest_config.yaml --start 2024-01-01 --end 2024-12-31
  python run_回测入口.py --list-strategies
  python run_回测入口.py --config config/backtest_config.yaml --stock-pool datasets/features/stock_pool.csv

  # 回测完成后渲染真实数据看板（单文件离线 HTML，路径 A 内存直连）
  python run_回测入口.py --config config/backtest_config.yaml --dashboard outputs/回测看板.html
        """,
    )

    # 配置文件
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="回测配置文件路径（YAML格式），默认 config/backtest_config.yaml",
    )

    # 策略参数
    parser.add_argument(
        "--strategy", "-s",
        type=str,
        default=None,
        help="策略名称（如 rsrs），需在策略注册器中已注册",
    )
    parser.add_argument(
        "--list-strategies",
        action="store_true",
        help="列出所有已注册的策略名称并退出",
    )

    # 回测时间范围
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="回测起始日期（YYYY-MM-DD），覆盖配置文件",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="回测结束日期（YYYY-MM-DD），覆盖配置文件",
    )

    # 资金与股票池
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="初始资金（元），覆盖配置文件",
    )
    parser.add_argument(
        "--stock-pool",
        type=str,
        default=None,
        help="股票池文件路径（CSV，含 code 列），覆盖配置文件",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="直接指定标的代码，逗号分隔（如 000001.SZ,600519.SH）",
    )

    # 输出
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="回测结果输出目录，默认 outputs/",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志输出（DEBUG级别）",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="不生成HTML报告（仅输出文本摘要）",
    )
    parser.add_argument(
        "--dashboard",
        type=str,
        default=None,
        help="回测成功后渲染真实数据看板到该路径（如 outputs/回测看板.html），"
             "基于 BacktestResult 内存直连（路径A），单文件离线可打开",
    )

    return parser


def list_registered_strategies() -> None:
    """列出所有已注册策略（调用 myquant.strategies.registry）"""
    from myquant.strategies.registry import discover_strategies, list_strategies

    discover_strategies()
    strategies = list_strategies()
    if not strategies:
        print("当前没有已注册的策略。")
        print("请在 myquant/strategies/ 下创建策略类并使用 @register_strategy 装饰器注册。")
        return
    print("已注册策略列表：")
    print("-" * 50)
    for name in strategies:
        print(f"  {name}")


def run_backtest(args: argparse.Namespace) -> int:
    """
    执行回测（薄入口：编排 myquant 内部组件）

    返回：
        0  成功
        1  配置错误
        2  回测执行失败
        3  防未来函数校验未通过
    """
    project_root = find_project_root()

    # ── 1. 确定配置文件路径 ──
    config_path = args.config
    if config_path is None:
        default_config = project_root / "config" / "backtest_config.yaml"
        example_config = project_root / "config" / "backtest_config.example.yaml"
        if default_config.exists():
            config_path = str(default_config)
        elif example_config.exists():
            print(f"[提示] 未找到实际配置文件，使用示例配置：{example_config.name}")
            print("       建议复制为 config/backtest_config.yaml 后修改参数。")
            config_path = str(example_config)
        else:
            print("[错误] 未找到回测配置文件。")
            print("       请创建 config/backtest_config.yaml 或使用 --config 指定。")
            return 1

    # ── 2. 加载配置 ──
    try:
        from myquant.core.config_loader import ConfigLoader
        loader = ConfigLoader(base_dir=project_root)
        config_dict = loader.load(config_path)
    except FileNotFoundError as e:
        print(f"[错误] 配置文件不存在: {e}")
        return 1
    except Exception as e:
        print(f"[错误] 配置文件加载失败: {e}")
        return 1

    # ── 3. 初始化日志 ──
    try:
        from myquant.core.logger import setup_logger
        output_cfg = config_dict.get("output", {})
        log_level = "DEBUG" if args.verbose else output_cfg.get("log_level", "INFO")
        log_dir = project_root / "logs"
        setup_logger(level=log_level, log_dir=log_dir)
    except Exception as e:
        print(f"[警告] 日志初始化失败: {e}，使用默认日志")

    from myquant.core.logger import get_logger
    logger = get_logger("run_backtest")

    # ── 3.5 应用性能优化开关（P2 配置：engine.mode + performance 段）──
    try:
        from myquant.utils.performance_config import (
            PerformanceConfig,
            apply_performance,
        )
        perf = PerformanceConfig.from_dict(config_dict)
        applied = apply_performance(perf)
        engine_mode = perf.engine_mode
        logger.info("性能配置已应用: 引擎模式=%s, 开关=%s", engine_mode, sorted(applied))
    except Exception as e:
        engine_mode = "auto"
        logger.warning("性能配置应用失败，回退默认 auto: %s", e)

    # ── 4. 构建 BacktestConfig ──
    try:
        bt_config = build_backtest_config(config_dict, args, project_root)
    except ValueError as e:
        print(f"[错误] {e}")
        return 1

    logger.info("=" * 60)
    logger.info("Pythonwdhmm 鲲鹏量化交易系统 — 回测启动")
    logger.info("=" * 60)
    logger.info(f"配置文件: {config_path}")
    logger.info(f"策略: {bt_config.strategy_name}")
    logger.info(f"标的: {bt_config.symbols}")
    logger.info(f"期间: {bt_config.start_date} ~ {bt_config.end_date}")
    logger.info(f"初始资金: {bt_config.initial_capital:,.0f}")
    logger.info(f"基准: {bt_config.benchmark}")

    # ── 5. 动态发现并实例化策略 ──
    try:
        from myquant.strategies.registry import create_strategy, discover_strategies
        discover_strategies()
        strategy = create_strategy(
            bt_config.strategy_name,
            **bt_config.params,
        )
        logger.info(f"策略实例化完成: {strategy.name}")
    except ValueError as e:
        print(f"[错误] 策略实例化失败: {e}")
        print("       使用 --list-strategies 查看可用策略")
        return 1
    except Exception as e:
        print(f"[错误] 策略加载异常: {e}")
        return 1

    # ── 6. 创建数据提供器（HistoricalDataProvider）──
    # ★★★ 为实盘无缝对接：实盘时替换为 RealtimeDataProvider，引擎零修改 ★★★
    try:
        from myquant.backtest.data_loader import HistoricalDataProvider
        data_cfg = config_dict.get("data", {})
        data_dir = data_cfg.get("data_dir", "./datasets/processed")
        if not Path(data_dir).is_absolute():
            data_dir = str(project_root / data_dir)
        data_provider = HistoricalDataProvider(
            processed_dir=data_dir,
            default_adjust=data_cfg.get("adjust", "raw"),
            data_format=data_cfg.get("data_format", "parquet"),
        )
        logger.info(f"数据提供器初始化: {data_provider}")
    except Exception as e:
        print(f"[错误] 数据提供器初始化失败: {e}")
        return 1

    # ── 7. 防未来函数校验器重置（规则4，回测开始前必须重置）──
    from myquant.core.lookahead_guard import get_lookahead_guard
    guard = get_lookahead_guard()
    guard.reset()
    # P2-D7-001 整改：消费配置 lookahead_guard.enabled/strict_mode（回测端与实盘端对齐）。
    # BacktestScheduler 统一注入全局单例（engine.py create_engine），此处同步即引擎全局生效；
    # 使 GUI/backtest_config.example.yaml 中 strict_mode: true 的声明不再被环境变量默认 false 覆盖。
    lg_cfg = config_dict.get("lookahead_guard", {}) or {}
    if "enabled" in lg_cfg:
        guard.enabled = bool(lg_cfg["enabled"])
        logger.info("lookahead_guard.enabled 已消费: %s", guard.enabled)
    if "strict_mode" in lg_cfg:
        guard.strict = bool(lg_cfg["strict_mode"])
        logger.info("lookahead_guard.strict_mode 已消费: %s（违规将立即抛异常，否则记录至 assert_pass 统一校验）", guard.strict)
    logger.info(f"防未来函数校验器已重置: {guard} (enabled={guard.enabled}, strict={guard.strict})")

    # ── M0-18 出口强制消费：防泄漏门禁配置（v3.0 §5.4，与 lookahead_guard 同模式）──
    # 代码真消费配置：leakage_gate.enabled 直接驱动出口断言阻断（无契约/引用阻断列策略
    # 在 create_engine 被拦）；配置段缺失时保持模块级默认（enabled=True 生产阻断）。
    from myquant.leakage_gate import GateConfig as _LG_GateConfig
    from myquant.leakage_gate import set_config as _lg_set_config

    _lg_cfg = config_dict.get("leakage_gate", {}) or {}
    if _lg_cfg:
        _lg_set_config(_LG_GateConfig(
            enabled=bool(_lg_cfg.get("enabled", True)),
            audit_violations=bool(_lg_cfg.get("audit_violations", True)),
        ))
        logger.info(
            "leakage_gate.enabled 已消费: %s / audit_violations: %s（出口断言将阻断未登记策略）",
            _lg_cfg.get("enabled", True), _lg_cfg.get("audit_violations", True),
        )

    # ── 8. 创建回测引擎（三模式调度，从DataProvider加载数据）──
    # P1-18：注册 broker factory（event 路径需要，否则 event 模式必崩）
    from myquant.backtest.engine import register_broker_factory
    from myquant.execution.sim_broker import SimBroker, make_suspended_lookup

    # B-06 #446：停牌接线——回测配置可配 suspended_symbols（缺失 None 向后兼容），
    # 不再硬编码 is_suspended=None，停牌股在撮合层被拒单
    _sus_raw = config_dict.get("suspended_symbols")
    _sus_symbols = set(_sus_raw) if isinstance(_sus_raw, list) else None
    _suspended_lookup = make_suspended_lookup(_sus_symbols)

    def _broker_factory(config):
        return SimBroker(
            initial_capital=config.initial_capital,
            commission_model=config.commission,
            slippage_model=config.slippage,
            price_limit_pct=float(config.price_limit_pct),
            enforce_limit=bool(config.enforce_limit),
            is_suspended=_suspended_lookup,
        )

    register_broker_factory(_broker_factory)
    try:
        from myquant.backtest.engine import BacktestScheduler
        scheduler = BacktestScheduler(mode=engine_mode)
        engine = scheduler.create_engine(
            config=bt_config,
            strategy=strategy,
            data_provider=data_provider,
        )
        resolved = scheduler.selected_mode or engine_mode
        logger.info("引擎模式: %s（auto 已按策略能力解析）", resolved)
    except Exception as e:
        print(f"[错误] 回测引擎创建失败: {e}")
        return 2

    # ── 9. 运行回测 ──
    try:
        result = engine.run()
    except Exception as e:
        logger.error(f"回测执行异常: {e}", exc_info=True)
        print(f"[错误] 回测执行失败: {e}")
        return 2

    if not result.is_success:
        print(f"[错误] 回测未成功完成: {result.error_message}")
        return 2

    logger.info(
        f"回测完成: 总收益={result.metrics.total_return:.2%}, "
        f"夏普={result.metrics.sharpe_ratio:.2f}, "
        f"最大回撤={result.metrics.max_drawdown:.2%}, "
        f"交易次数={result.metrics.total_trades}"
    )

    # ── 10. 防未来函数强制校验（规则4，不可跳过）──
    try:
        guard.assert_pass()
        logger.info("防未来函数校验通过")
    except Exception as e:
        logger.error(f"防未来函数校验未通过: {e}")
        print("[错误] 防未来函数校验未通过！回测结果不可信。")
        print(f"       {e}")
        return 3

    # ── 11. 生成报告 ──
    output_cfg = config_dict.get("output", {})
    output_dir = args.output or output_cfg.get("base_dir", "./outputs")
    if not Path(output_dir).is_absolute():
        output_dir = str(project_root / output_dir)

    # 文本摘要（始终输出）
    from myquant.backtest.report import ReportGenerator
    report_title = output_cfg.get("report_title", "鲲鹏量化交易系统 - 回测报告")
    generator = ReportGenerator(title=report_title)
    text_report = generator.generate_text(result)
    print()
    print(text_report)

    # 完整报告（HTML + CSV）
    if not args.no_report and output_cfg.get("generate_report", True):
        try:
            report_paths = generator.generate_full_report(result, output_dir)
            print()
            print("报告文件已生成:")
            for key, path in report_paths.items():
                print(f"  [{key}] {path}")
        except Exception as e:
            logger.warning(f"报告生成失败: {e}")
            print(f"[警告] 报告生成失败: {e}")

    # ── 11.5 渲染真实数据看板（--dashboard，路径 A：BacktestResult 内存直连）──
    # 薄入口：仅参数解析 + 调用 myquant 业务模块；渲染失败不影响回测结果与报告（返回码保持 0）
    if args.dashboard:
        try:
            from myquant.backtest.dashboard_serializer import (
                build_dashboard_json,
                render_dashboard,
            )
            dashboard_path = args.dashboard
            if not Path(dashboard_path).is_absolute():
                dashboard_path = str(project_root / dashboard_path)
            dashboard_data = build_dashboard_json(
                result,
                engine_mode=resolved,
                guard_passed=True,
            )
            rendered_path = render_dashboard(dashboard_data, dashboard_path)
            print(f"  [dashboard] {rendered_path}")
            logger.info("真实数据看板已渲染: %s", rendered_path)
        except Exception as e:
            logger.warning("看板渲染失败（不影响回测结果与报告）: %s", e)
            print(f"[警告] 看板渲染失败: {e}")

    # ── 12. 保存回测结果JSON ──
    try:
        import json
        import time
        result_path = Path(output_dir) / f"result_{int(time.time())}.json"
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2, default=str)
        print(f"  [result] {result_path}")
    except Exception as e:
        logger.warning(f"结果JSON保存失败: {e}")

    return 0


def main() -> int:
    """主入口函数"""
    parser = setup_argument_parser()
    args = parser.parse_args()

    # 列出策略模式
    if args.list_strategies:
        list_registered_strategies()
        return 0

    # 执行回测
    return run_backtest(args)


if __name__ == "__main__":
    sys.exit(main())

