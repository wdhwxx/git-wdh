# 文件名：scripts/一致性校验.py
"""
一致性校验（薄入口）
====================
P3-1：三模式（event / mixed / vectorized）净值一致性校验。
同一策略、同一数据下运行三套引擎，按"绝对 + 相对"双阈值逐日对拍净值曲线，
解决浮点数合法偏差误报问题；event 与 mixed/vectorized 之间用收敛级容差。

业务逻辑全部在 myquant/backtest/consistency_check.py（本文件仅做薄入口）。

运行方式:
    # 默认：三模式演示策略 + 确定性合成行情
    python scripts/一致性校验.py

    # 指定标的 / 行情规模 / 随机种子
    python scripts/一致性校验.py --symbols 000001.SZ,000002.SZ --n-bars 400 --seed 1

    # 自定义全局双阈值（覆盖所有对拍对；绝对 + 相对同时可配）
    python scripts/一致性校验.py --abs-tol 1e-2 --rel-tol 1e-4

    # 只对拍指定引擎对 + JSON 落盘
    python scripts/一致性校验.py --modes mixed,vectorized --json results/consistency.json

退出码: 0=全部对拍通过；1=存在超出阈值偏差；2=参数错误。

设计原则（代码规则）:
  - 薄入口：只做 参数解析 → 调用 consistency_check → 汇总输出
  - 严格防未来函数：仅消费各引擎已产出的净值曲线，不引入未来数据
  - 全量类型注解 + Google docstring；常量模块顶部；CLI 输出用 print
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 默认演示参数
DEFAULT_SYMBOLS: str = "000001.SZ,000002.SZ,600000.SH"
DEFAULT_N_BARS: int = 300
DEFAULT_SEED: int = 7
DEFAULT_MODES: str = "event,mixed,vectorized"
DEFAULT_START: str = "2023-01-02"
DEFAULT_END: str = "2024-01-31"
# 合法引擎模式
_VALID_MODES: tuple = ("event", "mixed", "vectorized")


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。

    Returns:
        项目根目录绝对路径。
    """
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    return script_dir.parent


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。

    Returns:
        已配置的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(
        prog="一致性校验",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 三模式净值一致性校验（绝对+相对双阈值）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/一致性校验.py
  python scripts/一致性校验.py --symbols 000001.SZ --n-bars 400 --seed 1
  python scripts/一致性校验.py --abs-tol 1e-2 --rel-tol 1e-4
  python scripts/一致性校验.py --modes mixed,vectorized --json results/consistency.json""",
    )
    parser.add_argument(
        "--strategy",
        default="demo",
        help="策略名（默认 demo 内置三模式演示策略；可传已注册策略名如 ma_cross）",
    )
    parser.add_argument(
        "--params",
        default="{}",
        help="策略参数 JSON（demo 策略支持 hold_start/hold_shares，默认 {}）",
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f"演示行情标的代码（逗号分隔，默认 {DEFAULT_SYMBOLS}）",
    )
    parser.add_argument(
        "--n-bars",
        type=int,
        default=DEFAULT_N_BARS,
        help=f"演示行情每标的 K 线数量（默认 {DEFAULT_N_BARS}）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"演示行情随机种子（默认 {DEFAULT_SEED}，可复现）",
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START,
        help=f"回测起始日期（默认 {DEFAULT_START}）",
    )
    parser.add_argument(
        "--end",
        default=DEFAULT_END,
        help=f"回测结束日期（默认 {DEFAULT_END}）",
    )
    parser.add_argument(
        "--modes",
        default=DEFAULT_MODES,
        help=f"参与对拍的引擎模式（逗号分隔，默认 {DEFAULT_MODES}）",
    )
    parser.add_argument(
        "--abs-tol",
        type=float,
        default=None,
        help="全局绝对容差（覆盖所有对拍对；默认按对使用内置容差）",
    )
    parser.add_argument(
        "--rel-tol",
        type=float,
        default=None,
        help="全局相对容差（覆盖所有对拍对；默认按对使用内置容差）",
    )
    parser.add_argument(
        "--json",
        dest="json_out",
        default=None,
        help="报告 JSON 输出路径（可选）",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="P2-05：用生产 processed 真实数据对拍（默认用 make_demo_data 合成行情，供 CI/演示）",
    )
    parser.add_argument(
        "--processed-dir",
        default="./datasets/processed",
        help="--real 模式下的 processed 数据目录（默认 ./datasets/processed）",
    )
    return parser


def _parse_params(raw: str) -> dict[str, Any]:
    """解析策略参数 JSON 并校验为 dict。

    Args:
        raw: 参数字符串。

    Returns:
        参数字典。

    Raises:
        ValueError: JSON 非法或不是对象时抛出。
    """
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--params 不是合法 JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise TypeError("--params 必须是 JSON 对象")
    return params


def _build_strategy(name: str, params: dict[str, Any]) -> Any:
    """按名称构建策略实例（demo 用内置演示策略，其余走策略注册器）。

    Args:
        name: 策略名。
        params: 策略参数。

    Returns:
        策略实例（StrategyBase 子类）。

    Raises:
        ValueError: 策略创建失败时抛出。
    """
    if name == "demo":
        from myquant.backtest.consistency_check import DemoThreeModeStrategy

        return DemoThreeModeStrategy(**params)
    try:
        from myquant.strategies import create_strategy

        return create_strategy(name, **params)
    except ValueError as exc:
        raise ValueError(f"策略 [{name}] 创建失败: {exc}") from exc


def main() -> int:
    """主入口：解析参数 → 三模式一致性校验 → 汇总输出。

    Returns:
        退出码（0=通过，1=存在偏差，2=参数错误）。
    """
    project_root = find_project_root()
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    # G1 EX-09A：装配撮合执行器工厂（薄入口脚本为合法装配层；
    # BacktestEngine 不再静默降级，未注册 broker_factory 直接 raise ValueError）
    from myquant.backtest.assembly import register_broker_factory
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

    args = setup_argument_parser().parse_args()

    # 1. 参数解析与防御校验
    try:
        params = _parse_params(args.params)
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            raise ValueError("--symbols 不能为空")
        modes = [m.strip().lower() for m in args.modes.split(",") if m.strip()]
        if not modes:
            raise ValueError("--modes 不能为空")
        invalid_modes = [m for m in modes if m not in _VALID_MODES]
        if invalid_modes:
            raise ValueError(
                f"非法引擎模式: {invalid_modes}（支持: {', '.join(_VALID_MODES)}）"
            )
    except ValueError as exc:
        print(f"[错误] {exc}")
        return 2

    # 2. 数据与策略
    if args.real:
        # P2-05：生产 processed 真实数据对拍（防未来：load_real_data 经 get_bars 按 end_date 截断）
        from myquant.backtest.consistency_check import load_real_data

        try:
            data = load_real_data(
                symbols=symbols,
                start_date=args.start,
                end_date=args.end,
                processed_dir=args.processed_dir,
            )
        except ValueError as exc:
            print(f"[错误] 真实数据加载失败: {exc}")
            return 2
    else:
        from myquant.backtest.consistency_check import make_demo_data

        data = make_demo_data(symbols=symbols, n_bars=args.n_bars, seed=args.seed)
    try:
        strategy = _build_strategy(args.strategy, params)
    except ValueError as exc:
        print(f"[错误] {exc}")
        return 2

    data_source = "真实数据(processed)" if args.real else "合成行情(make_demo_data)"
    print("=" * 64)
    print("  三模式净值一致性校验入口")
    print(f"  项目根: {project_root}")
    print(f"  策略: {args.strategy} | 标的: {len(symbols)} 个 | 模式: {','.join(modes)}")
    print(f"  数据源: {data_source}")
    if args.abs_tol is not None or args.rel_tol is not None:
        print(
            f"  全局双阈值: abs_tol={args.abs_tol} rel_tol={args.rel_tol}"
            "（覆盖所有对拍对）"
        )
    print("=" * 64)

    # 3. 执行三模式一致性校验
    from myquant.backtest.consistency_check import ConsistencyChecker

    checker = ConsistencyChecker(abs_tol=args.abs_tol, rel_tol=args.rel_tol)
    config_params = {
        "strategy_name": args.strategy,
        "symbols": symbols,
        "start_date": args.start,
        "end_date": args.end,
        "initial_capital": 1_000_000.0,
        "frequency": "1d",
    }
    from myquant.models.backtest import BacktestConfig

    try:
        report = checker.check_all(
            config=BacktestConfig(**config_params),
            strategy=strategy,
            data=data,
            modes=modes,
        )
    except (TypeError, ValueError) as exc:
        print(f"[错误] 一致性校验执行失败: {exc}")
        return 2

    print(report.summary())

    # P2-05 裁决规则（方案 §4.5 规则4）：overall_ok=False 不阻断回测本身，
    # 但阻断"可信/可上线"宣传标记——真实数据三引擎口径不一致时，禁止对外宣称
    # 本策略/系统三引擎口径一致、可上线。
    if not report.overall_ok:
        print("-" * 64)
        print("  ❌ 三引擎净值对拍未通过（overall_ok=False）")
        print("  → 不阻断本次回测，但【阻断『可信/可上线』宣传标记】：")
        print("    真实数据下 event/mixed/vectorized 口径存在超阈值偏差，")
        print("    在收敛偏差前，禁止将本策略标注为『口径一致/可上线/可信』。")
        print("-" * 64)

    # 4. JSON 落盘
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            payload = report.to_dict()
            # P2-05：报告里显式标注可信宣传是否被阻断（WARNING 级，不阻断回测）
            payload["trust_blocked"] = not report.overall_ok
            payload["data_source"] = data_source
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[已保存] 一致性校验报告: {out_path}")

    return 0 if report.overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
