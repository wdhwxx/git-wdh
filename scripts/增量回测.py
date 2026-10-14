# 文件名：scripts/增量回测.py
"""
增量回测 - 中文入口脚本（薄入口）
================================
数据指纹识别变更标的，只对变更标的执行回测，未变更标的复用上次结果缓存，
大幅减少重复算力；--force-full 开关强制全量重算。

接入的 P2 成果:
  - data_fingerprint + FingerprintStore : 确定性数据指纹 + 变更识别（新增/内容/结构变化）
  - parallel_factor_context             : 并行重算变更标的时自动关闭 factor_cache L1 缓存
  - StageTimer                          : 分阶段计时（数据准备/指纹计算/并行重算/缓存落盘）

功能:
  - 增量模式（默认）：指纹识别变更，只重算变更标的，其余复用缓存
  - --force-full：忽略指纹差异，全部标的强制重算
  - --demo：演示完整流程（首次全量 → 模拟修改一个标的 → 增量只重算该标的）
  - 结果与指纹持久化到缓存目录，下次运行自动识别变更

运行方式:
    # 增量运行（首次自动全量建立基线）
    python scripts/增量回测.py

    # 强制全量
    python scripts/增量回测.py --force-full

    # 演示流程（全量 → 模拟变更 → 增量只重算变更）
    python scripts/增量回测.py --demo

    # 自定义标的/缓存目录
    python scripts/增量回测.py --symbols 000001.SZ,600519.SH --cache-dir cache/inc_bt

设计原则（规则2/规则3/规则5）:
  - 薄入口：只做 参数解析 → 调用 incremental_backtest → 分阶段计时汇总输出
  - 任务函数 evaluate_symbol 位于 myquant.backtest.incremental_backtest（真实模块，
    满足 Windows spawn 下子进程 pickle 反序列化约束）
  - 结果缓存按 fingerprint_hash 匹配，指纹不匹配自动重算（正确性兜底）
"""

import argparse
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
        prog="增量回测",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 数据指纹增量回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/增量回测.py
  python scripts/增量回测.py --force-full
  python scripts/增量回测.py --demo
  python scripts/增量回测.py --symbols 000001.SZ,600519.SH --cache-dir cache/inc_bt""",
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
    parser.add_argument("--force-full", action="store_true",
                        help="强制全量重算（忽略指纹差异）")
    parser.add_argument("--demo", action="store_true",
                        help="演示流程：首次全量 → 模拟修改一个标的 → 增量只重算变更")
    parser.add_argument("--cpu", type=int, default=None,
                        help="计算进程池大小（默认自动）")
    parser.add_argument("--cache-dir", default="results/incremental_backtest",
                        help="缓存目录（指纹仓库 + 结果缓存，默认 results/incremental_backtest）")
    parser.add_argument("--start", default="2023-01-02",
                        help="回测起始日期（默认 2023-01-02）")
    parser.add_argument("--end", default="2024-01-31",
                        help="回测结束日期（默认 2024-01-31）")
    return parser


def _build_demo_data(symbols: List[str], n_bars: int, seed: int):
    """构造演示行情（延迟导入避免脚本顶部 import 副作用）。"""
    from myquant.backtest.param_optimizer import make_demo_data

    return make_demo_data(symbols=symbols, n_bars=n_bars, seed=seed)


def _run_once(timer, backtester, tag: str, force_full: bool):
    """执行一次回测并打印报告（含分阶段计时）。

    Returns:
        本次调度结果。
    """
    with timer.phase(tag):
        result = backtester.run(force_full=force_full)
    print(f"\n[{tag}] {result.plan.summary()}")
    print(result.summary())
    return result


def main() -> int:
    """主入口：解析参数 → 执行增量/全量回测 → 分阶段计时输出。"""
    project_root = find_project_root()
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    import json

    from myquant.backtest.incremental_backtest import IncrementalBacktester
    from myquant.utils.timer import StageTimer

    args = setup_argument_parser().parse_args()
    timer = StageTimer()

    # 1. 数据准备
    with timer.phase("数据准备"):
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            print("[错误] --symbols 不能为空")
            return 2
        data = _build_demo_data(symbols, args.n_bars, args.seed)

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

    cache_dir = Path(args.cache_dir)
    fingerprint_path = cache_dir / "fingerprints.json"
    results_path = cache_dir / "results.json"

    print("=" * 64)
    print("  数据指纹增量回测")
    print(f"  项目根: {project_root}")
    print(f"  标的: {len(symbols)} 个 | 策略: {args.strategy} | 参数: {params}")
    print(f"  缓存: {cache_dir}")
    print("=" * 64)

    # 3. 执行回测
    def _make_backtester():
        return IncrementalBacktester(
            strategy_name=args.strategy,
            params=params,
            data=data,
            fingerprint_path=str(fingerprint_path),
            results_path=str(results_path),
            cpu_workers=args.cpu,
            start_date=args.start,
            end_date=args.end,
        )

    if args.demo:
        # 演示流程：首次全量 → 修改一标的 → 增量只重算变更
        bt1 = _make_backtester()
        _run_once(timer, bt1, "首次运行", force_full=True)

        # 模拟数据更新：修改最后一个标的最后一日收盘价
        demo_symbol = symbols[-1]
        modified = data[demo_symbol].copy()
        modified.iloc[-1, modified.columns.get_loc("close")] *= 1.1
        data[demo_symbol] = modified
        print(f"\n[模拟变更] 修改 {demo_symbol} 最后一日收盘价")

        bt2 = _make_backtester()
        r2 = _run_once(timer, bt2, "增量运行", force_full=False)
        assert r2.recomputed_count == 1 and r2.plan.changed == [demo_symbol], (
            f"增量应只重算 {demo_symbol}"
        )
        print(f"\n[验证] ✓ 增量只重算 {demo_symbol}，其余 {r2.reused_count} 个复用缓存")
    else:
        bt = _make_backtester()
        _run_once(timer, bt, "回测运行", force_full=args.force_full)

    # 4. 分阶段计时报告
    print("=" * 64)
    print("  分阶段耗时")
    print(timer.report())
    print("=" * 64)
    print(f"[缓存] 指纹仓库: {fingerprint_path}")
    print(f"[缓存] 结果缓存: {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
