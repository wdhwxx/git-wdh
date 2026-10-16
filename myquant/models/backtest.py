# 文件名：myquant/models/backtest.py
"""
backtest.py - 回测模型
=======================
定义回测配置、结果与绩效指标容器。

类清单:
  - BacktestConfig     : 回测参数配置
  - PerformanceMetrics : 绩效指标容器
  - TradeRecord        : 回测成交记录 (含策略归因)
  - EquityPoint        : 净值曲线数据点
  - BacktestResult     : 回测完整结果
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# ═══════════════════════════════════════════════
#  BacktestConfig - 回测配置
# ═══════════════════════════════════════════════

class CommissionType(Enum):
    """手续费模式"""
    FIXED = "fixed"           # 固定金额/笔
    RATIO = "ratio"           # 比例
    TIERED = "tiered"         # 阶梯


@dataclass
class CommissionModel:
    """
    手续费模型（A股写实标准，交接文档5.5节）

    交易成本标准:
      - 佣金：万2（0.0002），最低5元，买卖双向
      - 印花税：万5（0.0005），仅卖出；2023-08-28 起由千1减半（财政部/税务总局2023年第39号公告）
      - 过户费：万0.1（0.00001），买卖双向
    """
    type: CommissionType = CommissionType.RATIO
    buy_rate: float = 0.0002       # 买入佣金率：万2
    sell_rate: float = 0.0002      # 卖出佣金率：万2
    min_commission: float = 5.0    # 最低佣金：5元
    stamp_tax: float = 0.0005      # 印花税：万5（0.05%，仅卖出；2023-08-28 起由千1减半）
    transfer_fee: float = 0.00001  # 过户费：万0.1（买卖双向）

    def calc_buy(self, amount: float) -> float:
        """买入成本：佣金 + 过户费"""
        if self.type == CommissionType.FIXED:
            commission = max(self.buy_rate, self.min_commission)
        else:
            commission = max(amount * self.buy_rate, self.min_commission)
        transfer = amount * self.transfer_fee
        return commission + transfer

    def calc_sell(self, amount: float) -> float:
        """卖出成本：佣金 + 印花税 + 过户费"""
        if self.type == CommissionType.FIXED:
            commission = max(self.sell_rate, self.min_commission)
        else:
            commission = max(amount * self.sell_rate, self.min_commission)
        tax = amount * self.stamp_tax
        transfer = amount * self.transfer_fee
        return commission + tax + transfer


@dataclass
class SlippageModel:
    """滑点模型"""
    type: str = "fixed_bps"     # "fixed_bps" | "volume_based" | "none"
    fixed_bps: float = 2.0      # 固定滑点 (基点)
    volume_impact: float = 0.1  # 量价冲击系数

    def calc(self, price: float, volume: float = 0, order_size: float = 0) -> float:
        if self.type == "none":
            return 0.0
        elif self.type == "fixed_bps":
            return price * self.fixed_bps / 10000.0
        elif self.type == "volume_based":
            if volume <= 0:
                return price * self.fixed_bps / 10000.0
            impact = self.volume_impact * (order_size / volume)
            return price * impact
        # P0-05：未知 type 显式报错（消灭静默零滑点通道）
        raise ValueError(
            f"SlippageModel.type 未知值: {self.type!r}（合法值: fixed_bps/volume_based/none）"
        )


@dataclass
class BacktestConfig:
    """
    回测配置

    Attributes
    ----------
    strategy_name  : 策略名称
    symbols        : 标的列表
    start_date     : 回测起始日期 'YYYY-MM-DD'
    end_date       : 回测结束日期 'YYYY-MM-DD'
    frequency      : 数据频率
    initial_capital: 初始资金
    commission     : 手续费模型
    slippage       : 滑点模型
    benchmark      : 基准代码
    risk_free_rate : 无风险利率 (年化)
    params         : 策略参数
    extra          : 扩展配置
    """

    strategy_name: str = "default"
    symbols: List[str] = field(default_factory=list)
    start_date: str = ""
    end_date: str = ""
    frequency: str = "1d"
    initial_capital: float = 1_000_000.0
    commission: CommissionModel = field(default_factory=CommissionModel)
    slippage: SlippageModel = field(default_factory=SlippageModel)
    benchmark: str = "000300.SH"
    risk_free_rate: float = 0.02
    params: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    # ── P0-03 约束真可控：提升为显式字段（统一键名，默认 fail-closed）──
    price_limit_pct: float = 0.10       # 涨跌停幅度（主板 10% 默认）
    enforce_limit: bool = False         # 是否强制涨跌停校验（event 路径开启）
    enable_t1: bool = True              # T+1 持仓冻结
    lot_size: int = 100                 # A 股一手股数
    # ── P1-09 流动性约束：参与率进配置 ──
    participation_rate: float = 1.0     # 成交量参与率（1.0=全量成交，0<rate<1 限时）
    # ── P0-01 端到端（#345）：真实性门禁配置透传（enabled/audit_violations）──
    # create_engine 从 config.to_dict()["realism_gate"] 读取（YAML/API/测试均可配）；
    # P1-03 收尾（#354）：默认带**显式段**（enabled=False 审计档）——代码直构
    # 默认即"显式配置"（非缺失）；YAML 缺段时 config_builder 产出空段 → _attach
    # 层判"配置缺失 = 阻断"（方案 §4.3：本门禁要求显式启用，缺失即红）
    realism_gate: Dict[str, Any] = field(
        default_factory=lambda: {"enabled": False, "audit_violations": True}
    )

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.symbols:
            errors.append("symbols 不能为空")
        if not self.start_date:
            errors.append("start_date 不能为空")
        if not self.end_date:
            errors.append("end_date 不能为空")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            errors.append("start_date 必须早于 end_date")
        if self.initial_capital <= 0:
            errors.append("initial_capital 必须 > 0")
        return errors

    @property
    def is_valid(self) -> bool:
        return len(self.validate()) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "symbols": self.symbols,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "frequency": self.frequency,
            "initial_capital": self.initial_capital,
            "benchmark": self.benchmark,
            "risk_free_rate": self.risk_free_rate,
            "params": self.params,
            # P0-03 显式字段透传（统一键名）
            "price_limit_pct": self.price_limit_pct,
            "enforce_limit": self.enforce_limit,
            "enable_t1": self.enable_t1,
            "lot_size": self.lot_size,
            # P1-09 流动性约束透传
            "participation_rate": self.participation_rate,
            # T6-1：佣金/滑点模型透传（此前缺失→门禁恒读默认值万3/万0.2）
            "commission": {
                "buy_rate": getattr(self.commission, "buy_rate", 0.0003),
                "sell_rate": getattr(self.commission, "sell_rate", 0.0003),
                "min_commission": getattr(self.commission, "min_commission", 5.0),
                "stamp_tax": getattr(self.commission, "stamp_tax", 0.001),
                "transfer_fee": getattr(self.commission, "transfer_fee", 0.00002),
            } if not isinstance(self.commission, dict) else self.commission,
            "slippage": {
                "type": getattr(self.slippage, "type", "fixed"),
                "value": getattr(self.slippage, "value", 0.0),
            },
            # P0-01 端到端（#345）：真实性门禁配置透传
            "realism_gate": self.realism_gate,
        }

    def __repr__(self) -> str:
        return (
            f"BacktestConfig(strategy='{self.strategy_name}' "
            f"symbols={self.symbols} "
            f"period={self.start_date}~{self.end_date} "
            f"capital={self.initial_capital:,.0f})"
        )


# ═══════════════════════════════════════════════
#  板块涨跌停映射（P0-03）
# ═══════════════════════════════════════════════

def price_limit_pct_for_symbol(symbol: str) -> float:
    """根据股票代码推断板块涨跌停幅度（P0-03 板块映射表）。

    规则：
    - 主板（60xxxx 沪市 / 00xxxx 深市）：10%
    - 创业板（30xxxx）：20%
    - 科创板（68xxxx）：20%
    - 北交所（8xxxxx / 4xxxxx）：30%
    - 其他/未知：默认 10%（主板标准）

    ST 股 5% 需要股票名称判断，不在本函数范围（调用方自行覆盖）。

    Args:
        symbol: 股票代码（如 "600519.SH"、"300750.SZ"）。

    Returns:
        涨跌停幅度（float，如 0.10 = ±10%）。
    """
    code = symbol.split(".")[0] if "." in symbol else symbol
    if code.startswith(("30", "68")):
        return 0.20   # 创业板 / 科创板
    if code.startswith(("8", "4")):
        return 0.30   # 北交所
    return 0.10       # 主板（60/00）+ 默认


# ═══════════════════════════════════════════════
#  PerformanceMetrics - 绩效指标容器
# ═══════════════════════════════════════════════

@dataclass
class PerformanceMetrics:
    """
    绩效指标容器

    存储回测/实盘计算出的所有绩效指标。
    支持序列化为 dict / 格式化输出。
    """

    # 收益指标
    total_return: float = 0.0
    annualized_return: float = 0.0
    benchmark_return: Optional[float] = None  # KP-P2-04：无基准时 None
    excess_return: Optional[float] = None  # KP-P2-04：无基准时 None

    # 风险指标
    annualized_volatility: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0
    downside_volatility: float = 0.0

    # 风险调整收益
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    information_ratio: float = 0.0

    # 交易统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # 成本
    total_commission: float = 0.0
    total_slippage: float = 0.0
    turnover: float = 0.0

    # 其他
    alpha: Optional[float] = None  # KP-P2-04：无基准时 None
    beta: Optional[float] = None  # KP-P2-04：无基准时 None
    tracking_error: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    skewness: float = 0.0
    kurtosis: float = 0.0

    # 元数据
    trading_days: int = 0
    start_date: str = ""
    end_date: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """导出为字典"""
        return {
            "收益指标": {
                "总收益率": f"{self.total_return:.4%}",
                "年化收益率": f"{self.annualized_return:.4%}",
                "基准收益率": "-" if self.benchmark_return is None else f"{self.benchmark_return:.4%}",
                "超额收益": "-" if self.excess_return is None else f"{self.excess_return:.4%}",
            },
            "风险指标": {
                "年化波动率": f"{self.annualized_volatility:.4%}",
                "最大回撤": f"{self.max_drawdown:.4%}",
                "最大回撤持续天数": self.max_drawdown_duration,
                "下行波动率": f"{self.downside_volatility:.4%}",
            },
            "风险调整": {
                "夏普比率": round(self.sharpe_ratio, 4),
                "索提诺比率": round(self.sortino_ratio, 4),
                "Calmar比率": round(self.calmar_ratio, 4),
                "信息比率": round(self.information_ratio, 4),
            },
            "交易统计": {
                "总交易次数": self.total_trades,
                "盈利次数": self.winning_trades,
                "亏损次数": self.losing_trades,
                "胜率": f"{self.win_rate:.2%}",
                "盈亏比": round(self.profit_loss_ratio, 4),
                "平均盈利": round(self.avg_win, 4),
                "平均亏损": round(self.avg_loss, 4),
                "最大连胜": self.max_consecutive_wins,
                "最大连亏": self.max_consecutive_losses,
            },
            "成本": {
                "总手续费": round(self.total_commission, 2),
                "总滑点": round(self.total_slippage, 2),
                "换手率": f"{self.turnover:.2f}",
            },
            "希腊值": {
                "Alpha": "-" if self.alpha is None else round(self.alpha, 6),
                "Beta": "-" if self.beta is None else round(self.beta, 4),
                "跟踪误差": f"{self.tracking_error:.4%}",
            },
            "分布": {
                "VaR(95%)": f"{self.var_95:.4%}",
                "CVaR(95%)": f"{self.cvar_95:.4%}",
                "偏度": round(self.skewness, 4),
                "峰度": round(self.kurtosis, 4),
            },
            "元数据": {
                "交易日数": self.trading_days,
                "起始日期": self.start_date,
                "结束日期": self.end_date,
            },
        }

    def summary(self) -> str:
        """格式化摘要输出"""
        lines = [
            "╔══════════════════════════════════════════════╗",
            "║           绩 效 报 告                        ║",
            "╠══════════════════════════════════════════════╣",
            f"║ 总收益率:     {self.total_return:>12.4%}              ║",
            f"║ 年化收益率:   {self.annualized_return:>12.4%}              ║",
            f"║ 年化波动率:   {self.annualized_volatility:>12.4%}              ║",
            f"║ 夏普比率:     {self.sharpe_ratio:>12.4f}              ║",
            f"║ 索提诺比率:   {self.sortino_ratio:>12.4f}              ║",
            f"║ Calmar比率:   {self.calmar_ratio:>12.4f}              ║",
            f"║ 最大回撤:     {self.max_drawdown:>12.4%}              ║",
            f"║ 胜率:         {self.win_rate:>12.2%}              ║",
            f"║ 盈亏比:       {self.profit_loss_ratio:>12.4f}              ║",
            f"║ 总交易次数:   {self.total_trades:>12d}              ║",
            f"║ Alpha:        {('-' if self.alpha is None else f'{self.alpha:>12.6f}'):>12}              ║",
            f"║ Beta:         {('-' if self.beta is None else f'{self.beta:>12.4f}'):>12}              ║",
            "╚══════════════════════════════════════════════╝",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"PerformanceMetrics(ret={self.total_return:.2%} "
            f"sharpe={self.sharpe_ratio:.2f} "
            f"maxdd={self.max_drawdown:.2%} "
            f"winrate={self.win_rate:.2%})"
        )


# ═══════════════════════════════════════════════
#  TradeRecord - 回测成交记录
# ═══════════════════════════════════════════════

@dataclass
class TradeRecord:
    """
    回测成交记录 (含策略归因)

    比 models/trade.py 的 Trade 更丰富，
    包含回测特有的归因字段。
    """

    trade_id: str = ""
    symbol: str = ""
    side: str = "BUY"
    quantity: float = 0.0
    price: float = 0.0
    timestamp: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    pnl: float = 0.0              # 本笔盈亏 (平仓时计算)
    pnl_pct: float = 0.0          # 本笔收益率
    holding_days: int = 0         # 持仓天数
    strategy_id: str = ""
    signal_source: str = ""       # 信号来源
    entry_reason: str = ""        # 入场原因
    exit_reason: str = ""         # 出场原因
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.quantity * self.price

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "timestamp": self.timestamp,
            "commission": self.commission,
            "slippage": self.slippage,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "holding_days": self.holding_days,
            "strategy_id": self.strategy_id,
            "signal_source": self.signal_source,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
        }

    def __repr__(self) -> str:
        return (
            f"TradeRecord({self.symbol} {self.side} "
            f"{self.quantity:.0f}@{self.price:.2f} "
            f"pnl={self.pnl:.2f})"
        )


# ═══════════════════════════════════════════════
#  EquityPoint - 净值曲线数据点
# ═══════════════════════════════════════════════

@dataclass
class EquityPoint:
    """净值曲线单个数据点"""
    timestamp: float = 0.0
    date: str = ""
    equity: float = 1.0           # 净值
    cash: float = 0.0             # 现金
    position_value: float = 0.0   # 持仓市值
    drawdown: float = 0.0         # 当前回撤
    benchmark_equity: float = 1.0 # 基准净值
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_value(self) -> float:
        return self.cash + self.position_value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "date": self.date,
            "equity": self.equity,
            "cash": self.cash,
            "position_value": self.position_value,
            "drawdown": self.drawdown,
            "benchmark_equity": self.benchmark_equity,
        }


# ═══════════════════════════════════════════════
#  BacktestResult - 回测完整结果
# ═══════════════════════════════════════════════

@dataclass
class BacktestResult:
    """
    回测完整结果

    包含:
      - 配置信息
      - 绩效指标
      - 净值曲线
      - 成交记录
      - 运行元数据
    """

    # 配置
    config: BacktestConfig = field(default_factory=BacktestConfig)

    # 绩效
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)

    # 净值曲线
    equity_curve: List[EquityPoint] = field(default_factory=list)

    # 成交记录
    trades: List[TradeRecord] = field(default_factory=list)

    # 运行元数据
    run_id: str = field(default_factory=lambda: f"bt_{int(time.time())}")
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    status: str = "pending"  # pending | running | completed | failed
    error_message: str = ""

    # P0-01 端到端（#345）：真实性门禁清单快照（seal 后挂载，随 results.json 落盘；
    # None = 未经门禁/未构建 manifest——旁路直构引擎无 manifest）
    realism_manifest: Optional[Dict[str, Any]] = None

    # 扩展
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 方法 ──
    def mark_complete(self) -> None:
        # KP-P2-11/T-03：终态保护——failed 态是终态，禁止被 completed 覆盖；
        # 防止任何调用方在防未来校验 mark_failed 后误将失败回测标记为成功。
        if self.status == "failed":
            raise RuntimeError("failed 态禁止覆盖为 completed")
        self.finished_at = time.time()
        self.status = "completed"

    def mark_failed(self, error: str) -> None:
        self.finished_at = time.time()
        self.status = "failed"
        self.error_message = error

    @property
    def duration_seconds(self) -> float:
        if self.finished_at > 0:
            return self.finished_at - self.started_at
        return time.time() - self.started_at

    @property
    def is_success(self) -> bool:
        return self.status == "completed"

    @property
    def final_equity(self) -> float:
        if self.equity_curve:
            return self.equity_curve[-1].equity
        return 1.0

    def get_trades_by_symbol(self, symbol: str) -> List[TradeRecord]:
        return [t for t in self.trades if t.symbol == symbol]

    def get_winning_trades(self) -> List[TradeRecord]:
        return [t for t in self.trades if t.pnl > 0]

    def get_losing_trades(self) -> List[TradeRecord]:
        return [t for t in self.trades if t.pnl <= 0]

    # ── 序列化 ──
    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "config": self.config.to_dict(),
            "metrics": self.metrics.to_dict(),
            "summary": {
                "total_trades": len(self.trades),
                "equity_points": len(self.equity_curve),
                "final_equity": self.final_equity,
                "duration_seconds": round(self.duration_seconds, 2),
            },
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def summary(self) -> str:
        """完整摘要"""
        header = (
            f"\n{'═' * 50}\n"
            f"  回测结果: {self.config.strategy_name}\n"
            f"  期间: {self.config.start_date} ~ {self.config.end_date}\n"
            f"  标的: {', '.join(self.config.symbols)}\n"
            f"{'═' * 50}\n"
        )
        return header + self.metrics.summary()

    def __repr__(self) -> str:
        return (
            f"BacktestResult(run_id={self.run_id} "
            f"status={self.status} "
            f"trades={len(self.trades)} "
            f"equity={self.final_equity:.4f})"
        )