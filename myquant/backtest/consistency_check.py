# 文件名：myquant/backtest/consistency_check.py
"""
backtest/consistency_check.py - P3-1 三模式净值一致性校验器
==========================================================
对 event / mixed / vectorized 三套引擎在同一策略、同一数据上的回测净值进行
逐点对拍，支持"绝对 + 相对"双阈值可配置，解决浮点数合法偏差误报问题。

设计原则（代码规则）:
  - 业务逻辑全量实现，无占位符；防御式编程显式校验输入边界。
  - 严格防未来函数：本模块只消费各引擎已产出的净值曲线（截至 t 时刻的回测结果），
    不引入任何未来数据；对拍仅做"已发生"结果的数值比较。
  - 绝对+相对双阈值（numpy isclose 语义）：
        判点通过  <=>  |a - b| <= abs_tol + rel_tol * |b|
      绝对容差兜底小净值（相对阈值在 b≈0 时失效），相对容差兜底大净值
      （绝对阈值在大净值下过严），避免单一阈值误报合法浮点偏差。
  - 分口径容差：mixed vs vectorized 共用 fee_common/order_matcher，应逐点一致（紧阈值）；
    event 为历史真值基准、费用口径不同（佣金基数含滑点，红线5禁止修改），
    故 event 参与的对拍用收敛级容差（默认相对 5e-3）。
  - 演示策略 DemoThreeModeStrategy 三套接口行为一致（target 序列 / mixed loop /
    on_bar + calc_buy_quantity 固定数量），保证三引擎在同一目标持仓下可对拍。

对外组件:
  - ComparisonTolerance : 绝对+相对双阈值（frozen dataclass）
  - PairComparison      : 单对引擎净值对拍结果
  - ConsistencyReport   : 三模式一致性校验汇总报告
  - ConsistencyChecker  : 校验器（运行三引擎 + 逐对对拍 + 汇总）
  - DemoThreeModeStrategy : 三模式演示策略桩（自测/演示用）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from myquant.backtest.engine import BacktestEngine
from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.backtest.vectorized_engine import VectorizedBacktestEngine
from myquant.core.logger import get_logger
from myquant.models.backtest import BacktestConfig, BacktestResult
from myquant.models.market_data import Bar
from myquant.models.trade import Position
from myquant.strategies.base import (
    Signal,
    SignalType,
    StrategyBase,
    StrategyContext,
)

logger = get_logger(__name__)

# ═══════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════

ENGINE_EVENT: str = "event"
ENGINE_MIXED: str = "mixed"
ENGINE_VECTORIZED: str = "vectorized"
SUPPORTED_ENGINES: tuple[str, ...] = (ENGINE_EVENT, ENGINE_MIXED, ENGINE_VECTORIZED)

DEFAULT_MODES: tuple[str, ...] = SUPPORTED_ENGINES

# 演示策略默认参数
DEFAULT_HOLD_START: int = 30       # 第 N 根起持有
DEFAULT_HOLD_SHARES: float = 10_000.0  # 固定目标持仓股数
DEFAULT_N_BARS: int = 300          # 演示行情每标的 K 线数量
DEFAULT_SEED: int = 7              # 演示行情随机种子（确定性可复现）

# 对拍时最多报告的错误日期采样数
MAX_MISMATCH_SAMPLES: int = 10
# 相对偏差计算时的分母下界（防止除零）
_REL_DENOM_FLOOR: float = 1e-12


# ═══════════════════════════════════════════════
#  双阈值
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class ComparisonTolerance:
    """净值对拍双阈值（绝对 + 相对，均可配置）。

    Attributes:
        abs_tol: 绝对容差（对接近 0 的小净值生效，避免相对阈值在分母≈0 时失效）。
        rel_tol: 相对容差（对大净值生效，避免绝对阈值在大净值下过严误报）。
    """

    abs_tol: float = 1e-3
    rel_tol: float = 1e-6

    def __post_init__(self) -> None:
        """防御校验：两个阈值必须为非负数。"""
        if self.abs_tol < 0:
            raise ValueError(f"abs_tol 必须 >= 0，收到: {self.abs_tol}")
        if self.rel_tol < 0:
            raise ValueError(f"rel_tol 必须 >= 0，收到: {self.rel_tol}")

    def band(self, reference: float) -> float:
        """计算以 reference 为基准的双阈值容差带宽度（isclose 语义）。

        公式: abs_tol + rel_tol * |reference|。
        本行仅做已产出净值的数值比较，不涉及未来数据。

        Args:
            reference: 参考净值（被比较对象）。

        Returns:
            容差带宽度。
        """
        return self.abs_tol + self.rel_tol * abs(reference)

    def is_close(self, value: float, reference: float) -> bool:
        """判断 value 与 reference 是否落在双阈值容差带内。

        Args:
            value: 待比较净值。
            reference: 参考净值。

        Returns:
            True 表示 |value - reference| <= band(reference)。
        """
        return abs(value - reference) <= self.band(reference)

    def to_dict(self) -> dict[str, float]:
        """导出为字典。"""
        return {"abs_tol": self.abs_tol, "rel_tol": self.rel_tol}


# 默认对拍容差（按引擎对，键为归一化排序元组）
#  - mixed vs vectorized：共用 fee_common/order_matcher，逐点紧阈值（rel 1e-6）
#  - event 参与的配对：历史真值基准、费用口径含滑点（红线5禁止修改），收敛级容差（rel 5e-3）
DEFAULT_PAIR_TOLERANCES: dict[tuple[str, str], ComparisonTolerance] = {
    (ENGINE_MIXED, ENGINE_VECTORIZED): ComparisonTolerance(
        abs_tol=1e-3, rel_tol=1e-6
    ),
    (ENGINE_EVENT, ENGINE_MIXED): ComparisonTolerance(
        abs_tol=1e-3, rel_tol=1e-3  # T6-2：5e-3→1e-3 收紧
    ),
    (ENGINE_EVENT, ENGINE_VECTORIZED): ComparisonTolerance(
        abs_tol=1e-3, rel_tol=1e-3  # T6-2：5e-3→1e-3 收紧
    ),
}


# ═══════════════════════════════════════════════
#  对拍结果
# ═══════════════════════════════════════════════

@dataclass
class PairComparison:
    """单对引擎净值对拍结果。

    Attributes:
        name: 对拍名称（如 "mixed_vs_vectorized"）。
        engine_a: 引擎 A 名称。
        engine_b: 引擎 B 名称。
        n_points: 参与对拍的公共净值点数。
        n_mismatch: 超出双阈值容差带的点数。
        max_abs_diff: 最大绝对偏差。
        max_rel_diff: 最大相对偏差。
        final_equity_a: 引擎 A 期末净值。
        final_equity_b: 引擎 B 期末净值。
        tolerance: 本对使用的双阈值。
        ok: 是否通过（n_mismatch == 0）。
        mismatch_dates: 超差点日期采样（最多 MAX_MISMATCH_SAMPLES 个）。
        reason: 失败原因说明（通过时为空）。
    """

    name: str
    engine_a: str
    engine_b: str
    n_points: int
    n_mismatch: int
    max_abs_diff: float
    max_rel_diff: float
    final_equity_a: float
    final_equity_b: float
    tolerance: ComparisonTolerance
    ok: bool
    mismatch_dates: list[str] = field(default_factory=list)
    reason: str = ""
    fee_basis_a: str = "per_order"
    fee_basis_b: str = "per_order"

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 可序列化字典。"""
        return {
            "name": self.name,
            "engine_a": self.engine_a,
            "engine_b": self.engine_b,
            "n_points": self.n_points,
            "n_mismatch": self.n_mismatch,
            "max_abs_diff": round(self.max_abs_diff, 8),
            "max_rel_diff": round(self.max_rel_diff, 10),
            "final_equity_a": round(self.final_equity_a, 6),
            "final_equity_b": round(self.final_equity_b, 6),
            "tolerance": self.tolerance.to_dict(),
            "ok": self.ok,
            "mismatch_dates": self.mismatch_dates,
            "reason": self.reason,
            # P2-02：两引擎费用摊薄口径披露（口径一致才紧对拍有据）
            "fee_basis_a": self.fee_basis_a,
            "fee_basis_b": self.fee_basis_b,
        }


@dataclass
class ConsistencyReport:
    """三模式净值一致性校验汇总报告。

    Attributes:
        strategy_name: 被校验策略名。
        config: 回测配置。
        engine_summaries: 各引擎回测摘要 {mode: dict}。
        comparisons: 全部对拍结果列表。
        overall_ok: 全部对拍通过则 True。
    """

    strategy_name: str
    config: BacktestConfig
    engine_summaries: dict[str, dict[str, Any]]
    comparisons: list[PairComparison]
    overall_ok: bool

    @property
    def passed_pairs(self) -> int:
        """通过的对拍对数。"""
        return sum(1 for c in self.comparisons if c.ok)

    @property
    def total_pairs(self) -> int:
        """对拍总对数。"""
        return len(self.comparisons)

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 可序列化字典。"""
        return {
            "strategy_name": self.strategy_name,
            "config": self.config.to_dict(),
            "engine_summaries": self.engine_summaries,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "passed_pairs": self.passed_pairs,
            "total_pairs": self.total_pairs,
            "overall_ok": self.overall_ok,
        }

    def summary(self) -> str:
        """格式化摘要输出（CLI 与自测展示）。"""
        lines: list[str] = [
            "=" * 64,
            f"  三模式净值一致性校验报告｜策略: {self.strategy_name}",
            (
                f"  标的: {len(self.config.symbols)} 个 | 期间: "
                f"{self.config.start_date} ~ {self.config.end_date}"
            ),
            "=" * 64,
        ]
        for mode, summary in self.engine_summaries.items():
            lines.append(
                f"  [{mode:<10}] 状态={summary['status']:<9} "
                f"期末净值={summary['final_equity']:>14.6f} "
                f"成交={summary['n_trades']:>4} 净值点={summary['n_points']:>4}"
            )
        lines.append("-" * 64)
        for c in self.comparisons:
            mark = "✅" if c.ok else "❌"
            lines.append(
                f"  {mark} {c.name:<26} 点数={c.n_points:>4} 超差={c.n_mismatch:>4} "
                f"max_abs={c.max_abs_diff:.4e} max_rel={c.max_rel_diff:.4e} "
                f"(abs_tol={c.tolerance.abs_tol:g}, rel_tol={c.tolerance.rel_tol:g})"
            )
            if not c.ok and c.mismatch_dates:
                lines.append(f"       超差日期示例: {', '.join(c.mismatch_dates[:5])}")
        lines.append("-" * 64)
        overall = "✅ 三模式净值一致" if self.overall_ok else "❌ 存在超出阈值偏差"
        lines.append(
            f"  对拍 {self.passed_pairs}/{self.total_pairs} 对通过 ｜ 整体结论: {overall}"
        )
        lines.append("=" * 64)
        return "\n".join(lines)


# ═══════════════════════════════════════════════
#  校验器
# ═══════════════════════════════════════════════

class ConsistencyChecker:
    """三模式净值一致性校验器。

    用法:
        checker = ConsistencyChecker()
        report = checker.check_all(config=config, strategy=strategy, data=data)
        ok = report.overall_ok
    """

    def __init__(
        self,
        pair_tolerances: dict[tuple[str, str], ComparisonTolerance] | None = None,
        abs_tol: float | None = None,
        rel_tol: float | None = None,
    ) -> None:
        """初始化校验器。

        Args:
            pair_tolerances: 按引擎对自定义容差；未给出时用 DEFAULT_PAIR_TOLERANCES。
            abs_tol: 全局绝对容差覆盖（对全部对拍对生效；None 表示不覆盖）。
            rel_tol: 全局相对容差覆盖（对全部对拍对生效；None 表示不覆盖）。

        Raises:
            ValueError: abs_tol/rel_tol 为负数时抛出（由 ComparisonTolerance 校验）。
        """
        base: dict[tuple[str, str], ComparisonTolerance] = dict(DEFAULT_PAIR_TOLERANCES)
        if pair_tolerances:
            for key, tol in pair_tolerances.items():
                if not isinstance(tol, ComparisonTolerance):
                    raise TypeError(
                        f"pair_tolerances 的值必须是 ComparisonTolerance，收到: {type(tol).__name__}"
                    )
                base[self._pair_key(*key)] = tol
        if abs_tol is not None or rel_tol is not None:
            override = ComparisonTolerance(
                abs_tol=1e-3 if abs_tol is None else abs_tol,
                rel_tol=1e-6 if rel_tol is None else rel_tol,
            )
            base = {k: override for k in base}
        self._pair_tolerances: dict[tuple[str, str], ComparisonTolerance] = base

    # ── 工具 ────────────────────────────────────────────────

    @staticmethod
    def _pair_key(engine_a: str, engine_b: str) -> tuple[str, str]:
        """归一化引擎对键（无序：('mixed','vectorized') == ('vectorized','mixed')）。

        Args:
            engine_a: 引擎 A 名称。
            engine_b: 引擎 B 名称。

        Returns:
            排序后的 (小, 大) 元组作为字典键。
        """
        return tuple(sorted((engine_a.strip().lower(), engine_b.strip().lower())))  # type: ignore[return-value]

    def _tolerance_for(self, engine_a: str, engine_b: str) -> ComparisonTolerance:
        """取指定引擎对的容差（未配置时回退默认双阈值）。

        Args:
            engine_a: 引擎 A 名称。
            engine_b: 引擎 B 名称。

        Returns:
            该对使用的 ComparisonTolerance。
        """
        return self._pair_tolerances.get(
            self._pair_key(engine_a, engine_b), ComparisonTolerance()
        )

    @staticmethod
    def _equity_series(result: BacktestResult) -> tuple[list[str], np.ndarray]:
        """提取回测结果的逐日组合净值序列 (日期列表, 净值数组)。

        对齐口径（关键）：多标的下 event/mixed 引擎按"每日期 × 每标的"记录净值点，
        同一日期内各标的持仓价格逐步更新（逐 symbol 处理），只有该日期的
        **最后一个点**才是当日完整组合净值；vectorized 引擎每日期只记 1 个组合点。
        因此统一取"每日期最后一个点"作为该日组合净值，消除记录粒度差异。

        Args:
            result: 回测结果。

        Returns:
            (日期列表, float64 净值数组)，按日期升序；净值曲线为空时返回空数组。
        """
        by_date: dict[str, float] = {}
        for p in result.equity_curve:
            if p.date:
                # 后写覆盖 → 保留每日期最后一个点（多标的下为完整组合净值）
                by_date[p.date] = p.equity
        if not by_date:
            return [], np.asarray([], dtype=np.float64)
        dates = sorted(by_date.keys())
        values = np.asarray([by_date[d] for d in dates], dtype=np.float64)
        return dates, values

    # ── 运行引擎 ────────────────────────────────────────────

    def run_engine(
        self,
        mode: str,
        config: BacktestConfig,
        strategy: StrategyBase,
        data: dict[str, pd.DataFrame],
    ) -> BacktestResult:
        """按模式构造并运行单引擎回测（每次运行前重置策略状态）。

        Args:
            mode: 引擎模式（event/mixed/vectorized）。
            config: 回测配置。
            strategy: 策略实例（须支持该模式）。
            data: 行情数据 {symbol: DataFrame}。

        Returns:
            BacktestResult 回测结果。

        Raises:
            ValueError: mode 非法时抛出。
            TypeError: strategy 不是 StrategyBase 实例时抛出。
        """
        mode_key = mode.strip().lower()
        if mode_key not in SUPPORTED_ENGINES:
            raise ValueError(
                f"非法引擎模式: {mode!r}（支持: {', '.join(SUPPORTED_ENGINES)}）"
            )
        if not isinstance(strategy, StrategyBase):
            raise TypeError(
                f"strategy 必须为 StrategyBase 实例，收到: {type(strategy).__name__}"
            )
        # 同一策略实例被三引擎先后复用，需重置内部计数状态
        reset = getattr(strategy, "reset", None)
        if callable(reset):
            reset()

        if mode_key == ENGINE_VECTORIZED:
            engine: Any = VectorizedBacktestEngine(
                config=config, strategy=strategy, data=data
            )
        elif mode_key == ENGINE_MIXED:
            engine = MixedBacktestEngine(config=config, strategy=strategy, data=data)
        else:
            engine = BacktestEngine(config=config, strategy=strategy, data=data)

        result = engine.run()
        # P2-02：把本引擎的费用摊薄口径挂到结果上，供对拍披露/防误判
        # （直构引擎未经 create_engine 装配，无 manifest，故从类常量直读）
        result.extra["fee_dilution_basis"] = getattr(engine, "FEE_DILUTION_BASIS", "per_order")
        if not result.is_success:
            logger.warning(
                "引擎 %s 回测未成功: status=%s, error=%s",
                mode_key,
                result.status,
                result.error_message,
            )
        return result

    # ── 单对对拍 ────────────────────────────────────────────

    def compare_pair(
        self,
        result_a: BacktestResult,
        result_b: BacktestResult,
        engine_a: str,
        engine_b: str,
        name: str,
    ) -> PairComparison:
        """对拍两引擎净值曲线（按日期对齐，逐点双阈值判定）。

        Args:
            result_a: 引擎 A 的回测结果。
            result_b: 引擎 B 的回测结果。
            engine_a: 引擎 A 名称（event/mixed/vectorized）。
            engine_b: 引擎 B 名称。
            name: 对拍名称。

        Returns:
            PairComparison 对拍结果。
        """
        dates_a, eq_a = self._equity_series(result_a)
        dates_b, eq_b = self._equity_series(result_b)
        tolerance = self._tolerance_for(engine_a, engine_b)
        # P2-02：两引擎费用摊薄口径（直构引擎从 result.extra 读）
        basis_a = result_a.extra.get("fee_dilution_basis", "per_order")
        basis_b = result_b.extra.get("fee_dilution_basis", "per_order")

        def _failed(reason: str) -> PairComparison:
            """构造失败对拍结果（空曲线/无公共日期等边界）。"""
            return PairComparison(
                name=name,
                engine_a=engine_a,
                engine_b=engine_b,
                n_points=0,
                n_mismatch=0,
                max_abs_diff=0.0,
                max_rel_diff=0.0,
                final_equity_a=float(eq_a[-1]) if eq_a.size else 0.0,
                final_equity_b=float(eq_b[-1]) if eq_b.size else 0.0,
                tolerance=tolerance,
                ok=False,
                mismatch_dates=[],
                reason=reason,
            )

        if eq_a.size == 0 or eq_b.size == 0:
            return _failed("存在空净值曲线，无法对拍")

        # 按日期对齐：取两引擎公共日期（净值曲线长度可能不同）
        map_b: dict[str, int] = {d: i for i, d in enumerate(dates_b)}
        common_dates: list[str] = []
        idx_a: list[int] = []
        idx_b: list[int] = []
        for i, d in enumerate(dates_a):
            j = map_b.get(d)
            if j is not None:
                common_dates.append(d)
                idx_a.append(i)
                idx_b.append(j)
        if not common_dates:
            return _failed("两引擎无公共净值日期，无法对拍")

        va = eq_a[np.asarray(idx_a, dtype=np.int64)]
        vb = eq_b[np.asarray(idx_b, dtype=np.int64)]
        abs_diff = np.abs(va - vb)
        rel_diff = abs_diff / np.maximum(np.abs(vb), _REL_DENOM_FLOOR)
        band = tolerance.band(vb)
        mismatch = abs_diff > band
        n_mismatch = int(mismatch.sum())

        mismatch_dates = [
            d for d, flag in zip(common_dates, mismatch.tolist()) if flag
        ][:MAX_MISMATCH_SAMPLES]

        ok = n_mismatch == 0
        reason = "" if ok else f"{n_mismatch}/{len(common_dates)} 个点超出双阈值容差带"
        # P2-02：两引擎费用摊薄口径不一致 → 显式标注为"已知口径差异"，
        # 避免未来 vectorized 算法变为 per_bar 时被误判为净值回归（口径差≠bug）。
        if basis_a != basis_b:
            note = f"⚠ 费用摊薄口径已知差异: {engine_a}={basis_a} vs {engine_b}={basis_b}"
            reason = f"{reason}；{note}" if reason else note
            ok = False  # 口径不一致本就该阻断"可信"宣传，不静默放过
        return PairComparison(
            name=name,
            engine_a=engine_a,
            engine_b=engine_b,
            n_points=len(common_dates),
            n_mismatch=n_mismatch,
            max_abs_diff=float(np.max(abs_diff)),
            max_rel_diff=float(np.max(rel_diff)),
            final_equity_a=float(va[-1]) if va.size else 0.0,
            final_equity_b=float(vb[-1]) if vb.size else 0.0,
            tolerance=tolerance,
            ok=ok,
            mismatch_dates=mismatch_dates,
            reason=reason,
            fee_basis_a=basis_a,
            fee_basis_b=basis_b,
        )

    # ── 汇总校验 ────────────────────────────────────────────

    def check_all(
        self,
        config: BacktestConfig,
        strategy: StrategyBase,
        data: dict[str, pd.DataFrame],
        modes: Sequence[str] = DEFAULT_MODES,
    ) -> ConsistencyReport:
        """运行三模式回测并逐对校验净值一致性。

        Args:
            config: 回测配置。
            strategy: 策略实例（须支持全部传入模式）。
            data: 行情数据 {symbol: DataFrame}。
            modes: 参与对拍的引擎模式序列（默认三模式全部）。

        Returns:
            ConsistencyReport 汇总报告。

        Raises:
            ValueError: modes 含非法模式或不足 2 种时抛出。
        """
        modes_tuple = tuple(m.strip().lower() for m in modes)
        invalid = [m for m in modes_tuple if m not in SUPPORTED_ENGINES]
        if invalid:
            raise ValueError(
                f"非法引擎模式: {invalid}（支持: {', '.join(SUPPORTED_ENGINES)}）"
            )
        if len(modes_tuple) < 2:
            raise ValueError(f"至少需要 2 种引擎模式做对拍，收到: {modes_tuple}")

        results: dict[str, BacktestResult] = {}
        engine_summaries: dict[str, dict[str, Any]] = {}
        for mode in modes_tuple:
            res = self.run_engine(mode, config, strategy, data)
            results[mode] = res
            engine_summaries[mode] = {
                "status": res.status,
                "final_equity": round(res.final_equity, 6),
                "n_trades": len(res.trades),
                "n_points": len(res.equity_curve),
                "error": res.error_message,
                # P2-02：费用摊薄口径披露（per_order/per_bar），对拍报告可审计
                "fee_dilution_basis": res.extra.get("fee_dilution_basis", "per_order"),
            }

        comparisons: list[PairComparison] = []
        for i in range(len(modes_tuple)):
            for j in range(i + 1, len(modes_tuple)):
                a, b = modes_tuple[i], modes_tuple[j]
                comparisons.append(
                    self.compare_pair(
                        results[a], results[b], a, b, f"{a}_vs_{b}"
                    )
                )

        overall_ok = all(c.ok for c in comparisons)
        report = ConsistencyReport(
            strategy_name=getattr(strategy, "name", "?"),
            config=config,
            engine_summaries=engine_summaries,
            comparisons=comparisons,
            overall_ok=overall_ok,
        )
        logger.info(
            "一致性校验完成: 策略=%s, 对拍 %d/%d 通过",
            report.strategy_name,
            report.passed_pairs,
            report.total_pairs,
        )
        return report


# ═══════════════════════════════════════════════
#  三模式演示策略桩
# ═══════════════════════════════════════════════

class DemoThreeModeStrategy(StrategyBase):
    """三模式演示策略桩：第 hold_start 根起持有 hold_shares 股并持有到结束。

    三套接口行为一致，保证三引擎在同一目标持仓下可对拍净值:
      - generate_signals_vectorized : 第 hold_start 根起目标持仓 = hold_shares
      - generate_mixed_loop         : 同一目标持仓（mixed target 模式）
      - on_bar + calc_buy_quantity  : 第 hold_start 根 BUY，固定买入 hold_shares
        （event / mixed 信号模式按策略覆盖的 calc_buy_quantity 下单）
    """

    name = "demo_three_mode"

    def __init__(
        self,
        hold_start: int = DEFAULT_HOLD_START,
        hold_shares: float = DEFAULT_HOLD_SHARES,
        **params: Any,
    ) -> None:
        """初始化演示策略。

        Args:
            hold_start: 起始持有 bar 序号（>= 0）。
            hold_shares: 固定目标持仓股数（> 0）。

        Raises:
            ValueError: 参数越界时抛出。
        """
        super().__init__(**params)
        if hold_start < 0:
            raise ValueError(f"hold_start 必须 >= 0，收到: {hold_start}")
        if hold_shares <= 0:
            raise ValueError(f"hold_shares 必须 > 0，收到: {hold_shares}")
        self.hold_start: int = int(hold_start)
        self.hold_shares: float = float(hold_shares)
        # 按标的记录 bar 序号：event/mixed 按"每日期 × 每标的"逐 bar 调用，
        # 必须按 symbol 分桶，才能与 vectorized（按标的独立目标序列）对齐。
        self._bar_by_symbol: dict[str, int] = {}

    def reset(self) -> None:
        """重置各标的 bar 计数（三引擎复用同一实例前调用）。"""
        self._bar_by_symbol.clear()

    def _next_bar_index(self, symbol: str) -> int:
        """取该标的当前 bar 序号并自增。

        Args:
            symbol: 标的代码。

        Returns:
            该标的本次调用前的 bar 序号（0 起）。
        """
        idx = self._bar_by_symbol.get(symbol, 0)
        self._bar_by_symbol[symbol] = idx + 1
        return idx

    def calc_buy_quantity(self, price: float, context: StrategyContext) -> float:
        """event/mixed 信号模式下按固定目标持仓买入（口径与 target 模式一致）。

        Args:
            price: 当前价格（未用于计算，固定数量）。
            context: 策略上下文。

        Returns:
            固定目标持仓股数。
        """
        return self.hold_shares

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：第 hold_start 根起目标持仓 hold_shares。

        本行数据截至 t 时刻：目标持仓序列由策略一次性给出，撮合由引擎在
        T+1 日开盘执行（防未来函数，与 mixed/event 口径一致）。

        Args:
            data: 单标的行情 DataFrame。

        Returns:
            目标持仓股数数组（长度 = len(data)）。
        """
        n = len(data)
        target = np.zeros(n, dtype=np.float64)
        start = min(self.hold_start, n)
        target[start:] = self.hold_shares
        return target

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Position | None,
    ) -> float:
        """mixed 引擎目标持仓接口：第 hold_start 根起返回 hold_shares。

        Args:
            bar: 当前 bar（T 日，已收盘）。
            context: 策略上下文。
            position: 当前持仓（可空）。

        Returns:
            目标持仓股数。
        """
        i = self._next_bar_index(bar.symbol)
        if i >= self.hold_start:
            return self.hold_shares
        return 0.0

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[Signal]:
        """event 引擎信号接口：各标的自第 hold_start 根发 BUY 信号。

        Args:
            bar: 当前 bar。
            context: 策略上下文。

        Returns:
            信号列表。
        """
        i = self._next_bar_index(bar.symbol)
        if i == self.hold_start:
            return [
                Signal(
                    symbol=bar.symbol,
                    signal_type=SignalType.BUY,
                    reason="demo_hold",
                )
            ]
        return []

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal | None:
        """事件信号单条接口（兼容基类抽象方法；本策略走 on_bar）。"""
        return None

    def engine_modes(self) -> list[str]:
        """三模式全部支持（按推荐优先级降序）。"""
        return [ENGINE_VECTORIZED, ENGINE_MIXED, ENGINE_EVENT]

    def supports_mode(self, mode: str) -> bool:
        """三模式全部支持。"""
        return mode.strip().lower() in SUPPORTED_ENGINES


# ═══════════════════════════════════════════════
#  演示行情（确定性，供自测与 --demo 入口使用）
# ═══════════════════════════════════════════════

def make_demo_data(
    symbols: Sequence[str] = ("000001.SZ", "000002.SZ", "600000.SH"),
    n_bars: int = DEFAULT_N_BARS,
    seed: int = DEFAULT_SEED,
) -> dict[str, pd.DataFrame]:
    """构造确定性演示行情（趋势 + 周期噪声），供自测与入口 --demo 使用。

    Args:
        symbols: 标的代码序列。
        n_bars: 每标的 K 线数量（> 20）。
        seed: 随机种子（同 seed 结果可复现）。

    Returns:
        {symbol: DataFrame}，列为 open/high/low/close/volume/amount，
        index 为工作日 DatetimeIndex。

    Raises:
        ValueError: n_bars 过小。
    """
    if n_bars < 20:
        raise ValueError(f"n_bars 至少 20 根以便策略预热，收到: {n_bars}")
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-02", periods=n_bars, freq="B")
    data: dict[str, pd.DataFrame] = {}
    for idx, symbol in enumerate(symbols):
        drift = 0.0006 + 0.0004 * idx
        noise = rng.normal(0.0, 0.01, n_bars)
        trend = drift * np.arange(n_bars)
        wave = 0.012 * np.sin(np.arange(n_bars) / (18.0 + idx))
        closes = 10.0 * (1.0 + trend + noise + wave)
        data[symbol] = pd.DataFrame(
            {
                "open": closes * 0.997,
                "high": closes * 1.01,
                "low": closes * 0.99,
                "close": closes,
                # P1-10：演示数据对齐生产链路（provider 派生前收盘价列）；
                # vectorized 已切 data_col 模式，缺该列即 fail-closed。
                "pre_close": np.concatenate([[np.nan], closes[:-1]]),
                "volume": 100_000.0 * (1.0 + 0.5 * rng.random(n_bars)),
                "amount": closes * 100_000.0,
            },
            index=dates,
        )
    return data


# ═══════════════════════════════════════════════
#  真实数据接入（P2-05：用生产 processed 数据替换 make_demo_data 合成行情）
# ═══════════════════════════════════════════════

# 默认 processed 数据目录（相对项目根）
DEFAULT_PROCESSED_DIR: str = "./datasets/processed"


def load_real_data(
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    processed_dir: str = DEFAULT_PROCESSED_DIR,
) -> dict[str, pd.DataFrame]:
    """从生产 processed 目录加载真实 OHLCV，供三引擎一致性对拍（P2-05）。

    与 make_demo_data 的区别：消费真实历史 K 线（中文表头），而非确定性合成
    行情；这样三引擎口径差异会落在真实分支上（费用基数 / 滑点 / 整手 / 日历）。

    ★★★ 防未来函数 ★★★
    - 逐标的经 HistoricalDataProvider.get_bars 加载，其内部严格按 end_date 截断
      之后的数据（本函数只传 start/end，不预测未来）。

    Args:
        symbols: 标的代码序列。
        start_date: 回测起始日期（YYYY-MM-DD）。
        end_date: 回测截止日期（YYYY-MM-DD，防未来关键参数）。
        processed_dir: processed 数据目录（默认 ./datasets/processed）。

    Returns:
        {symbol: DataFrame}，列为中文 OHLCV、index=日期升序；
        无数据或加载为空的标的被跳过并记 warning。

    Raises:
        ValueError: symbols 为空、或最终无任一标的成功加载时抛出。
    """
    if not symbols:
        raise ValueError("load_real_data: symbols 不能为空")
    # 直接从 data_access.provider 导入（data_loader 仅 re-export，新代码走真实实现层）
    from myquant.data_access.provider import HistoricalDataProvider

    provider = HistoricalDataProvider(processed_dir=processed_dir)
    data: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        df = provider.get_bars(symbol, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            logger.warning("真实数据加载为空，跳过该标的: %s（%s ~ %s）", symbol, start_date, end_date)
            continue
        data[symbol] = df
    if not data:
        raise ValueError(
            f"load_real_data: 所有标的均未加载到真实数据（{list(symbols)}），"
            f"检查 processed_dir={processed_dir} 与日期区间"
        )
    logger.info("真实数据加载完成: %d/%d 个标的（%s ~ %s）", len(data), len(symbols), start_date, end_date)
    return data


# ═══════════════════════════════════════════════
#  模块自测
# ═══════════════════════════════════════════════

def _main() -> int:
    """模块自测：三模式演示策略 + 合成行情，逐对校验并输出报告。

    Returns:
        0=全部通过，1=存在超出阈值偏差。
    """
    print("=" * 64)
    print("  consistency_check.py — 三模式净值一致性校验 自测 Demo")
    print("=" * 64)

    data = make_demo_data(n_bars=DEFAULT_N_BARS, seed=DEFAULT_SEED)
    config = BacktestConfig(
        strategy_name="demo_three_mode",
        symbols=list(data.keys()),
        start_date="2023-01-02",
        end_date="2024-01-31",
        initial_capital=1_000_000.0,
        frequency="1d",
    )

    checker = ConsistencyChecker()
    report = checker.check_all(
        config=config, strategy=DemoThreeModeStrategy(), data=data
    )

    print(report.summary())
    ok = report.overall_ok
    print(f"  自测结论: {'✅ 三模式净值一致' if ok else '❌ 存在超出阈值偏差'}")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_main())
