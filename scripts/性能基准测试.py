# 文件名：scripts/性能基准测试.py
"""
性能基准测试 - 中文入口脚本（薄入口，第5项F / P3-3）
====================================================
一键运行指标性能基准测试 + 三模式回测引擎性能基准采集 + 性能优化统一验证。

功能:
  - 运行 tests/benchmarks/test_indicator_perf.py（全量计算 + 增量更新基准）
  - 运行三模式回测引擎性能基准采集（event/mixed/vectorized 耗时/吞吐/相对加速比，
    核心逻辑在 myquant/backtest/engine_benchmark.py）
  - 运行 tests/_verify_perf_optimization.py（A-E五项164项统一验证）
  - 输出性能汇总报告（各指标全量/增量延迟、吞吐量、阈值判定、三模式引擎加速比）

运行方式:
    # 完整运行（指标基准 + 引擎基准 + 优化验证）
    python scripts/性能基准测试.py

    # 仅运行性能基准（指标 + 三模式引擎）
    python scripts/性能基准测试.py --benchmark-only

    # 仅运行优化验证
    python scripts/性能基准测试.py --verify-only

    # 仅运行三模式引擎性能基准
    python scripts/性能基准测试.py --engine-benchmark-only

    # 快速模式（减少迭代次数，更快出结果）
    python scripts/性能基准测试.py --quick

    # 完整运行但跳过引擎基准（兼容旧行为）
    python scripts/性能基准测试.py --skip-engine-benchmark

设计原则（规则2/规则3/规则5）:
  - 薄入口：只做 参数解析→调用基准/验证脚本→汇总输出
  - 基准测试复用 tests/benchmarks/ 框架（与CI一致）
  - 引擎性能基准复用 myquant/backtest/engine_benchmark.py（业务逻辑不在脚本内）
  - 验证复用 tests/_verify_perf_optimization.py（A-E五项整合）
  - 入口脚本用中文名，与项目其他中文入口一致
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
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
    # 回退：脚本上一级
    return script_dir.parent


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="性能基准测试",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 指标性能基准 / 三模式引擎基准 / 优化验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/性能基准测试.py
  python scripts/性能基准测试.py --benchmark-only
  python scripts/性能基准测试.py --verify-only
  python scripts/性能基准测试.py --engine-benchmark-only
  python scripts/性能基准测试.py --quick
  python scripts/性能基准测试.py --skip-engine-benchmark""",
    )
    parser.add_argument("--benchmark-only", action="store_true",
                        help="仅运行性能基准（指标基准 + 三模式引擎基准），跳过优化验证")
    parser.add_argument("--verify-only", action="store_true",
                        help="仅运行优化验证，跳过性能基准")
    parser.add_argument("--engine-benchmark-only", action="store_true",
                        help="仅运行三模式回测引擎性能基准采集")
    parser.add_argument("--skip-engine-benchmark", action="store_true",
                        help="完整运行中跳过三模式引擎性能基准（兼容旧行为）")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式（减少迭代次数/规模，更快出结果）")
    return parser


def run_benchmark(project_root: Path, quick: bool = False) -> int:
    """运行指标性能基准测试"""
    script = project_root / "tests" / "benchmarks" / "test_indicator_perf.py"
    if not script.exists():
        print(f"[错误] 基准测试脚本不存在: {script}")
        return 1

    print(f"\n{'='*60}")
    print("  运行性能基准测试")
    print(f"  脚本：{script.name}")
    print(f"{'='*60}")

    env = {**os.environ, "PYTHONPATH": str(project_root)}
    if quick:
        env["BENCHMARK_QUICK"] = "1"

    t0 = time.perf_counter()
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, cwd=str(project_root), env=env,
        check=False,
    )
    elapsed = time.perf_counter() - t0

    # 输出基准结果（提取关键行）
    output = result.stdout
    lines = output.strip().split("\n")
    in_section = False
    for line in lines:
        # 打印测试项名称和阈值判定行
        if line.strip().startswith("[") and ("全量" in line or "增量" in line or "吞吐" in line):
            in_section = True
            print(f"  {line.strip()}")
        elif in_section and ("均值:" in line or "吞吐量:" in line or "阈值:" in line):
            print(f"    {line.strip()}")
            if "阈值:" in line:
                in_section = False

    print(f"\n  基准测试耗时: {elapsed:.2f}s")
    if result.returncode != 0:
        print(f"  [警告] 基准测试返回码: {result.returncode}")
        print(f"  stderr: {result.stderr[-500:] if result.stderr else '(无)'}")
    return result.returncode


def run_engine_benchmark(quick: bool = False) -> int:
    """运行三模式回测引擎性能基准采集（业务逻辑在 myquant/backtest/engine_benchmark.py）。

    Args:
        quick: 是否快速模式（减小规模/重复次数）。

    Returns:
        0=全部引擎成功；1=存在失败引擎。
    """
    from myquant.backtest.engine_benchmark import (
        DEFAULT_N_BARS,
        DEFAULT_N_SYMBOLS,
        DEFAULT_REPETITIONS,
        DEFAULT_SEED,
        QUICK_N_BARS,
        QUICK_N_SYMBOLS,
        QUICK_REPETITIONS,
        EngineBenchmark,
        EngineBenchmarkConfig,
    )

    n_bars = QUICK_N_BARS if quick else DEFAULT_N_BARS
    n_symbols = QUICK_N_SYMBOLS if quick else DEFAULT_N_SYMBOLS
    repetitions = QUICK_REPETITIONS if quick else DEFAULT_REPETITIONS

    print(f"\n{'='*60}")
    print("  三模式回测引擎性能基准采集（event/mixed/vectorized）")
    print(f"  规模: {n_symbols} 标的 × {n_bars} 根 × {repetitions} 次(平均) | seed={DEFAULT_SEED}")
    print(f"{'='*60}")

    config = EngineBenchmarkConfig(
        n_bars=n_bars,
        n_symbols=n_symbols,
        repetitions=repetitions,
        seed=DEFAULT_SEED,
    )
    report = EngineBenchmark(config).run()
    print(report.summary())

    ok = all(r.is_success for r in report.results)
    return 0 if ok else 1


def run_verification(project_root: Path) -> int:
    """运行性能优化统一验证（A-E五项）"""
    script = project_root / "tests" / "_verify_perf_optimization.py"
    if not script.exists():
        print(f"[错误] 统一验证脚本不存在: {script}")
        return 1

    print(f"\n{'='*60}")
    print("  运行性能优化统一验证（A-E五项）")
    print(f"  脚本：{script.name}")
    print(f"{'='*60}")

    env = {**os.environ, "PYTHONPATH": str(project_root)}
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, cwd=str(project_root), env=env,
        check=False,
    )

    # 输出汇总表
    output = result.stdout
    lines = output.strip().split("\n")
    in_summary = False
    for line in lines:
        if "统一验证汇总" in line:
            in_summary = True
        if in_summary:
            print(f"  {line}")
        if in_summary and line.startswith("=") and "全部通过" not in line and "验证失败" not in line:
            # 汇总表结束
            pass

    # 打印最终结论
    for line in lines:
        if "全部通过" in line or "验证失败" in line:
            print(f"  {line.strip()}")

    return result.returncode


def main() -> int:
    """主入口"""
    project_root = find_project_root()
    args = setup_argument_parser().parse_args()

    # 确保项目根在 sys.path（引擎基准在进程内导入 myquant）
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    # G1 EX-09A：装配撮合执行器工厂（薄入口脚本为合法装配层）
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

    print("=" * 60)
    print("  Pythonwdhmm 鲲鹏量化交易系统 — 性能基准测试")
    print(f"  项目根目录: {project_root}")
    print("=" * 60)

    total_rc = 0

    # 仅三模式引擎基准
    if args.engine_benchmark_only:
        total_rc = max(total_rc, run_engine_benchmark(quick=args.quick))
    else:
        # 1. 指标性能基准（除非 --verify-only）
        if not args.verify_only:
            total_rc = max(total_rc, run_benchmark(project_root, quick=args.quick))

            # 2. 三模式引擎性能基准（除非 --skip-engine-benchmark）
            if not args.skip_engine_benchmark:
                total_rc = max(total_rc, run_engine_benchmark(quick=args.quick))

        # 3. 优化验证（除非 --benchmark-only）
        if not args.benchmark_only:
            total_rc = max(total_rc, run_verification(project_root))

    # 最终结论
    print(f"\n{'='*60}")
    if total_rc == 0:
        print("  ✅ 性能基准（指标+三模式引擎）与优化验证全部通过")
    else:
        print(f"  ❌ 存在失败项（返回码: {total_rc}），请检查上方输出")
    print(f"{'='*60}")
    return total_rc


if __name__ == "__main__":
    sys.exit(main())
