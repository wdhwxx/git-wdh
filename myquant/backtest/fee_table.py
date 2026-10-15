# 文件名：myquant/backtest/fee_table.py
"""
backtest/fee_table.py - 费用查表加速模块
==========================================
面向极大批量参数扫描的**可选**费用加速层，默认关闭。

> **接线状态（P2-06 评估，2026-10-14）**：本模块当前为"已实现未接线"的待命特性——
> 生产代码（engine / mixed_engine / vectorized_engine）均直接使用 fee_common.FeeCalculator，
> 全仓 `import fee_table / TableFeeCalculator` 仅见于本模块自测（TestFeeTable 27 项），
> 运行时零消费。**结论：保留不删**——它是为千万级成交批量扫描预留的近似加速路径，
> 有完整测试守护与精度论证（断点入网 RATIO/FIXED 零误差）；待真实出现批量性能瓶颈时
> 再由引擎显式启用（开启即触发精度降级警告）。当前不删、不接线，仅标注状态。

背景:
  fee_common.FeeCalculator 提供精确费用计算（标量 + 向量化）。
  在千万级成交（超大批量参数扫描）场景下，可改用查表 + 插值替代每次实时计算，
  以换取常数时间查找；代价是近似机制带来的潜在精度损失（越界 clamp、非分段线性模型）。

设计原则:
  - 默认关闭: 查表不改变 FeeCalculator 默认行为，精确计算始终是唯一真相来源；
  - 开启必警告: 一旦显式启用查表，立即输出精度降级警告，禁止静默使用；
  - 线性网格 + 断点入网: 佣金是分段线性函数（常数段 + 比例段，拐点 = min_commission/rate），
    采用线性等距网格，并将拐点强制纳入网格点，使 RATIO/FIXED 模型下插值**精确零误差**；
  - 线性插值: np.interp 在网格上做分段线性插值，对分段线性函数在断点入网时无偏差；
  - 越界 clamp: 查表金额超出 [min_amount, max_amount] 时取端点值（已知近似行为，不报错）；
  - 滑点不走查表: 滑点是价格 × bps 纯线性，且与金额无关，直接走 SlippageModel 精确计算；
  - 防未来函数: 查表只依赖成交金额与模型参数，不涉及任何行情时间序列。

精度说明:
  - 当前 CommissionModel 的 RATIO / FIXED 均为分段线性，断点入网后查表误差 ≈ 0；
  - 未来若引入非线性费率（如阶梯、波动率定价），必须将全部断点纳入网格，
    否则插值会引入偏差——这正是"开启必输出精度警告"的原因。

类清单:
  - FeeTable               : 预计算费用查找表（佣金/印花税/过户费）
  - TableFeeCalculator     : 使用查表路径的费用计算器（FeeCalculator 子类，默认关闭）
  - build_fee_table        : 从 CommissionModel 构建查找表
  - estimate_max_error     : 评估查表 vs 精确计算的最大相对误差
  - enable_table_lookup    : 全局启用开关（启用即输出精度警告）

依赖:
  标准库: logging, typing
  第三方: numpy
  本地  : myquant.models.backtest (CommissionModel, SlippageModel, CommissionType)
          myquant.models.trade   (OrderSide)
          myquant.backtest.fee_common (FeeCalculator, FeeBreakdown, normalize_side, _validate_notional)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

import numpy as np

from myquant.backtest.fee_common import (
    FeeCalculator,
    _validate_notional,
    normalize_side,
)
from myquant.models.backtest import CommissionModel, CommissionType, SlippageModel
from myquant.models.trade import OrderSide

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 查表金额范围默认值（元）
DEFAULT_MIN_AMOUNT: float = 1_000.0           # 最小成交金额（一手低价股约 1000 元）
DEFAULT_MAX_AMOUNT: float = 1_000_000_000.0   # 最大成交金额（10 亿元）

# 默认线性网格档位数（断点额外并入，实际档位 >= bins）
DEFAULT_BINS: int = 4096

# 精度警告文本（启用查表时输出，禁止静默使用）
PRECISION_WARNING: str = (
    "费用查表加速已启用：费用由预计算表 + 线性插值得到，为近似机制。"
    "当前 RATIO/FIXED 分段线性模型在断点入网下误差≈0；"
    "若引入非线性费率或发生越界 clamp，会产生偏差。"
    "生产基准请以 fee_common.FeeCalculator 精确计算为准。"
)

# 查表一致性容差（自测与 CI 用）
_ERROR_TOLERANCE: float = 0.001  # 0.1% 相对误差上界（RATIO 断点入网下应接近 0）


# ─────────────────────────────────────────────
# 全局启用开关
# ─────────────────────────────────────────────

_table_lookup_enabled: bool = False


def is_table_lookup_enabled() -> bool:
    """查询费用查表加速是否已全局启用。

    Returns:
        True 表示已启用（后续构建的计算器默认使用查表路径）。
    """
    return _table_lookup_enabled


def enable_table_lookup(enabled: bool = True) -> bool:
    """全局启用/关闭费用查表加速。

    状态从关闭跃迁到启用时，输出精度降级警告（每次跃迁一次，防刷屏）。
    默认关闭——调用方必须显式开启才生效。

    Args:
        enabled: True 启用查表，False 关闭（恢复精确计算）。

    Returns:
        设置后的启用状态。
    """
    global _table_lookup_enabled
    was_enabled = _table_lookup_enabled
    _table_lookup_enabled = enabled
    if enabled and not was_enabled:
        logger.warning(PRECISION_WARNING)
    if not enabled and was_enabled:
        logger.info("费用查表加速已关闭，恢复精确计算")
    return _table_lookup_enabled


# ─────────────────────────────────────────────
# FeeTable - 预计算费用查找表
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class FeeTable:
    """预计算费用查找表（不可变）。

    在 [min_amount, max_amount] 上建立线性等距网格，并强制并入佣金分段函数拐点，
    对每个网格点用精确计算（FeeCalculator）求出佣金/印花税/过户费，查询时线性插值。

    Attributes:
        min_amount       : 查表金额下限（元）。
        max_amount       : 查表金额上限（元）。
        bins             : 基础网格档位数（含断点后实际档位可能更多）。
        grid             : 网格点（线性坐标，含断点），单调递增。
        buy_commission   : 买入佣金表（含最低佣金非线性段）。
        sell_commission  : 卖出佣金表。
        stamp_tax        : 卖出印花税表（买入恒为 0，不设表）。
        transfer_fee     : 过户费表（买卖相同）。
    """

    min_amount: float
    max_amount: float
    bins: int
    grid: np.ndarray
    buy_commission: np.ndarray
    sell_commission: np.ndarray
    stamp_tax: np.ndarray
    transfer_fee: np.ndarray

    # ── 内部查找 ──

    def _lookup_scalar(self, table: np.ndarray, amount: float) -> float:
        """单点查表（线性网格线性插值）。

        Args:
            table: 目标费用表数组。
            amount: 成交金额。

        Returns:
            插值后的费用值；越界金额取端点值（clamp）。
        """
        clipped = float(np.clip(amount, self.min_amount, self.max_amount))
        return float(np.interp(clipped, self.grid, table))

    def _lookup_vectorized(self, table: np.ndarray, amounts: np.ndarray) -> np.ndarray:
        """批量查表（线性网格线性插值）。

        Args:
            table: 目标费用表数组。
            amounts: 成交金额数组。

        Returns:
            插值后的费用数组；越界金额取端点值（clamp）。
        """
        clipped = np.clip(amounts, self.min_amount, self.max_amount)
        return np.interp(clipped, self.grid, table)

    # ── 标量查表 ──

    def lookup_commission_breakdown(
        self, side: Union[OrderSide, str], notional: float
    ) -> Tuple[float, float, float]:
        """查表计算单笔成交的佣金/印花税/过户费明细。

        Args:
            side: 订单方向（BUY/SELL 或其别名）。
            notional: 成交金额（非负）。

        Returns:
            (commission, stamp_tax, transfer_fee) 三元组。

        Raises:
            ValueError: 方向或金额非法时抛出。
        """
        direction = normalize_side(side)
        amount = _validate_notional(notional)
        if direction == OrderSide.BUY:
            return (
                self._lookup_scalar(self.buy_commission, amount),
                0.0,
                self._lookup_scalar(self.transfer_fee, amount),
            )
        return (
            self._lookup_scalar(self.sell_commission, amount),
            self._lookup_scalar(self.stamp_tax, amount),
            self._lookup_scalar(self.transfer_fee, amount),
        )

    # ── 向量化查表 ──

    def lookup_commission_breakdown_vectorized(
        self,
        side: Union[OrderSide, str],
        notional: Union[np.ndarray, list],
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """批量查表计算佣金/印花税/过户费明细。

        Args:
            side: 订单方向（BUY/SELL 或其别名，整批同向）。
            notional: 成交金额数组（非负）。

        Returns:
            (commission_arr, stamp_tax_arr, transfer_fee_arr) 三元组数组。

        Raises:
            ValueError: 方向非法或金额含负值时抛出。
        """
        direction = normalize_side(side)
        amounts = np.asarray(notional, dtype=np.float64)
        if np.any(amounts < 0):
            raise ValueError(f"成交金额数组不能包含负值: {amounts!r}")
        if direction == OrderSide.BUY:
            return (
                self._lookup_vectorized(self.buy_commission, amounts),
                np.zeros_like(amounts),
                self._lookup_vectorized(self.transfer_fee, amounts),
            )
        return (
            self._lookup_vectorized(self.sell_commission, amounts),
            self._lookup_vectorized(self.stamp_tax, amounts),
            self._lookup_vectorized(self.transfer_fee, amounts),
        )

    # ── 精度评估 ──

    def estimate_max_relative_error(
        self,
        commission_model: CommissionModel,
        n_samples: int = 10000,
        seed: int = 42,
    ) -> Tuple[float, float]:
        """评估查表 vs 精确计算的最大相对误差。

        在 [min_amount, max_amount] 内的对数均匀随机采样点上，
        比较查表佣金与 FeeCalculator 精确佣金的相对误差。
        越界 clamp 属于已知近似行为，不计入插值误差。

        Args:
            commission_model: 用于精确计算的佣金模型（须与建表时一致）。
            n_samples: 采样点数。
            seed: 随机种子（结果可复现）。

        Returns:
            (max_relative_error, max_absolute_error) 元组。
        """
        if n_samples <= 0:
            raise ValueError(f"n_samples 必须 > 0: {n_samples!r}")
        rng = np.random.default_rng(seed)
        log_min = float(np.log(self.min_amount))
        log_max = float(np.log(self.max_amount))
        # 对数均匀采样，覆盖低端断点附近的密集区间（本行仅依赖金额范围参数）
        samples = np.exp(rng.uniform(log_min, log_max, size=n_samples))

        calc = FeeCalculator(commission_model=commission_model)
        precise = calc.calc_commission_breakdown_vectorized(
            side="SELL", notional=samples
        )[0]
        looked = self.lookup_commission_breakdown_vectorized(
            side="SELL", notional=samples
        )[0]
        rel_err = np.abs(looked - precise) / np.maximum(np.abs(precise), 1e-9)
        abs_err = np.abs(looked - precise)
        return float(np.max(rel_err)), float(np.max(abs_err))

    def __repr__(self) -> str:
        return (
            f"FeeTable(min={self.min_amount:,.0f}, max={self.max_amount:,.0f}, "
            f"bins={self.bins}, 实际网格点={len(self.grid)})"
        )


# ─────────────────────────────────────────────
# 构建函数
# ─────────────────────────────────────────────

def _commission_break_points(model: CommissionModel) -> List[float]:
    """计算佣金分段线性函数的全部拐点金额。

    佣金 = max(amount * rate, min_commission)，拐点 = min_commission / rate
    （amount < 拐点为常数段，amount >= 拐点为比例段）。
    RATIO 模式存在拐点；FIXED 为常数无拐点。

    Args:
        model: 佣金模型。

    Returns:
        拐点金额列表（已去重，可能为空）。
    """
    break_points: List[float] = []
    if model.type == CommissionType.RATIO:
        if model.buy_rate > 0:
            break_points.append(model.min_commission / model.buy_rate)
        if model.sell_rate > 0:
            break_points.append(model.min_commission / model.sell_rate)
    return break_points


def build_fee_table(
    commission_model: Optional[CommissionModel] = None,
    min_amount: float = DEFAULT_MIN_AMOUNT,
    max_amount: float = DEFAULT_MAX_AMOUNT,
    bins: int = DEFAULT_BINS,
) -> FeeTable:
    """从佣金模型构建费用查找表。

    网格 = 线性等距网格 ∪ 佣金拐点（min_commission/rate），
    使 RATIO 分段线性模型查表插值精确。

    Args:
        commission_model: 佣金模型（None 使用 A股写实默认）。
        min_amount: 查表金额下限（元，必须 > 0）。
        max_amount: 查表金额上限（元，必须 > min_amount）。
        bins: 基础线性网格档位数（必须 >= 2）。

    Returns:
        构建完成的 FeeTable。

    Raises:
        ValueError: 金额范围或档位数非法时抛出。
        TypeError: 佣金模型类型非法时抛出。
    """
    if commission_model is not None and not isinstance(commission_model, CommissionModel):
        raise TypeError(
            f"commission_model 必须为 CommissionModel 实例，收到: {type(commission_model).__name__}"
        )
    if min_amount <= 0:
        raise ValueError(f"min_amount 必须 > 0: {min_amount!r}")
    if max_amount <= min_amount:
        raise ValueError(f"max_amount 必须 > min_amount: max={max_amount!r}, min={min_amount!r}")
    if bins < 2:
        raise ValueError(f"bins 必须 >= 2: {bins!r}")

    model = commission_model or CommissionModel()
    # 线性等距基础网格 + 佣金拐点（本行仅依赖金额范围与模型参数，无行情数据）
    base_grid = np.linspace(min_amount, max_amount, num=bins)
    break_points = _commission_break_points(model)
    extra = np.asarray(break_points, dtype=np.float64)
    extra = extra[(extra >= min_amount) & (extra <= max_amount)]
    grid = np.unique(np.concatenate([base_grid, extra]))

    # 对每个网格点用精确计算填充（本行使用的数据截至 t 时刻：仅成交金额与模型参数）
    calc = FeeCalculator(commission_model=model)
    n = grid.size
    buy_commission = np.empty(n, dtype=np.float64)
    sell_commission = np.empty(n, dtype=np.float64)
    stamp_tax = np.empty(n, dtype=np.float64)
    transfer_fee = np.empty(n, dtype=np.float64)
    for i, amount in enumerate(grid):
        buy_comm, _, buy_transfer = calc.calc_commission_breakdown(
            side="BUY", notional=float(amount)
        )
        sell_comm, sell_stamp, _ = calc.calc_commission_breakdown(
            side="SELL", notional=float(amount)
        )
        buy_commission[i] = buy_comm
        sell_commission[i] = sell_comm
        stamp_tax[i] = sell_stamp
        transfer_fee[i] = buy_transfer

    logger.info(
        "费用查找表构建完成: 金额范围 [%.0f, %.0f], 基础档位 %d, 实际网格点 %d",
        min_amount,
        max_amount,
        bins,
        n,
    )
    return FeeTable(
        min_amount=min_amount,
        max_amount=max_amount,
        bins=bins,
        grid=grid,
        buy_commission=buy_commission,
        sell_commission=sell_commission,
        stamp_tax=stamp_tax,
        transfer_fee=transfer_fee,
    )


# ─────────────────────────────────────────────
# TableFeeCalculator - 查表费用计算器
# ─────────────────────────────────────────────

class TableFeeCalculator(FeeCalculator):
    """使用查表路径的费用计算器（FeeCalculator 子类）。

    与 FeeCalculator 接口完全一致，仅佣金/印花税/过户费改为查表插值；
    滑点仍走精确 SlippageModel 计算。默认关闭——只有显式构造本类才启用查表，
    构造时立即输出精度降级警告。

    用法:
        calc = TableFeeCalculator()                      # 默认构建 4096 档表
        fee = calc.calc_fee(side="BUY", notional=50000.0, price=10.0, quantity=5000)
    """

    def __init__(
        self,
        commission_model: Optional[CommissionModel] = None,
        slippage_model: Optional[SlippageModel] = None,
        table: Optional[FeeTable] = None,
        min_amount: float = DEFAULT_MIN_AMOUNT,
        max_amount: float = DEFAULT_MAX_AMOUNT,
        bins: int = DEFAULT_BINS,
    ):
        """初始化查表费用计算器。

        Args:
            commission_model: 佣金模型（None 使用默认；与建表模型一致时精度最佳）。
            slippage_model: 滑点模型（None 使用默认，滑点始终精确计算）。
            table: 现成 FeeTable（None 时按范围参数自动构建）。
            min_amount: 自动建表时的金额下限。
            max_amount: 自动建表时的金额上限。
            bins: 自动建表时的基础网格档位数。
        """
        super().__init__(
            commission_model=commission_model, slippage_model=slippage_model
        )
        self.table: FeeTable = table if table is not None else build_fee_table(
            commission_model=self.commission_model,
            min_amount=min_amount,
            max_amount=max_amount,
            bins=bins,
        )
        # 启用即输出精度警告（状态跃迁时，仅一次）
        enable_table_lookup(True)
        logger.debug("TableFeeCalculator 就绪: %s", self.table)

    # ── 覆盖佣金计算为查表 ──

    def calc_commission_breakdown(
        self, side: Union[OrderSide, str], notional: float
    ) -> Tuple[float, float, float]:
        """查表计算单笔成交的佣金/印花税/过户费明细（标量）。

        Args:
            side: 订单方向。
            notional: 成交金额。

        Returns:
            (commission, stamp_tax, transfer_fee) 三元组。
        """
        return self.table.lookup_commission_breakdown(side=side, notional=notional)

    def calc_commission_breakdown_vectorized(
        self,
        side: Union[OrderSide, str],
        notional: Union[np.ndarray, list],
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """批量查表计算佣金/印花税/过户费明细（向量化）。

        Args:
            side: 订单方向。
            notional: 成交金额数组。

        Returns:
            (commission_arr, stamp_tax_arr, transfer_fee_arr) 三元组数组。
        """
        return self.table.lookup_commission_breakdown_vectorized(
            side=side, notional=notional
        )

    def __repr__(self) -> str:
        return (
            f"TableFeeCalculator(table={self.table!r}, "
            f"滑点={self.slippage_model.type})"
        )


# ─────────────────────────────────────────────
# 精度评估便捷函数
# ─────────────────────────────────────────────

def estimate_max_error(
    table: FeeTable,
    commission_model: Optional[CommissionModel] = None,
    n_samples: int = 10000,
) -> float:
    """评估给定查找表的最大相对误差（便捷函数）。

    Args:
        table: 待评估的 FeeTable。
        commission_model: 精确计算用的佣金模型（None 使用默认）。
        n_samples: 采样点数。

    Returns:
        最大相对误差（0~1，如 0.0001 表示 0.01%）。
    """
    if not isinstance(table, FeeTable):
        raise TypeError(f"table 必须为 FeeTable 实例，收到: {type(table).__name__}")
    model = commission_model or CommissionModel()
    rel, _ = table.estimate_max_relative_error(
        commission_model=model, n_samples=n_samples
    )
    return rel


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

def _run_self_test() -> int:  # pragma: no cover — 仅 python -m 自测路径，不计入门禁覆盖
    """运行 fee_table 模块自测。

    Returns:
        通过的测试项数量。
    """
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("fee_table 自测开始")
    print("=" * 60)

    model = CommissionModel()
    calc = FeeCalculator(commission_model=model)
    table = build_fee_table(commission_model=model)

    # 1. 表结构正确：单调递增、断点已并入网格
    check(
        "表网格单调递增",
        bool(np.all(np.diff(table.grid) > 0)),
        "网格非单调递增",
    )
    expected_break = model.min_commission / model.buy_rate  # 25000
    break_in_grid = any(abs(g - expected_break) < 1e-6 for g in table.grid)
    check(
        "佣金拐点已纳入网格",
        break_in_grid,
        f"拐点={expected_break} 不在网格中",
    )

    # 2. 标量查表 vs 精确计算（多个采样点，含拐点两侧）
    samples = np.array(
        [1000.0, 5000.0, 10000.0, 20000.0, 25000.0, 30000.0, 100000.0, 1e6, 1e7, 1e8]
    )
    max_rel = 0.0
    for amt in samples:
        t_comm, t_stamp, t_transfer = table.lookup_commission_breakdown(
            side="SELL", notional=amt
        )
        p_comm, p_stamp, p_transfer = calc.calc_commission_breakdown(
            side="SELL", notional=amt
        )
        rel = abs(t_comm - p_comm) / max(p_comm, 1e-9)
        max_rel = max(max_rel, rel)
        if abs(t_stamp - p_stamp) > 1e-6 or abs(t_transfer - p_transfer) > 1e-6:
            failures.append(f"印花税/过户费插值偏差: {amt}")
    check(
        "标量查表 vs 精确计算 相对误差 < 0.1%",
        max_rel < _ERROR_TOLERANCE,
        f"最大相对误差={max_rel:.6%}",
    )
    print(f"    （标量查表最大相对误差: {max_rel:.6%}）")

    # 3. 向量化查表 vs 标量查表一致
    vec_comm, vec_stamp, vec_transfer = table.lookup_commission_breakdown_vectorized(
        side="SELL", notional=samples
    )
    consistent = True
    for i, amt in enumerate(samples):
        s_comm, s_stamp, s_transfer = table.lookup_commission_breakdown(
            side="SELL", notional=amt
        )
        if (
            abs(vec_comm[i] - s_comm) > 1e-9
            or abs(vec_stamp[i] - s_stamp) > 1e-9
            or abs(vec_transfer[i] - s_transfer) > 1e-9
        ):
            consistent = False
            break
    check(
        "向量化查表 == 标量查表",
        consistent,
        "向量化与标量查表结果不一致",
    )

    # 4. 随机采样插值误差评估（范围内，不含 clamp）
    rel_err, abs_err = table.estimate_max_relative_error(
        commission_model=model, n_samples=20000
    )
    check(
        "随机采样最大相对误差 < 0.1%",
        rel_err < _ERROR_TOLERANCE,
        f"最大相对误差={rel_err:.6%}, 最大绝对误差={abs_err:.6f}",
    )
    print(f"    （随机采样最大相对误差: {rel_err:.6%}，最大绝对误差: {abs_err:.6f} 元）")

    # 5. 越界 clamp（已知近似行为）
    low = table.lookup_commission_breakdown(side="BUY", notional=1.0)   # 低于下限
    high = table.lookup_commission_breakdown(side="BUY", notional=1e12)  # 高于上限
    check(
        "低于下限金额 clamp 到端点",
        0.0 <= low[0] <= 6.0,
        f"低端佣金={low[0]}",
    )
    check(
        "高于上限金额 clamp 到端点（佣金非负且等于上端点值）",
        abs(high[0] - table.buy_commission[-1]) < 1e-9,
        f"高端佣金={high[0]}",
    )

    # 6. TableFeeCalculator 接口一致性（与精确计算误差 < 0.1%）
    tcalc = TableFeeCalculator(commission_model=model)
    tcalc_ok = True
    for amt in samples:
        t_fee = tcalc.calc_commission_breakdown(side="BUY", notional=amt)
        p_fee = calc.calc_commission_breakdown(side="BUY", notional=amt)
        if abs(t_fee[0] - p_fee[0]) / max(p_fee[0], 1e-9) >= _ERROR_TOLERANCE:
            tcalc_ok = False
            failures.append(f"TableFeeCalculator 佣金偏差: {amt}")
    check("TableFeeCalculator 与精确计算误差 < 0.1%", tcalc_ok, "存在超阈值偏差")

    # 7. 精度不随 bins 恶化（断点入网后 bins=128 也应精确）
    small_table = build_fee_table(commission_model=model, bins=128)
    rel_small, _ = small_table.estimate_max_relative_error(
        commission_model=model, n_samples=5000
    )
    check(
        "bins=128 断点入网后仍精确",
        rel_small < _ERROR_TOLERANCE,
        f"小表误差={rel_small:.6%}",
    )
    print(f"    （bins=128 断点入网后误差: {rel_small:.6%}）")

    # 8. 非法参数防御
    try:
        build_fee_table(min_amount=0, max_amount=1e6)
        check("min_amount<=0 抛 ValueError", False, "未抛出")
    except ValueError:
        check("min_amount<=0 抛 ValueError", True)
    try:
        build_fee_table(min_amount=1e6, max_amount=1e5)
        check("max<=min 抛 ValueError", False, "未抛出")
    except ValueError:
        check("max<=min 抛 ValueError", True)
    try:
        build_fee_table(bins=1)
        check("bins<2 抛 ValueError", False, "未抛出")
    except ValueError:
        check("bins<2 抛 ValueError", True)
    try:
        build_fee_table(commission_model="bad")  # type: ignore[arg-type]
        check("非法模型类型抛 TypeError", False, "未抛出")
    except TypeError:
        check("非法模型类型抛 TypeError", True)

    # 9. 全局开关状态跃迁（先重置为关闭）
    enable_table_lookup(False)
    before = is_table_lookup_enabled()
    enable_table_lookup(True)
    after = is_table_lookup_enabled()
    check(
        "enable_table_lookup 关闭→启用成功",
        before is False and after is True,
        f"before={before}, after={after}",
    )

    # 10. 精度警告输出（状态跃迁启用时触发）
    import io
    import logging as _logging

    log_capture = io.StringIO()
    handler = _logging.StreamHandler(log_capture)
    # 直接引用模块级 logger 对象（python -m 运行时 __name__=="__main__"，
    # 按名字 getLogger 会捕获到错误的 logger）
    _fee_logger = logger
    old_level = _fee_logger.level
    _fee_logger.setLevel(_logging.WARNING)
    _fee_logger.addHandler(handler)
    try:
        enable_table_lookup(False)
        enable_table_lookup(True)
    finally:
        _fee_logger.removeHandler(handler)
        _fee_logger.setLevel(old_level)
    check(
        "启用查表输出精度警告",
        "费用查表加速已启用" in log_capture.getvalue(),
        f"警告内容: {log_capture.getvalue()[:80]!r}",
    )

    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return len(failures)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":  # pragma: no cover — 仅 python -m 自测路径
    # 自测 Demo：运行全部校验项
    raise SystemExit(_run_self_test())
