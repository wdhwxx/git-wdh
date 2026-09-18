# 文件名：run_批量回测.py
"""
批量回测主入口（薄入口）
========================
统一调度批量回测：数据指纹识别变更标的（增量只重算变更）、三模式引擎调度
（auto/event/mixed/vectorized）、内存自动降级（vectorized→mixed→event + 并行度调整）。

接入的 P2 成果:
  - data_fingerprint + FingerprintStore : 指纹识别变更，增量只重算变更标的
  - BacktestScheduler / run_backtest    : 三模式引擎调度（auto 按策略能力自动选择）
  - MemoryWatcher + MemoryDegradePolicy : 内存水位监控 + 自动降级（WARNING降一级/CRITICAL强制event+串行）
  - parallel_factor_context             : 并行重算时自动关闭 factor_cache L1 缓存
  - StageTimer                          : 分阶段计时

运行方式:
    # 批量回测（auto 三模式调度；首次自动全量建立指纹基线）
    python run_批量回测.py

    # 强制全量
    python run_批量回测.py --force-full

    # 显式指定引擎模式 / 并行度 / 批大小
    python run_批量回测.py --mode mixed --cpu 4 --batch-size 10

    # 自定义内存降级阈值（系统内存百分比）
    python run_批量回测.py --mem-warning 60 --mem-critical 75

    # 结果 JSON 落盘
    python run_批量回测.py --output results/batch_report.json

设计原则（规则2/规则3/规则5）:
  - 薄入口：只做 参数解析 → 调用 batch_backtest → 分阶段计时汇总输出
  - 任务函数 evaluate_batch_symbol 位于 myquant.backtest.batch_backtest（真实模块，
    满足 Windows spawn 下子进程 pickle 反序列化约束）
  - 内存降级只影响性能不影响正确性（三模式净值在 P1 已验收一致）
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List


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


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="run_批量回测",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 批量回测主入口（指纹+三模式+内存降级）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python run_批量回测.py
  python run_批量回测.py --force-full
  python run_批量回测.py --mode mixed --cpu 4 --batch-size 10
  python run_批量回测.py --mem-warning 60 --mem-critical 75""",
    )
    parser.add_argument("--strategy", default="ma_cross",
                        help="策略注册名（默认 ma_cross）")
    parser.add_argument("--params", default='{"fast_period": 5, "slow_period": 20}',
                        help="策略参数 JSON（默认双均线 5/20）")
    parser.add_argument("--symbols", default="000001.SZ,000002.SZ,000003.SZ,600000.SH,600001.SH",
                        help="标的代码（逗号分隔）")
    parser.add_argument("--n-bars", type=int, default=300,
                        help="演示行情每标的 K 线数量（默认 300）")
    parser.add_argument("--seed", type=int, default=7,
                        help="演示行情随机种子（默认 7，可复现）")
    parser.add_argument("--mode", default="auto",
                        choices=("auto", "vectorized", "mixed", "event"),
                        help="引擎模式（默认 auto 按策略能力自动选择）")
    parser.add_argument("--cpu", type=int, default=None,
                        help="计算进程池大小（默认自动）")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="每批标的数（默认一次全批）")
    parser.add_argument("--force-full", action="store_true",
                        help="强制全量重算（忽略指纹差异）")
    parser.add_argument("--cache-dir", default="results/batch_backtest",
                        help="缓存目录（指纹仓库 + 结果缓存，默认 results/batch_backtest）")
    parser.add_argument("--mem-warning", type=float, default=70.0,
                        help="内存告警阈值（系统内存百分比，默认 70）")
    parser.add_argument("--mem-critical", type=float, default=85.0,
                        help="内存严重阈值（系统内存百分比，默认 85）")
    parser.add_argument("--output", default=None,
                        help="报告 JSON 输出路径（可选）")
    parser.add_argument("--start", default="2023-01-02",
                        help="回测起始日期（默认 2023-01-02）")
    parser.add_argument("--end", default="2024-01-31",
                        help="回测结束日期（默认 2024-01-31）")
    return parser


def main() -> int:
    """主入口：解析参数 → 批量回测 → 分阶段计时输出。"""
    project_root = find_project_root()
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    from myquant.backtest.batch_backtest import BatchBacktester
    from myquant.backtest.param_optimizer import make_demo_data
    from myquant.utils.memory_watcher import MemoryWatcher
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

    # 2. 参数解析
    with timer.phase("参数解析"):
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as exc:
            print(f"[错误] --params 不是合法 JSON: {exc}")
            return 2
        if not isinstance(params, dict):
            print("[错误] --params 必须是 JSON 对象")
            return 2
        try:
            watcher = MemoryWatcher(warning_pct=args.mem_warning,
                                    critical_pct=args.mem_critical)
        except ValueError as exc:
            print(f"[错误] 内存阈值参数非法: {exc}")
            return 2

    cache_dir = Path(args.cache_dir)
    fingerprint_path = cache_dir / "fingerprints.json"
    results_path = cache_dir / "results.json"

    print("=" * 64)
    print("  批量回测主入口（指纹 + 三模式 + 内存降级）")
    print(f"  项目根: {project_root}")
    print(f"  标的: {len(symbols)} 个 | 策略: {args.strategy} | 引擎模式: {args.mode}")
    print(f"  缓存: {cache_dir} | 内存阈值: W{args.mem_warning:.0f}%/C{args.mem_critical:.0f}%")
    print("=" * 64)

    # 3. 执行批量回测
    with timer.phase("批量回测"):
        backtester = BatchBacktester(
            strategy_name=args.strategy,
            params=params,
            data=data,
            fingerprint_path=str(fingerprint_path),
            results_path=str(results_path),
            mode=args.mode,
            cpu_workers=args.cpu,
            start_date=args.start,
            end_date=args.end,
            batch_size=args.batch_size,
            memory_watcher=watcher,
        )
        result = backtester.run(force_full=args.force_full)

    # 4. 汇总输出
    with timer.phase("汇总输出"):
        print(f"\n{result.plan.summary()}")
        print(result.summary())
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
            print(f"[已保存] 批量回测报告: {out_path}")

    # 5. 分阶段计时
    print("=" * 64)
    print("  分阶段耗时")
    print(timer.report())
    print("=" * 64)
    print(f"[缓存] 指纹仓库: {fingerprint_path}")
    print(f"[缓存] 结果缓存: {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
