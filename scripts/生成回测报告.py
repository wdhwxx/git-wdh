# 文件名：scripts/生成回测报告.py
"""
生成回测报告 - 中文入口脚本（薄入口，第4项D）
============================================
一键运行回测并生成完整报告（HTML含蒙特卡洛 + PDF + 文本 + CSV），
聚焦「报告生成」，复用回测入口 run_回测入口.py 的配置构建逻辑。

功能:
  - 从 config/backtest_config.yaml（或 --config 指定）加载回测配置
  - 从 HistoricalDataProvider 加载历史数据（严格按 end_date 过滤，防未来）
  - 动态发现并实例化策略（strategies 注册器，与实盘共用）
  - 运行 BacktestEngine 回测（T日信号T+1成交，LookaheadGuard 强制校验）
  - 生成完整报告：
      * HTML（含蒙特卡洛：统计卡/收益直方图/回撤直方图/分位带）
      * PDF（绩效表/交易统计/净值与回撤曲线/蒙特卡洛/交易明细分页）
      * 文本摘要 + 交易明细CSV + 净值曲线CSV

运行方式:
    # 使用默认配置（config/backtest_config.yaml 或 example）生成报告
    python scripts/生成回测报告.py

    # 指定配置文件 + 覆盖时间范围
    python scripts/生成回测报告.py --config config/backtest_config.yaml --start 2023-01-01 --end 2024-12-31

    # 指定标的多标的 + 指定输出目录
    python scripts/生成回测报告.py --symbols 000001.SZ,600519.SH --output outputs/报告

    # 蒙特卡洛参数控制（默认2000次/252天/seed42，可复现）
    python scripts/生成回测报告.py --mc-simulations 5000 --mc-horizon 126 --mc-seed 7

    # 关闭蒙特卡洛区块
    python scripts/生成回测报告.py --no-monte-carlo

    # 列出已注册策略
    python scripts/生成回测报告.py --list-strategies

设计原则（规则2/规则3/规则4）:
  - 薄入口：只做 参数解析→配置加载→数据加载→策略实例化→引擎运行→报告生成
  - 报告生成复用 myquant/backtest/report.py 与 pdf_report.py（回测/实盘共用）
  - 固定蒙特卡洛 seed → 报告可复现
  - 防未来函数：LookaheadGuard 回测前后校验，数据按 end_date 过滤，不可跳过
"""

import argparse
import sys
from pathlib import Path


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="生成回测报告",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 回测报告生成（HTML+PDF+蒙特卡洛）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/生成回测报告.py
  python scripts/生成回测报告.py --config config/backtest_config.yaml --start 2023-01-01 --end 2024-12-31
  python scripts/生成回测报告.py --symbols 000001.SZ,600519.SH --output outputs/报告
  python scripts/生成回测报告.py --mc-simulations 5000 --mc-seed 7
  python scripts/生成回测报告.py --no-monte-carlo
  python scripts/生成回测报告.py --list-strategies""",
    )
    parser.add_argument("--config", "-c", type=str, default=None,
                        help="回测配置文件路径（YAML），默认 config/backtest_config.yaml")
    parser.add_argument("--strategy", "-s", type=str, default=None,
                        help="策略名称（覆盖配置），需已在策略注册器中注册")
    parser.add_argument("--symbols", type=str, default=None,
                        help="标的代码列表，逗号分隔（覆盖配置，如 000001.SZ,600519.SH）")
    parser.add_argument("--start", type=str, default=None,
                        help="回测起始日期 YYYY-MM-DD（覆盖配置）")
    parser.add_argument("--end", type=str, default=None,
                        help="回测结束日期 YYYY-MM-DD（覆盖配置）")
    parser.add_argument("--capital", type=float, default=None,
                        help="初始资金（元，覆盖配置）")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="报告输出目录（默认 outputs/<回测名>_<时间戳>）")
    parser.add_argument("--no-monte-carlo", action="store_true",
                        help="关闭蒙特卡洛分析区块")
    parser.add_argument("--mc-simulations", type=int, default=2000,
                        help="蒙特卡洛模拟次数（默认2000）")
    parser.add_argument("--mc-horizon", type=int, default=252,
                        help="蒙特卡洛模拟期数/交易日（默认252≈1年）")
    parser.add_argument("--mc-seed", type=int, default=42,
                        help="蒙特卡洛随机种子（默认42，固定可复现）")
    parser.add_argument("--list-strategies", action="store_true",
                        help="列出所有已注册的策略名称并退出")
    parser.add_argument("--verbose", action="store_true", help="输出DEBUG日志")
    return parser


def list_registered_strategies() -> None:
    """列出已注册策略"""
    from myquant.strategies.registry import discover_strategies, list_strategies
    discover_strategies()
    strategies = list_strategies()
    if not strategies:
        print("当前没有已注册的策略。")
        return
    print("已注册策略列表：")
    print("-" * 50)
    for name in strategies:
        print(f"  {name}")


def main() -> int:
    """主入口：解析参数 → 回测 → 生成完整报告（HTML+PDF+蒙特卡洛）"""
    project_root = find_project_root()
    # 项目根加入 sys.path（scripts 薄入口惯例，保证 python scripts/xxx.py 可导入 myquant）
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    args = setup_argument_parser().parse_args()

    # 列出策略模式
    if args.list_strategies:
        list_registered_strategies()
        return 0

    # ── 1. 加载配置（L4修复：配置构建下沉 myquant.backtest.config_builder，不再反向导入根入口）──
    from myquant.backtest.config_builder import build_backtest_config

    config_path = args.config
    if config_path is None:
        default_config = project_root / "config" / "backtest_config.yaml"
        example_config = project_root / "config" / "backtest_config.example.yaml"
        if default_config.exists():
            config_path = str(default_config)
        elif example_config.exists():
            print(f"[提示] 未找到实际配置文件，使用示例配置：{example_config.name}")
            config_path = str(example_config)
        else:
            print("[错误] 未找到回测配置文件（config/backtest_config.yaml）。")
            return 1

    try:
        from myquant.core.config_loader import ConfigLoader
        config_dict = ConfigLoader(base_dir=project_root).load(config_path)
    except Exception as e:
        print(f"[错误] 配置文件加载失败: {e}")
        return 1

    # ── 2. 初始化日志 ──
    try:
        from myquant.core.logger import setup_logger
        output_cfg = config_dict.get("output", {})
        setup_logger(level="DEBUG" if args.verbose else output_cfg.get("log_level", "INFO"),
                     log_dir=project_root / "logs")
    except Exception:
        pass
    from myquant.core.logger import get_logger
    logger = get_logger("生成回测报告")

    # ── 3. 构建 BacktestConfig（args 覆盖配置）──
    try:
        bt_config = build_backtest_config(config_dict, args, project_root)
    except ValueError as e:
        print(f"[错误] 配置校验失败: {e}")
        return 1

    logger.info("=" * 60)
    logger.info("Pythonwdhmm 鲲鹏量化交易系统 — 回测报告生成启动")
    logger.info(f"配置文件: {config_path}")
    logger.info(f"策略: {bt_config.strategy_name} | 标的: {bt_config.symbols} | 期间: {bt_config.start_date} ~ {bt_config.end_date}")
    print(f"\n[回测配置] 策略={bt_config.strategy_name} 标的={bt_config.symbols} "
          f"期间={bt_config.start_date}~{bt_config.end_date} 资金={bt_config.initial_capital:,.0f}")

    # ── 4. 动态发现并实例化策略 ──
    try:
        from myquant.strategies.registry import discover_strategies, create_strategy
        discover_strategies()
        strategy = create_strategy(bt_config.strategy_name, **bt_config.params)
        print(f"[策略] 实例化完成: {strategy.name}")
    except ValueError as e:
        print(f"[错误] 策略实例化失败: {e}")
        print("       使用 --list-strategies 查看可用策略")
        return 1
    except Exception as e:
        print(f"[错误] 策略加载异常: {e}")
        return 1

    # ── 5. 创建数据提供器（实盘时替换为 RealtimeDataProvider，引擎零修改）──
    try:
        from myquant.backtest.data_loader import HistoricalDataProvider
        data_cfg = config_dict.get("data", {})
        data_dir = data_cfg.get("data_dir", "./datasets/processed")
        if not Path(data_dir).is_absolute():
            data_dir = str(project_root / data_dir)
        data_provider = HistoricalDataProvider(
            processed_dir=data_dir,
            default_adjust=data_cfg.get("adjust", "raw"),
        )
        print(f"[数据] 提供器初始化: {data_provider}")
    except Exception as e:
        print(f"[错误] 数据提供器初始化失败: {e}")
        return 1

    # ── 6. 防未来函数校验器重置（规则4，回测前必须重置）──
    from myquant.core.lookahead_guard import get_lookahead_guard
    guard = get_lookahead_guard()
    guard.reset()

    # ── 7. 创建回测引擎并运行（G1 EX-09A：装配撮合执行器工厂）──
    try:
        from myquant.backtest.engine import register_broker_factory
        from myquant.execution.sim_broker import SimBroker
        register_broker_factory(
            lambda cfg: SimBroker(
                initial_capital=cfg.initial_capital,
                commission_model=cfg.commission,
                slippage_model=cfg.slippage,
                price_limit_pct=float(cfg.to_dict().get("price_limit_pct", 0.10)),
                enforce_limit=bool(cfg.to_dict().get("enforce_limit", False)),
                is_suspended=None,
                lot_size=int(getattr(cfg, "lot_size", 0)),
            )
        )
        # P1-04：回测入口收敛到 BacktestScheduler.create_engine（挂 leakage_gate
        # + realism_manifest + P1-11 出口审计）；保持 mode="event" 真值基准语义
        # （消费上方 register_broker_factory 的 SimBroker，撮合行为不变）。
        from myquant.backtest.engine import run_backtest
        result = run_backtest(
            config=bt_config,
            strategy=strategy,
            data_provider=data_provider,
            mode="event",
        )
        print("[回测] 引擎创建完成，开始运行...")
    except Exception as e:
        logger.error(f"回测执行异常: {e}", exc_info=True)
        print(f"[错误] 回测执行失败: {e}")
        return 2

    if not result.is_success:
        print(f"[错误] 回测未成功完成: {result.error_message}")
        return 2

    # ── 8. 防未来函数强制校验（规则4，不可跳过）──
    try:
        guard.assert_pass()
        print("[防未来] LookaheadGuard 校验通过")
    except AssertionError as e:
        print(f"[错误] 防未来函数校验未通过: {e}")
        return 3

    # ── 9. 生成完整报告（HTML含蒙特卡洛 + PDF + 文本 + CSV）──
    from myquant.backtest.report import ReportGenerator
    output_cfg = config_dict.get("output", {})
    report_title = output_cfg.get("report_title", "鲲鹏量化交易系统 - 回测报告")

    if args.output:
        output_dir = args.output
    else:
        bt_name = output_cfg.get("output_dir") or f"backtest_{bt_config.strategy_name}"
        output_dir = str(project_root / "outputs" / bt_name)
    from datetime import datetime
    output_dir = str(Path(output_dir) / datetime.now().strftime("%Y%m%d_%H%M%S"))

    generator = ReportGenerator(title=report_title)
    paths = generator.generate_full_report(
        result, output_dir,
        include_monte_carlo=not args.no_monte_carlo,
        mc_n_simulations=args.mc_simulations,
        mc_horizon_days=args.mc_horizon,
        mc_seed=args.mc_seed,
    )

    # ── 10. 输出报告路径清单 ──
    print("\n" + "=" * 60)
    print("  回测报告生成完成")
    print("=" * 60)
    print(f"  策略: {bt_config.strategy_name} | 期间: {bt_config.start_date} ~ {bt_config.end_date}")
    print(f"  总收益: {result.metrics.total_return:.2%} | 夏普: {result.metrics.sharpe_ratio:.4f} "
          f"| 最大回撤: {result.metrics.max_drawdown:.2%} | 交易次数: {result.metrics.total_trades}")
    print("-" * 60)
    for kind, path in paths.items():
        print(f"  [{kind.upper():<8}] {path}")
    print("-" * 60)
    print("  报告说明:")
    print("    - HTML 含蒙特卡洛分析（统计卡/收益直方图/回撤直方图/分位带），浏览器打开")
    print("    - PDF 为可打印归档版（绩效表/净值与回撤曲线/蒙特卡洛/交易明细分页）")
    print("    - 蒙特卡洛为基于历史收益的统计模拟，不代表未来实际表现")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
