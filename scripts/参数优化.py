# 文件名：scripts/参数优化.py
"""
策略参数优化 - 中文入口脚本（薄入口）
====================================
对策略参数网格做并行搜索（接入 P2 并行治理层），输出最优参数组合与 Top 榜。

接入的 P2 成果:
  - parallel_factor_context : 进入自动关闭 factor_cache L1 缓存并告警，退出自动恢复
  - ParallelManager         : 计算进程池并行评估参数组合（多标的 x 多参数组）
  - StageTimer              : 分阶段计时（数据准备/网格构建/并行评估/汇总输出），性能定位
  - pickle 预检             : 进程池提交前自动拦截不可 pickle 任务

功能:
  - 默认使用内置演示行情（确定性可复现），无需真实数据库即可体验并行参数优化
  - 目标指标可切换：sharpe_ratio / total_return / max_drawdown
  - 结果输出：控制台 Top 榜 + 可选 CSV 落盘

运行方式:
    # 完整演示（默认: ma_cross 策略, fast/slow 双均线网格, 3 标的）
    python scripts/参数优化.py

    # 自定义网格与标的
    python scripts/参数优化.py --fast 3,5,8 --slow 10,20,40 --symbols 000001.SZ,000002.SZ

    # 指定目标指标 / 进程数 / 结果落盘
    python scripts/参数优化.py --objective total_return --cpu 4 --output results/param_opt.csv

设计原则（规则2/规则3/规则5）:
  - 薄入口：只做 参数解析 → 调用 param_optimizer → 分阶段计时汇总输出
  - 任务函数 evaluate_params 位于 myquant.backtest.param_optimizer（真实模块，
    满足 Windows spawn 下子进程 pickle 反序列化约束）
  - 命令行用标准 argparse，参数显式校验
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    return script_dir.parent


def _parse_int_list(raw: str, name: str) -> List[int]:
    """解析逗号分隔整数列表并做去重保序校验。

    Args:
        raw: 形如 "3,5,8" 的字符串。
        name: 参数名（用于报错信息）。

    Returns:
        去重保序后的整数列表。

    Raises:
        ValueError: 空输入或含非整数项。
    """
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"参数 {name} 不能为空")
    values: List[int] = []
    for p in parts:
        try:
            v = int(p)
        except ValueError as exc:
            raise ValueError(f"参数 {name} 含非整数项: {p!r}") from exc
        if v <= 0:
            raise ValueError(f"参数 {name} 必须为正整数，收到: {v}")
        if v not in values:
            values.append(v)
    return values


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="参数优化",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 策略参数并行优化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/参数优化.py
  python scripts/参数优化.py --fast 3,5,8 --slow 10,20,40
  python scripts/参数优化.py --objective total_return --cpu 4 --output results/param_opt.csv""",
    )
    parser.add_argument("--strategy", default="ma_cross",
                        help="策略注册名（默认 ma_cross）")
    parser.add_argument("--fast", default="3,5,8,12",
                        help="fast_period 候选值（逗号分隔）")
    parser.add_argument("--slow", default="15,25,40",
                        help="slow_period 候选值（逗号分隔）")
    parser.add_argument("--symbols", default="000001.SZ,000002.SZ,600000.SH",
                        help="标的代码（逗号分隔）")
    parser.add_argument("--n-bars", type=int, default=300,
                        help="演示行情每标的 K 线数量（默认 300）")
    parser.add_argument("--seed", type=int, default=7,
                        help="演示行情随机种子（默认 7，可复现）")
    parser.add_argument("--objective", default="sharpe_ratio",
                        choices=("sharpe_ratio", "total_return", "max_drawdown"),
                        help="目标指标（默认 sharpe_ratio）")
    parser.add_argument("--cpu", type=int, default=None,
                        help="计算进程池大小（默认自动）")
    parser.add_argument("--start", default="2023-01-02",
                        help="回测起始日期（默认 2023-01-02）")
    parser.add_argument("--end", default="2024-01-31",
                        help="回测结束日期（默认 2024-01-31）")
    parser.add_argument("--output", default=None,
                        help="结果 CSV 输出路径（可选）")
    return parser


def main() -> int:
    """主入口：解析参数 → 分阶段执行参数优化 → 汇总输出。"""
    # 注入项目根到 sys.path，保证任意工作目录可 import myquant
    project_root = find_project_root()
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    from myquant.backtest.param_optimizer import (
        ParamGrid,
        ParamOptimizer,
        ParamSpec,
        make_demo_data,
    )
    from myquant.utils.csv_utils import CSV_HEADER_MAPS, export_csv_cn
    from myquant.utils.timer import StageTimer

    args = setup_argument_parser().parse_args()
    timer = StageTimer()

    # 1. 数据准备
    with timer.phase("数据准备"):
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            print("[错误] --symbols 不能为空")
            return 2
        data = make_demo_data(symbols=symbols, n_bars=args.n_bars, seed=args.seed)

    # 2. 网格构建
    with timer.phase("网格构建"):
        fast_values = _parse_int_list(args.fast, "fast")
        slow_values = _parse_int_list(args.slow, "slow")
        grid = ParamGrid(
            (
                ParamSpec("fast_period", tuple(fast_values)),
                ParamSpec("slow_period", tuple(slow_values)),
            )
        )

    print("=" * 64)
    print("  策略参数并行优化")
    print(f"  项目根: {project_root}")
    print(f"  策略: {args.strategy} | 目标: {args.objective}")
    print(f"  网格: {len(grid)} 组参数 x {len(data)} 标的 = "
          f"{len(grid) * len(data)} 组任务")
    print("=" * 64)

    # 3. 并行评估（parallel_factor_context 自动管理 L1 缓存开关）
    with timer.phase("并行评估"):
        optimizer = ParamOptimizer(
            strategy_name=args.strategy,
            grid=grid,
            data=data,
            objective=args.objective,
            cpu_workers=args.cpu,
            start_date=args.start,
            end_date=args.end,
        )
        result = optimizer.optimize()

    # 4. 汇总输出
    with timer.phase("汇总输出"):
        print(result.summary())
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            export_csv_cn(result.to_dataframe(), out_path, header_map=CSV_HEADER_MAPS["param_optimize"], prefix_map={"param_": "参数:"})
            print(f"[已保存] 全量结果: {out_path}")
        best = result.best()
        if best is not None:
            print(f"[最优] {json.dumps(best['params'], ensure_ascii=False)} "
                  f"夏普 {best['sharpe_ratio']:.3f} "
                  f"收益 {best['total_return']:.2%}")

    # 5. 分阶段计时报告（P2 计时器：性能定位）
    print("=" * 64)
    print("  分阶段耗时")
    print(timer.report())
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
