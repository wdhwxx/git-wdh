# 文件名：myquant/core/constants.py
"""
constants.py - 项目常量、状态码、时间单位、枚举类型
====================================================
所有量化系统共用的常量定义，避免魔法数字。

阶段9新增：数据清洗枚举
  - DirtyDataType：脏数据类型（OHLC错误/负价格/重复时间戳/价格跳变等）
  - RepairStrategy：修复策略（丢弃/标记/修正/隔离/人工）
  - QualityGrade：质量等级（优秀/良好/一般/较差/极差）
  - DataType：数据类型（BAR/TICK）
  - CleanStep：清洗步骤（校验/去重/异常检测/排序/落盘/报告）
  - SeverityLevel：严重级别（信息/警告/错误/严重）
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Dict, List

# ═══════════════════════════════════════════════
#  枚举类型
# ═══════════════════════════════════════════════

class OrderSide(Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """订单类型"""
    MARKET = "MARKET"           # 市价单
    LIMIT = "LIMIT"             # 限价单
    STOP = "STOP"               # 止损单
    STOP_LIMIT = "STOP_LIMIT"   # 止损限价
    TRAILING_STOP = "TRAILING_STOP"  # 追踪止损


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "PENDING"         # 待提交
    SUBMITTED = "SUBMITTED"     # 已提交
    PARTIAL_FILLED = "PARTIAL_FILLED"  # 部分成交
    FILLED = "FILLED"           # 全部成交
    CANCELLED = "CANCELLED"     # 已撤销
    REJECTED = "REJECTED"       # 被拒绝
    EXPIRED = "EXPIRED"         # 已过期


class MarketStatus(Enum):
    """市场状态"""
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    LUNCH_BREAK = "LUNCH_BREAK"
    AFTER_HOURS = "AFTER_HOURS"


class DataFrequency(Enum):
    """数据频率"""
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


class SignalDirection(Enum):
    """信号方向"""
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    FLAT = "FLAT"


class RiskLevel(Enum):
    """风险等级"""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class ExecutionMode(Enum):
    """运行模式"""
    BACKTEST = "backtest"
    PAPER_TRADING = "paper_trading"
    LIVE_TRADING = "live_trading"


# ═══════════════════════════════════════════════
#  时间常量 (秒)
# ═══════════════════════════════════════════════

class TimeUnit:
    SECOND = 1
    MINUTE = 60
    HOUR = 3600
    DAY = 86400
    WEEK = 604800
    MONTH = 2592000   # 30天近似
    YEAR = 31536000   # 365天近似


# ═══════════════════════════════════════════════
#  交易常量
# ═══════════════════════════════════════════════

TRADING_DAYS_PER_YEAR = 252
TRADING_DAYS_PER_MONTH = 21
TRADING_DAYS_PER_WEEK = 5
TRADING_HOURS_PER_DAY = 4

# A股交易时段 (HHMM 格式)
MARKET_OPEN_TIME = "0930"
MARKET_CLOSE_TIME = "1500"
PRE_MARKET_OPEN = "0915"
PRE_MARKET_CLOSE = "0925"
LUNCH_START = "1130"
LUNCH_END = "1300"

# 涨跌停限制
PRICE_LIMIT_MAIN = 0.10       # 主板 ±10%
PRICE_LIMIT_ST = 0.05         # ST ±5%
PRICE_LIMIT_KC = 0.20         # 科创板 ±20%
PRICE_LIMIT_CY = 0.20         # 创业板 ±20%

# 最小交易单位
LOT_SIZE = 100                # A股一手=100股
MIN_ORDER_AMOUNT = 100        # 最小委托金额

# A股交易成本（写实标准，交接文档5.5节，回测/实盘共用）
COMMISSION_RATE = 0.0002      # 佣金率：万2（0.0002），买卖双向
COMMISSION_MIN = 5.0           # 佣金最低5元
STAMP_TAX_RATE = 0.0005       # 印花税率：万5（0.05%，0.0005），仅卖出收取；2023-08-28 起由千1减半（财政部/税务总局2023年第39号公告）
TRANSFER_FEE_RATE = 0.00001   # 过户费率：万0.1（0.00001），买卖双向
SLIPPAGE_RATE = 0.0002        # 滑点：万2（0.0002），买卖双向


# ═══════════════════════════════════════════════
#  系统常量
# ═══════════════════════════════════════════════

# 回测结果缓存格式版本（P3-D10-001 整改：batch/incremental 双源收敛为单一权威）
# 1.1：batch 缓存含 config_signature；incremental 缓存按 fingerprint_hash 匹配（无 config_signature），
#     两者同属一代格式，统一由本常量声明，避免两处独立定义漂移
RESULTS_FORMAT_VERSION: str = "1.1"

DEFAULT_ENCODING = "utf-8"
DEFAULT_DATE_FMT = "%Y-%m-%d"
DEFAULT_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_TIMESTAMP_FMT = "%Y%m%d%H%M%S"

MAX_RETRY_COUNT = 3
RETRY_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30

CACHE_DEFAULT_TTL = 3600      # 缓存默认过期时间 (秒)
CACHE_MAX_SIZE = 1024         # 缓存最大条目数


# ═══════════════════════════════════════════════
#  颜色常量 (终端输出)
# ═══════════════════════════════════════════════

class Color:
    """ANSI 终端颜色"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"

    # 背景
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"

    @classmethod
    def disable(cls):
        """禁用颜色 (非终端环境)"""
        for attr in dir(cls):
            if not attr.startswith("_") and isinstance(getattr(cls, attr), str):
                setattr(cls, attr, "")


# ═══════════════════════════════════════════════
#  状态码 (内部通信)
# ═══════════════════════════════════════════════

class StatusCode(IntEnum):
    """内部状态码"""
    OK = 0
    ERROR = -1
    DATA_NOT_FOUND = 1001
    STRATEGY_ERROR = 2001
    RISK_VIOLATION = 3001
    ORDER_REJECTED = 4001
    CONNECTION_FAILED = 5001
    TIMEOUT = 5002
    CONFIG_INVALID = 6001
    CACHE_MISS = 7001
    CACHE_EXPIRED = 7002


# ═══════════════════════════════════════════════
#  映射表
# ═══════════════════════════════════════════════

ORDER_SIDE_MAP: Dict[str, OrderSide] = {
    "buy": OrderSide.BUY,
    "b": OrderSide.BUY,
    "sell": OrderSide.SELL,
    "s": OrderSide.SELL,
}

FREQUENCY_SECONDS: Dict[DataFrequency, int] = {
    DataFrequency.SECOND_1: 1,
    DataFrequency.MINUTE_1: 60,
    DataFrequency.MINUTE_5: 300,
    DataFrequency.MINUTE_15: 900,
    DataFrequency.MINUTE_30: 1800,
    DataFrequency.HOUR_1: 3600,
    DataFrequency.DAILY: 86400,
    DataFrequency.WEEKLY: 604800,
}


# ═══════════════════════════════════════════════
#  数据清洗枚举 (阶段9新增)
# ═══════════════════════════════════════════════

class DataType(Enum):
    """数据类型"""
    BAR = "bar"       # K线数据（OHLCV）
    TICK = "tick"     # 逐笔数据（price/volume/bid/ask）


class DirtyDataType(Enum):
    """
    脏数据类型

    用于标记清洗过程中发现的各类数据质量问题，
    支持按类型统计、过滤、告警和修复策略选择。

    严重级别说明:
      - ERROR级：数据逻辑错误，必须剔除或修复
      - WARNING级：数据异常但可能有效，标记保留供人工审核
    """
    # ── OHLC逻辑错误 (ERROR) ──
    OHLC_LOGIC_ERROR = "ohlc_logic_error"           # high<low / high<max(open,close) / low>min(open,close)

    # ── 价格相关错误 (ERROR) ──
    NEGATIVE_PRICE = "negative_price"                # 价格为负（open/high/low/close/price < 0）
    ZERO_PRICE = "zero_price"                        # 价格为0（可能是停牌或数据缺失）

    # ── 成交量相关错误 (ERROR/WARNING) ──
    NEGATIVE_VOLUME = "negative_volume"              # 成交量为负
    ZERO_VOLUME = "zero_volume"                      # 成交量为0（WARNING，可能是停牌）

    # ── 元数据错误 (ERROR) ──
    MISSING_SYMBOL = "missing_symbol"                # 标的代码为空
    INVALID_TIMESTAMP = "invalid_timestamp"          # 时间戳无效（<=0或无法解析）
    TIME_MONOTONIC_ERROR = "time_monotonic_error"    # 时间不单调（时间戳回退）

    # ── 重复数据 (ERROR) ──
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"      # 重复时间戳（Bar）
    DUPLICATE_TICK = "duplicate_tick"                # 重复Tick（timestamp+price+volume三元组）

    # ── 异常数据 (WARNING，标记保留) ──
    PRICE_JUMP = "price_jump"                        # 价格跳变（与前一条比较超阈值）
    VOLUME_ANOMALY = "volume_anomaly"                # 成交量异常放大（与前一条比较超阈值）

    # ── Tick特有错误 (ERROR) ──
    BID_ASK_CROSS = "bid_ask_cross"                  # 买卖盘价格交叉（bid >= ask）

    # ── 其他 ──
    UNKNOWN = "unknown"                               # 未知类型

    @property
    def severity(self) -> str:
        """严重级别：error / warning"""
        if self in (DirtyDataType.PRICE_JUMP, DirtyDataType.VOLUME_ANOMALY, DirtyDataType.ZERO_VOLUME, DirtyDataType.ZERO_PRICE):
            return "warning"
        return "error"

    @property
    def is_critical(self) -> bool:
        """是否为严重错误（必须剔除）"""
        return self.severity == "error"

    @classmethod
    def from_error_message(cls, error_msg: str) -> "DirtyDataType":
        """
        从错误消息文本推断脏数据类型

        Parameters
        ----------
        error_msg : str
            错误消息（如 "high(9.5) < low(10.5)"）

        Returns
        -------
        DirtyDataType : 推断的脏数据类型
        """
        msg = error_msg.lower()
        # 重复类优先匹配（避免"重复时间戳"被误判为时间戳无效）
        if "duplicate" in msg or "重复" in msg:
            if "tick" in msg:
                return cls.DUPLICATE_TICK
            return cls.DUPLICATE_TIMESTAMP
        if "ohlc" in msg or "high" in msg and "low" in msg:
            return cls.OHLC_LOGIC_ERROR
        if "负" in msg or "negative" in msg:
            if "volume" in msg or "量" in msg:
                return cls.NEGATIVE_VOLUME
            return cls.NEGATIVE_PRICE
        if "symbol" in msg or "标的" in msg:
            return cls.MISSING_SYMBOL
        if "timestamp" in msg or "时间戳" in msg:
            return cls.INVALID_TIMESTAMP
        if "jump" in msg or "跳变" in msg:
            return cls.PRICE_JUMP
        if "anomaly" in msg or "异常" in msg and "volume" in msg:
            return cls.VOLUME_ANOMALY
        if "bid" in msg and "ask" in msg:
            return cls.BID_ASK_CROSS
        if "monotonic" in msg or "单调" in msg:
            return cls.TIME_MONOTONIC_ERROR
        return cls.UNKNOWN


class RepairStrategy(Enum):
    """
    修复策略

    针对不同类型的脏数据，采用不同的处理方式。
    实盘场景下可按标的/数据源/数据类型差异化配置。
    """
    DROP = "drop"                    # 直接丢弃（默认，最安全）
    FLAG = "flag"                    # 标记保留（附加_errors/_warnings字段，不剔除）
    CORRECT = "correct"              # 尝试修正（如OHLC错误时用open/close修正high/low，插值修复缺失值）
    QUARANTINE = "quarantine"        # 隔离到bad目录，不进入processed数据集
    MANUAL = "manual"                # 人工审核（标记待处理，不自动决策）

    @property
    def is_automatic(self) -> bool:
        """是否为自动处理策略（无需人工介入）"""
        return self in (RepairStrategy.DROP, RepairStrategy.FLAG, RepairStrategy.CORRECT, RepairStrategy.QUARANTINE)

    @classmethod
    def default_for(cls, dirty_type: DirtyDataType) -> "RepairStrategy":
        """
        获取某类脏数据的默认修复策略

        Parameters
        ----------
        dirty_type : DirtyDataType
            脏数据类型

        Returns
        -------
        RepairStrategy : 默认修复策略
        """
        # WARNING级默认标记保留
        if dirty_type.severity == "warning":
            return cls.FLAG
        # ERROR级默认隔离
        return cls.QUARANTINE


class QualityGrade(Enum):
    """
    数据质量等级

    根据合格率（valid / total）划分等级，用于质量报告和告警阈值。
    """
    EXCELLENT = "excellent"    # 优秀：合格率 >= 99%
    GOOD = "good"              # 良好：合格率 >= 95%
    FAIR = "fair"              # 一般：合格率 >= 85%
    POOR = "poor"              # 较差：合格率 >= 70%
    CRITICAL = "critical"      # 极差：合格率 < 70%

    @property
    def min_rate(self) -> float:
        """该等级的最低合格率"""
        return {
            QualityGrade.EXCELLENT: 0.99,
            QualityGrade.GOOD: 0.95,
            QualityGrade.FAIR: 0.85,
            QualityGrade.POOR: 0.70,
            QualityGrade.CRITICAL: 0.0,
        }[self]

    @property
    def is_acceptable(self) -> bool:
        """是否可接受（>=FAIR级）"""
        return self in (QualityGrade.EXCELLENT, QualityGrade.GOOD, QualityGrade.FAIR)

    @property
    def should_alert(self) -> bool:
        """是否应触发告警（<=POOR级）"""
        return self in (QualityGrade.POOR, QualityGrade.CRITICAL)

    @classmethod
    def from_rate(cls, valid_rate: float) -> "QualityGrade":
        """
        根据合格率计算质量等级

        Parameters
        ----------
        valid_rate : float
            合格率（0.0 ~ 1.0）

        Returns
        -------
        QualityGrade : 质量等级
        """
        if valid_rate >= 0.99:
            return cls.EXCELLENT
        elif valid_rate >= 0.95:
            return cls.GOOD
        elif valid_rate >= 0.85:
            return cls.FAIR
        elif valid_rate >= 0.70:
            return cls.POOR
        else:
            return cls.CRITICAL

    @classmethod
    def from_counts(cls, valid: int, total: int) -> "QualityGrade":
        """根据有效数和总数计算质量等级"""
        if total <= 0:
            return cls.CRITICAL
        return cls.from_rate(valid / total)


class CleanStep(Enum):
    """清洗步骤"""
    LOAD = "load"                          # 数据加载
    VALIDATE = "validate"                  # 单条校验
    DEDUPLICATE = "deduplicate"            # 去重
    DETECT_ANOMALIES = "detect_anomalies"  # 异常检测
    SORT = "sort"                          # 排序输出
    SAVE_BAD = "save_bad"                  # 脏数据落盘
    SAVE_REPORT = "save_report"            # 质量报告生成
    SAVE_PROCESSED = "save_processed"      # processed数据保存

    @property
    def priority(self) -> int:
        """步骤执行优先级（数字越小越先执行）"""
        return {
            CleanStep.LOAD: 0,
            CleanStep.VALIDATE: 10,
            CleanStep.DEDUPLICATE: 20,
            CleanStep.DETECT_ANOMALIES: 30,
            CleanStep.SORT: 40,
            CleanStep.SAVE_BAD: 50,
            CleanStep.SAVE_REPORT: 60,
            CleanStep.SAVE_PROCESSED: 70,
        }[self]


class SeverityLevel(Enum):
    """严重级别（通用，用于告警/日志/事件）"""
    INFO = "info"            # 信息
    WARNING = "warning"      # 警告
    ERROR = "error"          # 错误
    CRITICAL = "critical"    # 严重

    @property
    def level(self) -> int:
        """数值级别（用于比较）"""
        return {
            SeverityLevel.INFO: 10,
            SeverityLevel.WARNING: 20,
            SeverityLevel.ERROR: 30,
            SeverityLevel.CRITICAL: 40,
        }[self]

    def __ge__(self, other: "SeverityLevel") -> bool:
        return self.level >= other.level

    def __gt__(self, other: "SeverityLevel") -> bool:
        return self.level > other.level

    def __le__(self, other: "SeverityLevel") -> bool:
        return self.level <= other.level

    def __lt__(self, other: "SeverityLevel") -> bool:
        return self.level < other.level


# ═══════════════════════════════════════════════
#  监控常量（G2-1 下沉：打断 monitoring 内部真环）
# ═══════════════════════════════════════════════

class MetricType(Enum):
    """
    指标类型（与 Prometheus 兼容）

    - COUNTER: 只增不减的计数器（如事件总数、错误次数）
    - GAUGE: 可增可减的仪表盘（如当前脏数据占比、磁盘使用率）
    - HISTOGRAM: 直方图（如请求耗时分布，当前版本简化为计数+总和）
    """
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"

    @property
    def is_monotonic(self) -> bool:
        """是否为单调递增类型"""
        return self == MetricType.COUNTER


#: 缺省"底层→上层"依赖映射：key=底层（根因候选）category，value=其可引发的上层 category。
#: 行情断流（watchdog A-4）→ 数据质量告警 / 策略漂移告警；数据质量 → 策略告警。
#: 可构造注入 dependency_map 覆盖/扩展。
DEFAULT_DEPENDENCY_MAP: Dict[str, List[str]] = {
    "watchdog": ["data_quality", "strategy"],
    "data_quality": ["strategy"],
}

#: 交易异常事件类型 → 告警级别（A-8 P1~P0：order.rejected 等 P1→ERROR、紧急强平 P0→CRITICAL）
TRADE_EXCEPTION_ALERT_LEVEL_MAP: Dict[str, SeverityLevel] = {
    "order.rejected": SeverityLevel.ERROR,            # P1：拒单
    "order.failed": SeverityLevel.ERROR,              # P1：订单发送/执行失败
    "fill.report_error": SeverityLevel.ERROR,          # P1：成交回报异常
    "emergency.liquidation": SeverityLevel.CRITICAL,  # P0：紧急强平
}

#: watchdog（A-4/A-5/A-6）默认评估/探测间隔（秒）与断连 Runbook 标识。
#: G2-5 阶段二下沉 core：live/scheduler 装配看门狗时经 L0 取用，消除
#: scheduler → monitoring.watchdog 反向依赖（EX-13D）。watchdog.py re-export 兼容旧引用。
STALENESS_PROBE_INTERVAL_SECONDS: float = 10.0   # A-4 行情活性秒级兜底评估间隔
BROKER_PROBE_INTERVAL_SECONDS: float = 5.0       # A-6 券商连接快速探测默认间隔
RUNBOOK_BROKER_DISCONNECT: str = "RB-BROKER-DISCONNECT-001"  # A-6 券商断连 Runbook 标识


# ═══════════════════════════════════════════════
#  数据清洗映射表 (阶段9新增)
# ═══════════════════════════════════════════════

# 脏数据类型 → 默认修复策略
DIRTY_TYPE_REPAIR_MAP: Dict[DirtyDataType, RepairStrategy] = {
    dt: RepairStrategy.default_for(dt) for dt in DirtyDataType
}

# 脏数据类型 → 中文描述
DIRTY_TYPE_DESC_MAP: Dict[DirtyDataType, str] = {
    DirtyDataType.OHLC_LOGIC_ERROR: "OHLC逻辑错误",
    DirtyDataType.NEGATIVE_PRICE: "负价格",
    DirtyDataType.ZERO_PRICE: "零价格",
    DirtyDataType.NEGATIVE_VOLUME: "负成交量",
    DirtyDataType.ZERO_VOLUME: "零成交量",
    DirtyDataType.MISSING_SYMBOL: "缺少标的代码",
    DirtyDataType.INVALID_TIMESTAMP: "无效时间戳",
    DirtyDataType.TIME_MONOTONIC_ERROR: "时间不单调",
    DirtyDataType.DUPLICATE_TIMESTAMP: "重复时间戳",
    DirtyDataType.DUPLICATE_TICK: "重复Tick",
    DirtyDataType.PRICE_JUMP: "价格跳变",
    DirtyDataType.VOLUME_ANOMALY: "成交量异常",
    DirtyDataType.BID_ASK_CROSS: "买卖盘价格交叉",
    DirtyDataType.UNKNOWN: "未知脏数据类型",
}

# 质量等级 → 中文描述
QUALITY_GRADE_DESC_MAP: Dict[QualityGrade, str] = {
    QualityGrade.EXCELLENT: "优秀",
    QualityGrade.GOOD: "良好",
    QualityGrade.FAIR: "一般",
    QualityGrade.POOR: "较差",
    QualityGrade.CRITICAL: "极差",
}

# 修复策略 → 中文描述
REPAIR_STRATEGY_DESC_MAP: Dict[RepairStrategy, str] = {
    RepairStrategy.DROP: "直接丢弃",
    RepairStrategy.FLAG: "标记保留",
    RepairStrategy.CORRECT: "尝试修正",
    RepairStrategy.QUARANTINE: "隔离归档",
    RepairStrategy.MANUAL: "人工审核",
}