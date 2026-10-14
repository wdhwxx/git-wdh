# 文件名：myquant/models/market_data.py
"""
market_data.py - 市场数据模型
==============================
定义行情数据结构与校验逻辑。

类清单:
  - BarFrequency  : K线频率枚举
  - Tick          : 逐笔/快照数据
  - Bar           : K线 (OHLCV)
  - Snapshot      : 盘口快照 (多档)
  - MarketEvent   : 市场事件包装
  - MarketDataValidator : 数据校验器
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# ═══════════════════════════════════════════════
#  BarFrequency - K线频率
# ═══════════════════════════════════════════════

class BarFrequency(Enum):
    """K线频率"""
    TICK = "tick"
    SECOND_1 = "1s"
    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_30 = "30m"
    HOUR_1 = "1h"
    DAILY = "1d"
    WEEKLY = "1w"
    MONTHLY = "1M"

    @property
    def seconds(self) -> int:
        """频率对应的秒数"""
        mapping = {
            BarFrequency.TICK: 0,
            BarFrequency.SECOND_1: 1,
            BarFrequency.MINUTE_1: 60,
            BarFrequency.MINUTE_5: 300,
            BarFrequency.MINUTE_15: 900,
            BarFrequency.MINUTE_30: 1800,
            BarFrequency.HOUR_1: 3600,
            BarFrequency.DAILY: 86400,
            BarFrequency.WEEKLY: 604800,
            BarFrequency.MONTHLY: 2592000,
        }
        return mapping.get(self, 0)

    def __str__(self) -> str:
        return self.value


# ═══════════════════════════════════════════════
#  Tick - 逐笔/快照数据
# ═══════════════════════════════════════════════

@dataclass
class Tick:
    """
    Tick 数据 (逐笔或快照)

    Attributes
    ----------
    symbol    : 标的代码
    timestamp : 时间戳 (秒级或毫秒级)
    price     : 最新价
    volume    : 本笔成交量
    amount    : 本笔成交额
    bid_price : 买一价
    bid_size  : 买一量
    ask_price : 卖一价
    ask_size  : 卖一量
    open      : 当日开盘价
    high      : 当日最高
    low       : 当日最低
    pre_close : 昨收
    extra     : 扩展字段
    """

    symbol: str = ""
    timestamp: float = field(default_factory=time.time)
    price: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    bid_price: float = 0.0
    bid_size: float = 0.0
    ask_price: float = 0.0
    ask_size: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_close: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 计算属性 ──
    @property
    def mid_price(self) -> float:
        """中间价"""
        if self.bid_price > 0 and self.ask_price > 0:
            return (self.bid_price + self.ask_price) / 2.0
        return self.price

    @property
    def spread(self) -> float:
        """买卖价差"""
        if self.ask_price > 0 and self.bid_price > 0:
            return self.ask_price - self.bid_price
        return 0.0

    @property
    def spread_bps(self) -> float:
        """价差 (基点)"""
        mid = self.mid_price
        if mid <= 0:
            return 0.0
        return (self.spread / mid) * 10000

    @property
    def change(self) -> float:
        """涨跌额"""
        if self.pre_close > 0:
            return self.price - self.pre_close
        return 0.0

    @property
    def change_pct(self) -> float:
        """涨跌幅"""
        if self.pre_close > 0:
            return (self.price - self.pre_close) / self.pre_close
        return 0.0

    @property
    def vwap(self) -> float:
        """当笔VWAP"""
        if self.volume > 0:
            return self.amount / self.volume
        return self.price

    # ── 校验 ──
    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.symbol:
            errors.append("symbol 不能为空")
        if self.price < 0:
            errors.append(f"price 不能为负: {self.price}")
        if self.volume < 0:
            errors.append(f"volume 不能为负: {self.volume}")
        if self.bid_price > 0 and self.ask_price > 0 and self.bid_price >= self.ask_price:
            errors.append(f"bid({self.bid_price}) >= ask({self.ask_price}) 异常")
        if self.timestamp <= 0:
            errors.append("timestamp 无效")
        return errors

    @property
    def is_valid(self) -> bool:
        return len(self.validate()) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "price": self.price,
            "volume": self.volume,
            "amount": self.amount,
            "bid_price": self.bid_price,
            "bid_size": self.bid_size,
            "ask_price": self.ask_price,
            "ask_size": self.ask_size,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "pre_close": self.pre_close,
        }

    def __repr__(self) -> str:
        return (
            f"Tick({self.symbol} px={self.price:.2f} "
            f"vol={self.volume:.0f} bid={self.bid_price:.2f} "
            f"ask={self.ask_price:.2f})"
        )


# ═══════════════════════════════════════════════
#  Bar - K线
# ═══════════════════════════════════════════════

@dataclass
class Bar:
    """
    K线 (OHLCV)

    Attributes
    ----------
    symbol    : 标的代码
    frequency : K线频率
    timestamp : Bar 起始时间戳
    open      : 开盘价
    high      : 最高价
    low       : 最低价
    close     : 收盘价
    pre_close : 昨收价（用于涨跌停计算，0=未知）
    volume    : 成交量
    amount    : 成交额
    trades    : 成交笔数
    extra     : 扩展字段
    """

    symbol: str = ""
    frequency: BarFrequency = BarFrequency.DAILY
    timestamp: float = field(default_factory=time.time)
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    pre_close: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    trades: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 计算属性 ──
    @property
    def body(self) -> float:
        """实体大小"""
        return abs(self.close - self.open)

    @property
    def upper_shadow(self) -> float:
        """上影线"""
        return self.high - max(self.open, self.close)

    @property
    def lower_shadow(self) -> float:
        """下影线"""
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        """振幅"""
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        """阳线"""
        return self.close >= self.open

    @property
    def is_bearish(self) -> bool:
        """阴线"""
        return self.close < self.open

    @property
    def vwap(self) -> float:
        """VWAP"""
        if self.volume > 0:
            return self.amount / self.volume
        return (self.high + self.low + self.close) / 3.0

    @property
    def typical_price(self) -> float:
        """典型价格"""
        return (self.high + self.low + self.close) / 3.0

    # ── 校验 ──
    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.symbol:
            errors.append("symbol 不能为空")
        if self.high < self.low:
            errors.append(f"high({self.high}) < low({self.low})")
        if self.high < max(self.open, self.close):
            errors.append("high 小于 open/close")
        if self.low > min(self.open, self.close):
            errors.append("low 大于 open/close")
        if self.volume < 0:
            errors.append(f"volume 不能为负: {self.volume}")
        if any(p < 0 for p in [self.open, self.high, self.low, self.close]):
            errors.append("价格不能为负")
        return errors

    @property
    def is_valid(self) -> bool:
        return len(self.validate()) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "frequency": self.frequency.value,
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "pre_close": self.pre_close,
            "volume": self.volume,
            "amount": self.amount,
            "trades": self.trades,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Bar":
        data = data.copy()
        if "frequency" in data and isinstance(data["frequency"], str):
            data["frequency"] = BarFrequency(data["frequency"])
        return cls(**data)

    def __repr__(self) -> str:
        return (
            f"Bar({self.symbol} {self.frequency.value} "
            f"O={self.open:.2f} H={self.high:.2f} "
            f"L={self.low:.2f} C={self.close:.2f} V={self.volume:.0f})"
        )


# ═══════════════════════════════════════════════
#  Snapshot - 盘口快照 (多档)
# ═══════════════════════════════════════════════

@dataclass
class OrderLevel:
    """单档盘口"""
    price: float = 0.0
    size: float = 0.0


@dataclass
class Snapshot:
    """
    盘口快照 (多档)

    Attributes
    ----------
    symbol    : 标的代码
    timestamp : 时间戳
    bids      : 买盘 (价格从高到低)
    asks      : 卖盘 (价格从低到高)
    last_price: 最新成交价
    volume    : 累计成交量
    amount    : 累计成交额
    """

    symbol: str = ""
    timestamp: float = field(default_factory=time.time)
    bids: List[OrderLevel] = field(default_factory=list)  # [(price, size), ...]
    asks: List[OrderLevel] = field(default_factory=list)
    last_price: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 计算属性 ──
    @property
    def best_bid(self) -> Optional[OrderLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[OrderLevel]:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2.0
        return self.last_price

    @property
    def spread(self) -> float:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return 0.0

    @property
    def bid_depth(self) -> float:
        """买盘总深度"""
        return sum(lvl.size for lvl in self.bids)

    @property
    def ask_depth(self) -> float:
        """卖盘总深度"""
        return sum(lvl.size for lvl in self.asks)

    @property
    def order_imbalance(self) -> float:
        """订单不平衡度 [-1, 1], 正=买压强"""
        total = self.bid_depth + self.ask_depth
        if total == 0:
            return 0.0
        return (self.bid_depth - self.ask_depth) / total

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "bids": [(l.price, l.size) for l in self.bids],
            "asks": [(l.price, l.size) for l in self.asks],
            "last_price": self.last_price,
            "volume": self.volume,
            "amount": self.amount,
            "spread": self.spread,
            "mid_price": self.mid_price,
        }

    def __repr__(self) -> str:
        return (
            f"Snapshot({self.symbol} last={self.last_price:.2f} "
            f"bid={self.bids[0].price if self.bids else 0:.2f} "
            f"ask={self.asks[0].price if self.asks else 0:.2f} "
            f"spread={self.spread:.2f})"
        )


# ═══════════════════════════════════════════════
#  MarketEvent - 市场事件包装
# ═══════════════════════════════════════════════

@dataclass
class MarketEvent:
    """
    市场事件 (统一包装)

    用于事件总线传输，将 Tick/Bar/Snapshot 统一封装。
    """

    event_id: str = field(default_factory=lambda: f"mkt_{int(time.time()*1000)}")
    symbol: str = ""
    event_type: str = "tick"  # "tick" | "bar" | "snapshot"
    timestamp: float = field(default_factory=time.time)
    data: Optional[Any] = None  # Tick | Bar | Snapshot
    source: str = ""            # 数据源标识
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_tick(self) -> bool:
        return self.event_type == "tick"

    @property
    def is_bar(self) -> bool:
        return self.event_type == "bar"

    @property
    def is_snapshot(self) -> bool:
        return self.event_type == "snapshot"

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "source": self.source,
        }
        if self.data and hasattr(self.data, "to_dict"):
            result["data"] = self.data.to_dict()
        return result

    def __repr__(self) -> str:
        return f"MarketEvent({self.event_type} {self.symbol} src={self.source})"


# ═══════════════════════════════════════════════
#  MarketDataValidator - 数据校验器
# ═══════════════════════════════════════════════

class MarketDataValidator:
    """
    市场数据校验器

    功能:
      - 单条数据校验
      - 批量数据校验
      - 时序连续性检查
      - 异常值检测 (价格跳变、成交量异常)

    Usage:
        validator = MarketDataValidator()
        errors = validator.validate_bar(bar)
        errors = validator.validate_series(bars)
    """

    def __init__(
        self,
        max_price_change_pct: float = 0.22,   # 单Bar最大涨跌幅 (含涨跌停+容差)
        max_volume_ratio: float = 100.0,       # 成交量异常倍数
        allow_zero_volume: bool = True,
    ):
        self.max_price_change_pct = max_price_change_pct
        self.max_volume_ratio = max_volume_ratio
        self.allow_zero_volume = allow_zero_volume

    def validate_tick(self, tick: Tick) -> List[str]:
        """校验单条Tick"""
        return tick.validate()

    def validate_bar(self, bar: Bar) -> List[str]:
        """校验单条Bar"""
        return bar.validate()

    def validate_series(self, bars: List[Bar]) -> Dict[str, Any]:
        """
        批量校验Bar序列

        Returns
        -------
        {
            "total": int,
            "valid": int,
            "errors": List[{"index": int, "errors": List[str]}],
            "warnings": List[{"index": int, "message": str}],
            "gaps": List[{"index": int, "expected_ts": float, "actual_ts": float}],
        }
        """
        result = {
            "total": len(bars),
            "valid": 0,
            "errors": [],
            "warnings": [],
            "gaps": [],
        }

        if not bars:
            return result

        prev_bar: Optional[Bar] = None

        for i, bar in enumerate(bars):
            # 基础校验
            errors = bar.validate()
            if errors:
                result["errors"].append({"index": i, "errors": errors})
                continue

            result["valid"] += 1

            # 与前一根Bar对比
            if prev_bar is not None:
                # 时间连续性
                expected_ts = prev_bar.timestamp + prev_bar.frequency.seconds
                if abs(bar.timestamp - expected_ts) > 1.0:
                    result["gaps"].append({
                        "index": i,
                        "expected_ts": expected_ts,
                        "actual_ts": bar.timestamp,
                    })

                # 价格跳变
                if prev_bar.close > 0:
                    change_pct = abs(bar.open - prev_bar.close) / prev_bar.close
                    if change_pct > self.max_price_change_pct:
                        result["warnings"].append({
                            "index": i,
                            "message": f"价格跳变 {change_pct:.2%} (open={bar.open}, prev_close={prev_bar.close})",
                        })

                # 成交量异常
                if prev_bar.volume > 0 and bar.volume > 0:
                    vol_ratio = bar.volume / prev_bar.volume
                    if vol_ratio > self.max_volume_ratio:
                        result["warnings"].append({
                            "index": i,
                            "message": f"成交量异常放大 {vol_ratio:.1f}x",
                        })

            prev_bar = bar

        return result

    def validate_snapshot(self, snapshot: Snapshot) -> List[str]:
        """校验盘口快照"""
        errors: List[str] = []
        if not snapshot.symbol:
            errors.append("symbol 不能为空")
        # 检查买盘价格递减
        for i in range(1, len(snapshot.bids)):
            if snapshot.bids[i].price >= snapshot.bids[i - 1].price:
                errors.append(f"买盘第{i+1}档价格未递减")
                break
        # 检查卖盘价格递增
        for i in range(1, len(snapshot.asks)):
            if snapshot.asks[i].price <= snapshot.asks[i - 1].price:
                errors.append(f"卖盘第{i+1}档价格未递增")
                break
        # 交叉检查
        if snapshot.bids and snapshot.asks:
            if snapshot.bids[0].price >= snapshot.asks[0].price:
                errors.append("盘口交叉: bid >= ask")
        return errors

    def __repr__(self) -> str:
        return f"MarketDataValidator(max_change={self.max_price_change_pct:.0%})"