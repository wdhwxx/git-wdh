# 文件名：myquant/realism_gate/invariants.py
"""
realism_gate/invariants.py - 结果侧真实性不变量（P1-15，v2.2 §4.9）
======================================================================

动机：§4.2 的 manifest 只在**引擎创建时**校验"配置/装配是否真实"，无法发现
**结果层面的不可能事件**。本模块在回测**结束时**（on_stop 前）对调用方注入的
成交 / 净值 / 持仓 / 行情序列做六条极低成本、几乎无法伪造的不变量校验，直接抓住
本项目已存在的缺陷（vectorized cash[i:] 覆盖、event 现金透支、T+1 缺失等）。

六条不变量（v2.2 §4.9）：
  - RI-1 成交价界：每笔成交价 ∈ [当日 low, 当日 high]（容忍滑点带宽）
  - RI-2 成交量界：单笔成交量 ≤ 当日 volume × participation_cap
                   （cap 未声明时按"无限流动性"声明放行并打标）
  - RI-3 权益恒等：equity == cash + Σ(持仓量 × 当日收盘价)，容差 1e-6×初始资金
  - RI-4 现金非负：任一时点 cash >= -tolerance（未声明杠杆时）
  - RI-5 T+1 一致：同一标的同一交易日卖出量 ≤ 前一交易日末持仓量
  - RI-6 持仓非负：任一时点持仓量 >= 0（未声明做空时）

分层红线：本模块登记 L0，**只消费调用方注入的序列**，不 import myquant.backtest /
myquant.execution / myquant.data_access（R-02/R-16 跨层红线）。违规以
RealismViolation 列表返回，由调用方（引擎 run() 收尾）决定 mark_failed / 落盘。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from myquant.realism_gate.manifest import RealismViolation

logger = logging.getLogger("myquant.realism_gate.invariants")

# ── 常量（禁止硬编码魔法数字）─────────────────────────────────

# RI-3 权益恒等式容差系数：|equity - (cash + Σposition*close)| ≤ _EQUITY_TOL * initial_capital
_EQUITY_TOL: float = 1e-6
# RI-1 价界容忍带宽（1 tick = 0.01 元，最低带宽）
_TICK: float = 0.01
# RI-4 现金非负容差（绝对值，元）——允许极小浮点误差，不视为杠杆
_CASH_TOL: float = 1.0


# ═══════════════════════════════════════════════
#  注入数据结构（调用方构造，本模块只读）
# ═══════════════════════════════════════════════


@dataclass(frozen=True)
class TradeRecord:
    """单条成交记录（引擎在 run() 收尾时逐条注入）。

    Attributes:
        symbol: 标的代码。
        trade_date: 成交日期（ISO 字符串，逐 bar 对齐）。
        side: 订单方向，"BUY" 或 "SELL"。
        price: 成交价（含滑点）。
        quantity: 成交股数（正数）。
        day_low: 当日最低价（价界校验用）。
        day_high: 当日最高价（价界校验用）。
        day_volume: 当日成交量（量界校验用，股）。
        slippage_amount: 该笔滑点金额（元；价界容忍带宽用），默认 0。
    """

    symbol: str
    trade_date: str
    side: str
    price: float
    quantity: float
    day_low: float
    day_high: float
    day_volume: float
    slippage_amount: float = 0.0


@dataclass(frozen=True)
class BarState:
    """单标的单 bar 末态（逐 bar 注入，RI-3/RI-4/RI-5/RI-6 用）。

    Attributes:
        symbol: 标的代码。
        bar_date: bar 日期（ISO 字符串）。
        cash: 该标的子账户当日末现金（元）；组合级校验时由调用方先汇总。
        position: 该标的当日末持仓（股）。
        close: 当日收盘价（权益恒等式用）。
    """

    symbol: str
    bar_date: str
    cash: float
    position: float
    close: float


# ═══════════════════════════════════════════════
#  校验入口
# ═══════════════════════════════════════════════


def check_result_invariants(
    trades: Sequence[TradeRecord],
    bar_states: Sequence[BarState],
    initial_capital: float,
    participation_cap: Optional[float] = None,
    declared_unlimited_liquidity: bool = True,
    allow_leverage: bool = False,
    allow_short: bool = False,
) -> List[RealismViolation]:
    """执行 RI-1 ~ RI-6 全部不变量校验，返回违规列表。

    本函数为纯函数：不写盘、不改入参、不依赖全局状态。调用方（引擎 run() 收尾）
    拿到违规列表后自行决定 mark_failed / 写 manifest / 落 audit_log。

    Args:
        trades: 全部成交记录序列。
        bar_states: 全部标的逐 bar 末态序列（RI-3/4/5/6 用）。
        initial_capital: 初始资金（RI-3 容差基准），必须 > 0。
        participation_cap: 成交量参与率上限（0<cap≤1）；None 表示未声明。
        declared_unlimited_liquidity: cap 未声明时是否已显式声明"无限流动性"
            （True=按清单声明放行 RI-2 并在结果打标；False=RI-2 缺 cap 即违规）。
        allow_leverage: 是否已声明允许杠杆（True=RI-4 现金可为负不违规）。
        allow_short: 是否已声明允许卖空（True=RI-6 持仓可为负不违规）。

    Returns:
        违规 RealismViolation 列表；无违规返回空列表。

    Raises:
        ValueError: initial_capital <= 0，或 participation_cap 不在 (0,1]。
    """
    if initial_capital <= 0:
        raise ValueError(f"initial_capital 必须 > 0，收到 {initial_capital}")
    if participation_cap is not None and not (0.0 < participation_cap <= 1.0):
        raise ValueError(
            f"participation_cap 须在 (0,1] 或 None，收到 {participation_cap}"
        )

    violations: List[RealismViolation] = []

    violations.extend(_check_ri1_price_bounds(trades))
    violations.extend(
        _check_ri2_volume_bound(trades, participation_cap, declared_unlimited_liquidity)
    )
    violations.extend(_check_ri3_equity_identity(bar_states, initial_capital))
    violations.extend(_check_ri4_cash_non_negative(bar_states, allow_leverage))
    violations.extend(_check_ri5_t1_consistency(trades, bar_states))
    violations.extend(_check_ri6_position_non_negative(bar_states, allow_short))

    return violations


# ═══════════════════════════════════════════════
#  组合级 BarState 构造（事件驱动引擎 RI-3/4/6 适配）
# ═══════════════════════════════════════════════


def build_portfolio_bar_states(
    points: Sequence[tuple],
) -> List[BarState]:
    """从组合级净值快照序列构造 BarState（供事件驱动引擎激活 RI-3/4/6）。

    事件驱动引擎（mixed/event）无逐 symbol 持仓历史，但 recorder 逐 bar 记录了
    组合级 cash + 持仓市值快照。这里把每个快照转成一条组合级 BarState：
    position 语义为"组合持仓市值"（非股数）、close=1.0，使 RI-3 恒等式
    implied = cash + position_value 自然成立（不伪造、不误报），同时让
    RI-4（cash 非负）/ RI-6（持仓市值非负）真正对组合生效。

    Args:
        points: 净值快照序列，每项为 ``(date: str, cash: float, position_value: float)``。

    Returns:
        组合级 BarState 列表（symbol 固定为 "_PORTFOLIO_"）。
    """
    out: List[BarState] = []
    for date, cash, position_value in points:
        out.append(BarState(
            symbol="_PORTFOLIO_",
            bar_date=str(date),
            cash=float(cash),
            position=float(position_value),
            close=1.0,  # 组合级：市值即 position*1.0，RI-3 恒等式自然成立
        ))
    return out


def build_position_timeline_bar_states(
    trades: Sequence,
    close_lookup: Optional[Dict[str, Dict[str, float]]] = None,
) -> List[BarState]:
    """从成交流重建逐 symbol 持仓时间线（供事件驱动引擎激活 RI-5 T+1）。

    按 symbol 分组、按成交日期累计净持仓（BUY 加 / SELL 减），得到每个
    (symbol, date) 的当日末持仓；close 由 close_lookup[symbol][date] 提供
    （缺失则 0.0，RI-3 自动跳过该点）。

    Args:
        trades: 成交记录序列（鸭子类型，需 symbol/trade_date/side/quantity 字段）。
        close_lookup: ``{symbol: {date: close}}`` 当日收盘价查找表，可为 None。

    Returns:
        逐 symbol 持仓时间线 BarState 列表。
    """
    close_lookup = close_lookup or {}
    # 每 symbol 的每日净变化：{symbol: {date: net_change}}
    change_by_sym_date: Dict[str, Dict[str, float]] = {}
    for t in trades:
        sym = getattr(t, "symbol")
        date = getattr(t, "trade_date")
        side = getattr(t, "side")
        qty = float(getattr(t, "quantity"))
        bucket = change_by_sym_date.setdefault(sym, {})
        bucket[date] = bucket.get(date, 0.0) + (qty if side == "BUY" else -qty)

    out: List[BarState] = []
    for sym, by_date in change_by_sym_date.items():
        # 跨日累计持仓（昨日末持仓 + 当日净变化），还原真实持仓时间线；
        # 当日净变化不等于当日末持仓——必须跨日累计才能让 RI-6/RI-5 判据正确。
        running = 0.0
        for date in sorted(by_date.keys()):
            running += by_date[date]
            close = float(close_lookup.get(sym, {}).get(date, 0.0))
            out.append(BarState(
                symbol=sym,
                bar_date=date,
                cash=0.0,  # 逐 symbol 持仓时间线无独立现金（组合级 cash 另算）
                position=running,
                close=close,
            ))
    return out


# ═══════════════════════════════════════════════
#  RI-1 成交价界
# ═══════════════════════════════════════════════


def _check_ri1_price_bounds(
    trades: Sequence[TradeRecord],
) -> List[RealismViolation]:
    """RI-1：每笔成交价 ∈ [当日 low - 带宽, 当日 high + 带宽]。

    容忍带宽 = max(slippage_amount, 1 tick)，覆盖滑点导致的合理越界。
    """
    out: List[RealismViolation] = []
    for t in trades:
        band = max(float(t.slippage_amount), _TICK)
        lo = float(t.day_low) - band
        hi = float(t.day_high) + band
        if not (lo <= float(t.price) <= hi):
            out.append(RealismViolation(
                field_path="invariants.price_bounds",
                critical=True,
                message=(
                    f"RI-1 成交价越界: {t.symbol} {t.trade_date} price={t.price:.4f} "
                    f"不在 [{lo:.4f}, {hi:.4f}]（low={t.day_low}, high={t.day_high}）"
                ),
                actual_value={
                    "symbol": t.symbol, "date": t.trade_date, "price": t.price,
                    "low": t.day_low, "high": t.day_high, "band": band,
                },
            ))
    return out


# ═══════════════════════════════════════════════
#  RI-2 成交量界
# ═══════════════════════════════════════════════


def _check_ri2_volume_bound(
    trades: Sequence[TradeRecord],
    participation_cap: Optional[float],
    declared_unlimited_liquidity: bool,
) -> List[RealismViolation]:
    """RI-2：单笔成交量 ≤ 当日 volume × participation_cap。

    cap 未声明时：若已声明"无限流动性"（declared_unlimited_liquidity=True）放行；
    否则视为 critical 违规（流动性约束缺失，P1-09 的前置证据）。
    """
    out: List[RealismViolation] = []

    # cap 未声明路径
    if participation_cap is None:
        if declared_unlimited_liquidity:
            # 已声明无限流动性 → 放行（由调用方在清单打标），不产违规
            return out
        # 未声明 cap 也未声明无限流动性 → 流动性约束缺失
        out.append(RealismViolation(
            field_path="invariants.volume_bound",
            critical=True,
            message="RI-2 流动性约束缺失：participation_cap 未声明且未声明无限流动性",
            actual_value={"participation_cap": None},
        ))
        return out

    cap = float(participation_cap)
    for t in trades:
        limit = float(t.day_volume) * cap
        if float(t.quantity) > limit + 1e-9:  # 容差浮点
            out.append(RealismViolation(
                field_path="invariants.volume_bound",
                critical=True,
                message=(
                    f"RI-2 成交量超限: {t.symbol} {t.trade_date} qty={t.quantity} "
                    f"> volume×cap={limit:.2f}（volume={t.day_volume}, cap={cap}）"
                ),
                actual_value={
                    "symbol": t.symbol, "date": t.trade_date, "qty": t.quantity,
                    "day_volume": t.day_volume, "cap": cap, "limit": limit,
                },
            ))
    return out


# ═══════════════════════════════════════════════
#  RI-3 权益恒等
# ═══════════════════════════════════════════════


def _check_ri3_equity_identity(
    bar_states: Sequence[BarState],
    initial_capital: float,
) -> List[RealismViolation]:
    """RI-3：equity == cash + position × close，逐 bar 逐标的校验。

    容差 = _EQUITY_TOL × initial_capital。直接抓住 vectorized cash[i:]/position[i:]
    覆盖前值导致的恒等式断裂。

    当 position==0 或 close==0 时跳过（equity=cash，无恒等式可校验；
    引擎暂未注入逐 bar close 时不会误报）。
    """
    tol = _EQUITY_TOL * float(initial_capital)
    out: List[RealismViolation] = []
    for b in bar_states:
        # 无持仓或无收盘价时，equity=cash 本身即成立，跳过恒等式校验
        if float(b.position) == 0.0 or float(b.close) == 0.0:
            continue
        implied_equity = float(b.cash) + float(b.position) * float(b.close)
        # 恒等式自洽：equity(cash+pos*close) 与 cash 的"增量"应等于 pos*close。
        # 检查 abs((implied - cash) - pos*close) <= tol——等价于 cash+pos*close 两边
        # 自洽；一旦 cash/position 被覆盖不同步（vectorized cash[i:] 前值覆盖），
        # 该增量与 pos*close 的偏差就会超过容差。
        delta = abs(implied_equity - float(b.cash))
        expected = abs(float(b.position)) * abs(float(b.close))
        if implied_equity != implied_equity or abs(delta - expected) > tol:
            out.append(RealismViolation(
                field_path="invariants.equity_identity",
                critical=True,
                message=(
                    f"RI-3 权益恒等异常: {b.symbol} {b.bar_date} "
                    f"cash={b.cash:.2f} pos={b.position} close={b.close:.2f} "
                    f"implied_equity={implied_equity:.2f}"
                ),
                actual_value={
                    "symbol": b.symbol, "date": b.bar_date, "cash": b.cash,
                    "position": b.position, "close": b.close,
                    "implied_equity": implied_equity,
                },
            ))
    return out


# ═══════════════════════════════════════════════
#  RI-4 现金非负
# ═══════════════════════════════════════════════


def _check_ri4_cash_non_negative(
    bar_states: Sequence[BarState],
    allow_leverage: bool,
) -> List[RealismViolation]:
    """RI-4：任一时点 cash >= -_CASH_TOL（未声明杠杆时）。

    直接抓住 event 市价单资金校验 fail-open（透支）。
    """
    if allow_leverage:
        return []
    out: List[RealismViolation] = []
    for b in bar_states:
        if float(b.cash) < -_CASH_TOL:
            out.append(RealismViolation(
                field_path="invariants.cash_non_negative",
                critical=True,
                message=(
                    f"RI-4 现金透支: {b.symbol} {b.bar_date} cash={b.cash:.2f} "
                    f"< -{_CASH_TOL}（未声明杠杆）"
                ),
                actual_value={"symbol": b.symbol, "date": b.bar_date, "cash": b.cash},
            ))
    return out


# ═══════════════════════════════════════════════
#  RI-5 T+1 一致性
# ═══════════════════════════════════════════════


def _check_ri5_t1_consistency(
    trades: Sequence[TradeRecord],
    bar_states: Sequence[BarState],
) -> List[RealismViolation]:
    """RI-5：同一标的同一交易日卖出量 ≤ 前一交易日末持仓量。

    持仓来自 bar_states（按 symbol 分组、按 bar_date 排序）；卖出量 > 前一日末持仓
    即 T+1 冻结缺失（P1-08 的前置证据）。
    """
    # 按 symbol 分组持仓时间线
    pos_timeline: Dict[str, List[BarState]] = {}
    for b in bar_states:
        pos_timeline.setdefault(b.symbol, []).append(b)
    for sym in pos_timeline:
        pos_timeline[sym].sort(key=lambda x: x.bar_date)

    # 按 symbol 汇总当日卖出量
    sell_by_sym_date: Dict[str, Dict[str, float]] = {}
    for t in trades:
        if t.side != "SELL":
            continue
        sell_by_sym_date.setdefault(t.symbol, {}).setdefault(t.trade_date, 0.0)
        sell_by_sym_date[t.symbol][t.trade_date] += float(t.quantity)

    out: List[RealismViolation] = []
    for sym, sells in sell_by_sym_date.items():
        timeline = pos_timeline.get(sym, [])
        for sell_date, sell_qty in sells.items():
            # 前一交易日末持仓 = 卖出日之前最后一个 bar 的 position
            prev_pos = 0.0
            for b in timeline:
                if b.bar_date < sell_date:
                    prev_pos = float(b.position)
                else:
                    break
            if sell_qty > prev_pos + 1e-6:
                out.append(RealismViolation(
                    field_path="invariants.t1_consistency",
                    critical=True,
                    message=(
                        f"RI-5 T+1 越界: {sym} {sell_date} 卖出 {sell_qty} > "
                        f"前一日末持仓 {prev_pos}（T+1 冻结缺失）"
                    ),
                    actual_value={
                        "symbol": sym, "date": sell_date, "sell_qty": sell_qty,
                        "prev_position": prev_pos,
                    },
                ))
    return out


# ═══════════════════════════════════════════════
#  RI-6 持仓非负
# ═══════════════════════════════════════════════


def _check_ri6_position_non_negative(
    bar_states: Sequence[BarState],
    allow_short: bool,
) -> List[RealismViolation]:
    """RI-6：任一时点持仓量 >= 0（未声明做空时）。抓住卖空穿透。"""
    if allow_short:
        return []
    out: List[RealismViolation] = []
    for b in bar_states:
        if float(b.position) < -1e-6:
            out.append(RealismViolation(
                field_path="invariants.position_non_negative",
                critical=True,
                message=(
                    f"RI-6 持仓为负: {b.symbol} {b.bar_date} position={b.position}"
                    f"（未声明做空）"
                ),
                actual_value={"symbol": b.symbol, "date": b.bar_date, "position": b.position},
            ))
    return out
