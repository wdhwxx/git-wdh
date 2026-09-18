# 文件名：myquant/backtest/async_monte_carlo.py
"""
async_monte_carlo.py - 异步蒙特卡洛模拟
========================================
基于 asyncio 并发执行多组蒙特卡洛模拟（多标的 / 多参数组合），
复用 monte_carlo.MonteCarloAnalyzer 的向量化核心，通过 asyncio.to_thread
叠加线程池调度，并发上限可配，保持输入顺序返回结果，
支持错误隔离、进度回传与 skip 跳过开关。

核心能力:
  - 多组并发: 对 (symbol, equity_curve, 参数) 任务列表并发执行 analyze
  - 并发限流: concurrency 信号量，避免线程爆炸与资源竞争
  - 顺序保持: 结果按输入顺序返回（对应标的/参数组），便于对照
  - 错误隔离: return_exceptions=True 时单组失败收集到 errors，不影响其他组
  - skip 开关: 任务级 / 全局级跳过，跳过组直接返回 skipped 空结果（零算力）
  - 同步包装: run_blocking 用 asyncio.run 一步到位，供报告/脚本直接调用

防未来函数（铁律）:
  - 输入仅用已实现的净值曲线（List[EquityPoint]），无任何未来数据
  - 复用 monte_carlo.MonteCarloAnalyzer（历史模拟法：对已发生的历史日收益
    做有放回重采样），源码无预测方法/无硬编码未来日期

设计原则（为实盘无缝对接准备）:
  - 任务定义 MonteCarloTask 携带全部输入，与 MonteCarloAnalyzer 参数一一对应
  - seed 逐任务可固定 → 每组完全可复现
  - 返回 AsyncMonteCarloResult 聚合容器，summary()/to_dict() 供报告/监控直接使用

使用方式:
    from myquant.backtest.async_monte_carlo import AsyncMonteCarlo, MonteCarloTask

    tasks = [
        MonteCarloTask(symbol="600519.SH", equity_curve=eq1, n_simulations=2000),
        MonteCarloTask(symbol="000001.SZ", equity_curve=eq2, seed=42),
    ]
    result = AsyncMonteCarlo(concurrency=4).run_blocking(tasks)
    print(result.summary())

依赖: asyncio, concurrent.futures, dataclasses, logging, typing
本地: myquant.backtest.monte_carlo (MonteCarloAnalyzer, MonteCarloResult),
      myquant.models.backtest.EquityPoint
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from myquant.backtest.monte_carlo import MonteCarloAnalyzer, MonteCarloResult
from myquant.core.logger import get_logger
from myquant.models.backtest import EquityPoint

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

DEFAULT_CONCURRENCY: int = 4
"""默认并发上限（同时执行的蒙特卡洛任务数）"""
DEFAULT_N_SIMULATIONS: int = 2000
"""默认单组模拟路径条数"""
DEFAULT_HORIZON_DAYS: int = 252
"""默认模拟期数（交易日，≈1年）"""


# ─────────────────────────────────────────────
# 任务定义
# ─────────────────────────────────────────────

@dataclass
class MonteCarloTask:
    """一组蒙特卡洛模拟任务的输入定义。

    Attributes:
        symbol: 标的代码（仅用于结果标识/进度回传，不参与计算）。
        equity_curve: 历史净值曲线（List[EquityPoint]）。
        n_simulations: 该组模拟路径条数。
        horizon_days: 该组模拟期数（交易日）。
        seed: 该组随机种子（None=真随机；固定则完全可复现）。
        skip: 是否跳过该组（None=继承 AsyncMonteCarlo 全局 skip 设置）。
    """

    symbol: str
    equity_curve: Sequence[EquityPoint]
    n_simulations: int = DEFAULT_N_SIMULATIONS
    horizon_days: int = DEFAULT_HORIZON_DAYS
    seed: Optional[int] = None
    skip: Optional[bool] = None

    def to_analyzer(self, global_skip: bool = False) -> MonteCarloAnalyzer:
        """构造对应参数的 MonteCarloAnalyzer。

        Args:
            global_skip: 全局 skip 设置（任务自身未指定 skip 时继承）。

        Returns:
            配置好的分析器实例。
        """
        skip = global_skip if self.skip is None else self.skip
        return MonteCarloAnalyzer(
            n_simulations=self.n_simulations,
            horizon_days=self.horizon_days,
            seed=self.seed,
            skip=skip,
        )

    def to_dict(self) -> Dict[str, Any]:
        """导出为字典。"""
        return {
            "symbol": self.symbol,
            "n_simulations": self.n_simulations,
            "horizon_days": self.horizon_days,
            "seed": self.seed,
            "skip": self.skip,
            "equity_points": len(self.equity_curve),
        }


# ─────────────────────────────────────────────
# 结果聚合容器
# ─────────────────────────────────────────────

@dataclass
class AsyncMonteCarloResult:
    """异步批量蒙特卡洛结果聚合容器。

    Attributes:
        results: 按输入顺序排列的模拟结果（跳过组为 skipped 空结果）。
        errors: 失败的 (symbol, exception) 列表（return_exceptions=True 时非空）。
        elapsed_seconds: 总耗时（秒）。
    """

    results: List[MonteCarloResult] = field(default_factory=list)
    errors: List[Tuple[str, Exception]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def completed_count(self) -> int:
        """成功完成的模拟组数（不含跳过组）。"""
        return sum(1 for r in self.results if not r.skipped)

    @property
    def skipped_count(self) -> int:
        """被跳过的模拟组数。"""
        return sum(1 for r in self.results if r.skipped)

    @property
    def failed_count(self) -> int:
        """失败的模拟组数。"""
        return len(self.errors)

    @property
    def total_count(self) -> int:
        """任务总数。"""
        return len(self.results) + len(self.errors)

    def summary(self) -> str:
        """生成汇总报告文本。"""
        lines = ["异步蒙特卡洛模拟报告"]
        lines.append("=" * 60)
        lines.append(f"任务总数: {self.total_count}")
        lines.append(f"成功完成: {self.completed_count}")
        lines.append(f"跳过:     {self.skipped_count}")
        lines.append(f"失败:     {self.failed_count}")
        lines.append(f"总耗时:   {self.elapsed_seconds:.3f}s")
        if self.results:
            lines.append("-" * 60)
            lines.append(f"{'标的':<12} {'状态':<6} {'中位收益':>10} {'中位回撤':>10} {'破产概率':>8}")
            for r in self.results:
                status = "SKIP" if r.skipped else "DONE"
                med_ret = "-" if r.skipped else f"{r.median_final_return:.2%}"
                med_dd = "-" if r.skipped else f"{r.median_max_drawdown:.2%}"
                ruin = "-" if r.skipped else f"{r.prob_ruin:.2%}"
                lines.append(f"{_symbol_of(r):<12} {status:<6} {med_ret:>10} {med_dd:>10} {ruin:>8}")
        if self.errors:
            lines.append("-" * 60)
            lines.append("失败明细:")
            for symbol, exc in self.errors:
                lines.append(f"  - {symbol}: {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """导出为结构化字典（JSON 安全）。"""
        return {
            "total_count": self.total_count,
            "completed_count": self.completed_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "elapsed_seconds": self.elapsed_seconds,
            "results": [r.to_dict() for r in self.results],
            "errors": [
                {"symbol": s, "error_type": type(e).__name__, "message": str(e)}
                for s, e in self.errors
            ],
        }


def _symbol_of(result: MonteCarloResult) -> str:
    """从结果中尽量还原标的（to_dict 未携带 symbol，任务循环时另行标注）。"""
    return getattr(result, "_task_symbol", "-")


# ─────────────────────────────────────────────
# 异步蒙特卡洛执行器
# ─────────────────────────────────────────────

class AsyncMonteCarlo:
    """异步蒙特卡洛执行器：并发运行多组蒙特卡洛模拟。

    Args:
        concurrency: 最大并发任务数（>=1）。
        return_exceptions: True 时单组异常收集到 errors，不影响其他组；
                           False 时任一任务异常立即向上抛出。
        progress_cb: 进度回调 callable(done, total, symbol)；None 不回调。
        skip: 全局跳过开关（True 时所有未显式指定 skip 的任务均跳过）。
        logger: 自定义 logger（默认模块 logger）。
    """

    def __init__(
        self,
        concurrency: int = DEFAULT_CONCURRENCY,
        return_exceptions: bool = False,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        skip: bool = False,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency 必须 >= 1，收到: {concurrency}")
        if not isinstance(return_exceptions, bool):
            raise TypeError(
                f"return_exceptions 必须为 bool，收到: {type(return_exceptions).__name__}"
            )
        if not isinstance(skip, bool):
            raise TypeError(f"skip 必须为 bool，收到: {type(skip).__name__}")
        self.concurrency: int = concurrency
        self.return_exceptions: bool = return_exceptions
        self.progress_cb: Optional[Callable[[int, int, str], None]] = progress_cb
        self.skip: bool = skip
        self._logger: logging.Logger = logger_ or logger

    # ── 异步主入口 ──

    async def run(self, tasks: Sequence[MonteCarloTask]) -> AsyncMonteCarloResult:
        """异步执行多组蒙特卡洛模拟。

        Args:
            tasks: 任务列表（非空）。

        Returns:
            AsyncMonteCarloResult：结果按输入顺序排列。

        Raises:
            ValueError: tasks 为空。
            RuntimeError: 在事件循环之外调用本方法时抛出（应用 run_blocking）。
        """
        if not tasks:
            raise ValueError("tasks 不能为空")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "run() 必须在事件循环内调用；同步场景请使用 run_blocking()"
            )

        semaphore = asyncio.Semaphore(self.concurrency)
        total = len(tasks)
        done_count = 0

        async def _run_one(task: MonteCarloTask, index: int) -> Tuple[int, Optional[MonteCarloResult], Optional[Exception]]:
            async with semaphore:
                analyzer = task.to_analyzer(global_skip=self.skip)
                if analyzer.skip:
                    # 跳过：直接构造 skipped 空结果，不入线程池（零算力）
                    result = analyzer.analyze([])
                    result._task_symbol = task.symbol  # type: ignore[attr-defined]
                    return index, result, None
                try:
                    if not task.equity_curve or len(task.equity_curve) < 2:
                        raise ValueError(
                            f"任务 {task.symbol} 的净值曲线为空或不足 2 个点"
                        )
                    result = await asyncio.to_thread(analyzer.analyze, task.equity_curve)
                    result._task_symbol = task.symbol  # type: ignore[attr-defined]
                    return index, result, None
                except Exception as exc:  # noqa: BLE001 - 由调用方决定是否吞掉
                    if not self.return_exceptions:
                        raise
                    return index, None, exc
                finally:
                    nonlocal done_count
                    done_count += 1
                    if self.progress_cb is not None:
                        try:
                            self.progress_cb(done_count, total, task.symbol)
                        except Exception:
                            self._logger.warning("进度回调执行失败", exc_info=True)

        started = time.perf_counter()
        gathered = await asyncio.gather(
            *(_run_one(task, i) for i, task in enumerate(tasks)),
            return_exceptions=self.return_exceptions,
        )
        elapsed = time.perf_counter() - started

        # 按输入顺序重排，分离结果与错误
        ordered: List[Tuple[int, Optional[MonteCarloResult], Optional[Exception]]] = []
        for item in gathered:
            if isinstance(item, BaseException) and not isinstance(item, Exception):
                raise item  # 系统级异常（如 CancelledError）直接上抛
            if isinstance(item, Exception):
                # 当前任务无 index 信息，按序补位
                ordered.append((len(ordered), None, item))
            else:
                ordered.append(item)

        results: List[MonteCarloResult] = []
        errors: List[Tuple[str, Exception]] = []
        for _, result, exc in ordered:
            if exc is not None:
                symbol = tasks[len(results) + len(errors)].symbol
                errors.append((symbol, exc))
            elif result is not None:
                results.append(result)
            else:
                symbol = tasks[len(results) + len(errors)].symbol
                errors.append((symbol, RuntimeError("未知异常：无返回结果")))

        self._logger.info(
            "异步蒙特卡洛完成: total=%d, done=%d, skip=%d, fail=%d, %.3fs",
            total, len(results), len(tasks) - len(results) - len(errors), len(errors), elapsed,
        )
        return AsyncMonteCarloResult(
            results=results,
            errors=errors,
            elapsed_seconds=elapsed,
        )

    # ── 同步包装 ──

    def run_blocking(self, tasks: Sequence[MonteCarloTask]) -> AsyncMonteCarloResult:
        """同步执行多组蒙特卡洛模拟（asyncio.run 包装，供脚本/报告直接调用）。

        Args:
            tasks: 任务列表（非空）。

        Returns:
            AsyncMonteCarloResult。

        Raises:
            ValueError: tasks 为空或非法。
        """
        if not tasks:
            raise ValueError("tasks 不能为空")
        return asyncio.run(self.run(tasks))


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import numpy as _np

    print("=" * 60)
    print("  async_monte_carlo.py — 异步蒙特卡洛 自测 Demo")
    print("=" * 60)

    ok_count = [0]
    total_count = [0]

    def _ok(cond: bool, label: str) -> None:
        total_count[0] += 1
        mark = "✓" if cond else "✗"
        print(f"    {mark} {label}")
        if cond:
            ok_count[0] += 1

    def _make_equity(n_points: int, base: float = 1.0, vol: float = 0.01,
                     start_date: str = "2023-01-02") -> List[EquityPoint]:
        """构造模拟净值曲线（确定性伪随机，用于测试）。"""
        from datetime import datetime as _dt, timedelta as _td
        rng = _np.random.default_rng(42)
        returns = rng.normal(0.0002, vol, n_points - 1)
        equity = base * _np.cumprod(1.0 + returns)
        equity = _np.concatenate(([base], equity))
        base_date = _dt.strptime(start_date, "%Y-%m-%d")
        points: List[EquityPoint] = []
        for i in range(n_points):
            d = base_date + _td(days=i)
            e = float(equity[i])
            points.append(EquityPoint(
                timestamp=d.timestamp(),
                date=d.strftime("%Y-%m-%d"),
                equity=e,
            ))
        return points

    # ── 1) 构造多组任务并同步执行 ──
    print("\n[1] 多组并发执行")
    eq1 = _make_equity(200, base=1.0)
    eq2 = _make_equity(200, base=1.0, vol=0.02)
    eq3 = _make_equity(200, base=1.0, vol=0.005)
    tasks = [
        MonteCarloTask(symbol="600519.SH", equity_curve=eq1, n_simulations=500, seed=1),
        MonteCarloTask(symbol="000001.SZ", equity_curve=eq2, n_simulations=500, seed=2),
        MonteCarloTask(symbol="300750.SZ", equity_curve=eq3, n_simulations=500, seed=3),
    ]
    mc = AsyncMonteCarlo(concurrency=2)
    res = mc.run_blocking(tasks)
    _ok(len(res.results) == 3, "三组结果全部返回")
    _ok(res.completed_count == 3, "三组均完成")
    _ok(res.failed_count == 0 and res.skipped_count == 0, "无失败/跳过")
    _ok(len(res.results[0].final_returns) == 500, "每组 final_returns 长度 = n_simulations")
    _ok(res.results[0].n_simulations == 500, "结果携带正确参数")
    _ok(res.elapsed_seconds >= 0.0, "elapsed 已记录")
    _ok(res.results[0].seed == 1 and res.results[2].seed == 3, "各组 seed 独立保留")

    # ── 2) 顺序保持 ──
    print("\n[2] 顺序保持")
    _ok(all(r.seed == t.seed for r, t in zip(res.results, tasks)), "结果顺序与输入一致")

    # ── 3) 任务级 skip ──
    print("\n[3] 任务级 skip")
    tasks_skip = [
        MonteCarloTask(symbol="A", equity_curve=eq1, n_simulations=100, seed=1),
        MonteCarloTask(symbol="B", equity_curve=eq1, n_simulations=100, seed=2, skip=True),
        MonteCarloTask(symbol="C", equity_curve=eq1, n_simulations=100, seed=3),
    ]
    res_skip = AsyncMonteCarlo().run_blocking(tasks_skip)
    _ok(res_skip.skipped_count == 1, "跳过组计数为 1")
    _ok(res_skip.completed_count == 2, "完成组计数为 2")
    _ok(res_skip.results[1].skipped is True, "第 2 组结果为 skipped")
    _ok(len(res_skip.results[1].final_returns) == 0, "跳过组无模拟数据")

    # ── 4) 全局 skip（任务未显式指定时继承） ──
    print("\n[4] 全局 skip 继承")
    tasks_no_skip_flag = [
        MonteCarloTask(symbol="A", equity_curve=eq1, n_simulations=100),
        MonteCarloTask(symbol="B", equity_curve=eq1, n_simulations=100, skip=False),
    ]
    res_global_skip = AsyncMonteCarlo(skip=True).run_blocking(tasks_no_skip_flag)
    _ok(res_global_skip.skipped_count == 1, "未显式指定 skip 的任务继承全局跳过")
    _ok(res_global_skip.completed_count == 1, "显式 skip=False 的任务正常执行")

    # ── 5) 错误隔离（return_exceptions=True） ──
    print("\n[5] 错误隔离")
    tasks_bad = [
        MonteCarloTask(symbol="OK1", equity_curve=eq1, n_simulations=100, seed=1),
        MonteCarloTask(symbol="BAD", equity_curve=[], n_simulations=100, seed=2),
        MonteCarloTask(symbol="OK2", equity_curve=eq2, n_simulations=100, seed=3),
    ]
    res_err = AsyncMonteCarlo(return_exceptions=True).run_blocking(tasks_bad)
    _ok(res_err.failed_count == 1, "1 组失败被收集")
    _ok(res_err.completed_count == 2, "其余 2 组正常完成")
    _ok(res_err.errors[0][0] == "BAD", "错误关联正确标的")
    _ok(len(res_err.results) == 2, "成功结果 2 条")

    # ── 6) 进度回调 ──
    print("\n[6] 进度回调")
    progress = []
    mc_p = AsyncMonteCarlo(concurrency=2, progress_cb=lambda d, t, s: progress.append((d, t, s)))
    mc_p.run_blocking(tasks)
    _ok(len(progress) == len(tasks), "进度回调次数 = 任务数")
    _ok(progress[-1][0] == len(tasks), "最终进度 = 任务总数")
    _ok(all(t == len(tasks) for _, t, _ in progress), "回调 total 参数正确")

    # ── 7) 空任务校验 ──
    print("\n[7] 参数校验")
    try:
        AsyncMonteCarlo().run_blocking([])
        _ok(False, "空任务应抛 ValueError")
    except ValueError:
        _ok(True, "空任务正确抛 ValueError")
    try:
        AsyncMonteCarlo(concurrency=0)
        _ok(False, "concurrency=0 应抛 ValueError")
    except ValueError:
        _ok(True, "concurrency=0 正确抛 ValueError")
    try:
        AsyncMonteCarlo(return_exceptions="yes")
        _ok(False, "return_exceptions 非 bool 应抛 TypeError")
    except TypeError:
        _ok(True, "return_exceptions 非 bool 正确抛 TypeError")

    # ── 8) 汇总与序列化 ──
    print("\n[8] 汇总与序列化")
    summary = res.summary()
    _ok("异步蒙特卡洛模拟报告" in summary, "summary 含标题")
    _ok("600519.SH" in summary, "summary 含标的")
    d = res.to_dict()
    _ok(d["total_count"] == 3 and d["completed_count"] == 3, "to_dict 计数正确")
    _ok("results" in d and "errors" in d, "to_dict 结构完整")

    # ── 9) 确定性复现（同 seed 同结果） ──
    print("\n[9] 确定性复现")
    t1 = MonteCarloTask(symbol="R", equity_curve=eq1, n_simulations=200, seed=7)
    t2 = MonteCarloTask(symbol="R", equity_curve=eq1, n_simulations=200, seed=7)
    r1 = AsyncMonteCarlo().run_blocking([t1]).results[0]
    r2 = AsyncMonteCarlo().run_blocking([t2]).results[0]
    _ok(r1.median_final_return == r2.median_final_return, "同 seed 结果完全一致")

    print("\n" + "=" * 60)
    print(f"  自测结果: {ok_count[0]}/{total_count[0]} 项通过")
    if ok_count[0] == total_count[0]:
        print("  ✓ async_monte_carlo.py 异步蒙特卡洛 全部自测通过")
    else:
        print(f"  ✗ 有 {total_count[0] - ok_count[0]} 项未通过，请检查！")
    print("=" * 60)
