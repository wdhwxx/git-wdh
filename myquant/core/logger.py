# 文件名：myquant/core/logger.py
"""
logger.py - 统一日志配置
=========================
功能:
  1. 多输出目标: 控制台 + 文件 + (可选)网络
  2. 按级别分文件: info.log / error.log / trade.log
  3. 日志轮转 (大小/时间)
  4. 结构化字段 (模块名、策略名、订单号)
  5. 彩色控制台输出
  6. 全局统一配置入口
  7. data_quality 专用日志通道: 清洗/实时行情告警独立落盘到
     logs/data_quality/ 子目录（WARNING及以上），结构化字段
     source(offline/live) + symbol，与全量日志双写互不影响，
     为实时行情与实盘无缝对接预留统一告警入口
  8. 回测日志模式 (V5 P2 新增): 消除回测高频日志风暴对主线程的阻塞
     - 日志降级: 回测模式自动提升 root 级别（默认 WARNING）
     - 异步队列: root handler 移交 QueueListener 后台线程写盘，主线程仅入队
     - 采样输出: 对高频 logger 前缀按"每 N 条/每 N 秒 输出 1 条"采样
     - 静音清单: 彻底静音指定 logger 前缀
     - 队列保护: 队列满时丢弃日志而非阻塞主线程（日志可丢，回测不卡）
     - 隔离红线: 只作用于 root 的 level/handlers/filters，
       trade / data_quality 专用 logger 不受影响（交易记录、告警不可丢）

依赖: logging, logging.handlers, queue, threading, time, re, sys, pathlib, datetime
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

# ─────────────────────────────────────────────
# 颜色支持
# ─────────────────────────────────────────────

class _Colors:
    RESET = "\033[0m"
    DEBUG = "\033[36m"     # Cyan
    INFO = "\033[32m"      # Green
    WARNING = "\033[33m"   # Yellow
    ERROR = "\033[31m"     # Red
    CRITICAL = "\033[35m"  # Magenta
    BOLD = "\033[1m"


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


class ColoredFormatter(logging.Formatter):
    """带颜色的控制台格式化器"""

    LEVEL_COLORS = {
        logging.DEBUG: _Colors.DEBUG,
        logging.INFO: _Colors.INFO,
        logging.WARNING: _Colors.WARNING,
        logging.ERROR: _Colors.ERROR,
        logging.CRITICAL: _Colors.CRITICAL,
    }

    def __init__(self, fmt: str, datefmt: str = "%Y-%m-%d %H:%M:%S", use_color: bool = True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color and _supports_color()

    def format(self, record: logging.LogRecord) -> str:
        if self.use_color:
            color = self.LEVEL_COLORS.get(record.levelno, _Colors.RESET)
            record.levelname = f"{color}{record.levelname:<8}{_Colors.RESET}"
        return super().format(record)


# ─────────────────────────────────────────────
# 日志格式模板
# ─────────────────────────────────────────────

CONSOLE_FMT = "%(asctime)s │ %(levelname)s │ %(name)s │ %(message)s"
FILE_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"
TRADE_FMT = "%(asctime)s | %(levelname)-8s | %(message)s"
# data_quality 专用格式: 时间 | 级别 | 来源(offline/live) | 标的 | 告警内容
DATA_QUALITY_FMT = "%(asctime)s | %(levelname)-8s | %(source)s | %(symbol)s | %(message)s"

# data_quality 通道路由前缀:
#   data_clean.*  离线清洗管线（bar/tick/pipeline/rules_loader/quality_report）
#   live.*        实盘模块（data_feed 实时行情流式清洗告警）
#   data_quality  统一告警 API（log_data_quality_alert / get_data_quality_logger）
DATA_QUALITY_LOGGER_PREFIXES = ("data_clean.", "live.", "data_quality")


# ─────────────────────────────────────────────
# data_quality 专用过滤器与格式化器
# ─────────────────────────────────────────────

# 从告警消息中自动提取标的代码（如 600519 / 600519.SH / 000001.SZ）
_SYMBOL_PATTERN = re.compile(r"\b\d{6}(?:\.(?:SH|SZ|BJ|sh|sz|bj))?\b")


class DataQualityFilter(logging.Filter):
    """
    data_quality 通道路由过滤器

    职责:
      1. 仅放行数据质量相关命名空间的日志记录（离线清洗/实时行情/统一API）
      2. 为每条记录注入结构化字段:
         - source: 按命名空间推断 "offline"(离线清洗) / "live"(实盘实时) / "api"
           （若调用方通过 extra 显式指定则优先使用，实盘对接零改造）
         - symbol: 优先取 extra 中的 symbol，否则从消息文本自动提取；
           提取不到时填 "-"
      3. 作为 logging.Filter 挂在专用 handler 上，不影响记录在其他
         handler（控制台/全量日志/错误日志）上的正常输出——双写机制
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        # 命名空间路由: data_clean.* / live.* / data_quality
        if not (name.startswith(DATA_QUALITY_LOGGER_PREFIXES) or name == "data_quality"):
            return False

        # source 字段: 显式指定优先，否则按命名空间推断
        if not hasattr(record, "source"):
            if name.startswith("live"):
                record.source = "live"
            elif name.startswith("data_clean"):
                record.source = "offline"
            else:
                record.source = "api"

        # symbol 字段: 显式指定优先，否则从消息正则提取
        if not hasattr(record, "symbol"):
            matched = _SYMBOL_PATTERN.search(record.getMessage())
            record.symbol = matched.group(0) if matched else "-"
        return True


class DataQualityFormatter(logging.Formatter):
    """
    data_quality 专用格式化器

    确保 source / symbol 字段存在默认值，避免调用方直接写该
    logger 时因缺字段而格式化失败（健壮性优先）。
    """

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "source"):
            record.source = "-"
        if not hasattr(record, "symbol"):
            record.symbol = "-"
        return super().format(record)


# ─────────────────────────────────────────────
# 核心配置函数
# ─────────────────────────────────────────────

_logging_initialized = False


def setup_logging(
    level: int = logging.INFO,
    log_dir: Optional[Union[str, Path]] = None,
    console: bool = True,
    file_output: bool = True,
    trade_log: bool = True,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    app_name: str = "myquant",
    data_quality_log: bool = True,
) -> None:
    """
    全局日志初始化 (仅执行一次)

    Parameters
    ----------
    level : 日志级别
    log_dir : 日志目录，默认 logs/
    console : 是否输出到控制台
    file_output : 是否输出到文件
    trade_log : 是否单独记录交易日志
    max_bytes : 单文件最大字节数
    backup_count : 保留文件数
    app_name : 应用名 (日志文件名前缀)
    data_quality_log : 是否启用数据质量专用日志通道
        （logs/data_quality/ 子目录，WARNING及以上，
        自动路由 data_clean.*/live.*/data_quality 命名空间）
    """
    global _logging_initialized
    if _logging_initialized:
        return
    _logging_initialized = True

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除已有 handler
    root_logger.handlers.clear()

    # ── 控制台 ──
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(
            ColoredFormatter(CONSOLE_FMT, use_color=True)
        )
        root_logger.addHandler(console_handler)

    # ── 文件输出 ──
    if file_output or trade_log:
        if log_dir is None:
            log_dir = Path("logs")
        else:
            log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d")

        if file_output:
            # 全量日志
            all_handler = logging.handlers.RotatingFileHandler(
                log_dir / f"{app_name}_{timestamp}.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            all_handler.setLevel(logging.DEBUG)
            all_handler.setFormatter(logging.Formatter(FILE_FMT))
            root_logger.addHandler(all_handler)

            # 错误日志
            error_handler = logging.handlers.RotatingFileHandler(
                log_dir / f"{app_name}_error_{timestamp}.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(logging.Formatter(FILE_FMT))
            root_logger.addHandler(error_handler)

        if trade_log:
            # 交易日志 (专用logger)
            trade_logger = logging.getLogger("trade")
            trade_logger.setLevel(logging.INFO)
            trade_logger.propagate = False  # 不传播到root

            trade_handler = logging.handlers.RotatingFileHandler(
                log_dir / f"{app_name}_trade_{timestamp}.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            trade_handler.setFormatter(logging.Formatter(TRADE_FMT))
            trade_logger.addHandler(trade_handler)

        # ── data_quality 专用通道（蓝图: logs/data_quality/ 清洗告警子目录）──
        # 双写机制: 过滤器挂在专用handler上，清洗/实时告警照常写入
        # 控制台与全量日志，同时独立落盘到 data_quality 子目录。
        # 离线清洗代码零改动（命名空间 data_clean.* 自动路由）。
        if data_quality_log:
            dq_dir = log_dir / "data_quality"
            dq_dir.mkdir(parents=True, exist_ok=True)

            dq_handler = logging.handlers.RotatingFileHandler(
                dq_dir / f"data_quality_{timestamp}.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            dq_handler.setLevel(logging.WARNING)  # 只收告警及以上
            dq_handler.setFormatter(DataQualityFormatter(DATA_QUALITY_FMT))
            dq_handler.addFilter(DataQualityFilter())
            root_logger.addHandler(dq_handler)

            # 统一告警API logger: 确保级别不低于WARNING，
            # 实时行情/实盘调用 log_data_quality_alert 时告警必达
            dq_logger = logging.getLogger("data_quality")
            if dq_logger.getEffectiveLevel() > logging.WARNING:
                dq_logger.setLevel(logging.WARNING)


# ─────────────────────────────────────────────
# 便捷获取 logger
# ─────────────────────────────────────────────

def get_logger(name: str = "myquant") -> logging.Logger:
    """
    获取命名 logger

    Usage:
        logger = get_logger("strategy.ma_cross")
        logger.info("信号触发: BUY")
    """
    return logging.getLogger(name)


def get_trade_logger() -> logging.Logger:
    """获取交易日志专用 logger"""
    return logging.getLogger("trade")


def get_data_quality_logger() -> logging.Logger:
    """
    获取数据质量统一告警 logger

    用于实时行情/实盘场景中手动上报数据质量告警；
    该 logger 的记录会自动路由到 logs/data_quality/ 专用通道
    （需 setup_logging 已启用 data_quality_log）。

    Usage:
        dq_logger = get_data_quality_logger()
        dq_logger.warning("脏数据告警: 600519 errors=[OHLC非法]",
                          extra={"source": "live", "symbol": "600519"})
    """
    return logging.getLogger("data_quality")


def log_data_quality_alert(
    message: str,
    source: str = "live",
    symbol: str = "-",
    level: int = logging.WARNING,
    exc_info: bool = False,
) -> None:
    """
    数据质量统一告警 API（实时行情/实盘无缝对接入口）

    实时行情接入、实盘引擎、清洗管线等任何环节发现数据质量问题，
    统一调用本函数上报；告警同时进入:
      - logs/data_quality/ 专用文件（结构化: 时间|级别|来源|标的|内容）
      - 控制台与全量日志（双写，运维可见）

    Parameters
    ----------
    message : 告警内容
    source : 来源标识（"offline"=离线清洗, "live"=实时行情/实盘,
             "api"=统一调用；后续新增数据源可扩展此字段）
    symbol : 标的代码（未知时传 "-"）
    level : 日志级别（默认 WARNING；严重问题可传 logging.ERROR）
    exc_info : 是否附带异常堆栈

    Usage:
        log_data_quality_alert(
            "脏数据告警: 价格跳变 18.5%",
            source="live", symbol="600519",
        )
    """
    logger = logging.getLogger("data_quality")
    logger.log(
        level,
        message,
        extra={"source": source, "symbol": symbol},
        exc_info=exc_info,
    )


# ─────────────────────────────────────────────
# 日志上下文 (结构化字段)
# ─────────────────────────────────────────────

class LogContext:
    """
    日志上下文管理器，为日志附加结构化字段

    Usage:
        with LogContext(strategy="MA_Cross", symbol="600519"):
            logger.info("信号触发: BUY")
    """

    def __init__(self, **fields):
        self.fields = fields
        self._token = None

    def __enter__(self):
        # 将字段注入当前线程的 logging filter
        self._filter = _ContextFilter(self.fields)
        logging.getLogger().addFilter(self._filter)
        return self

    def __exit__(self, *args):
        logging.getLogger().removeFilter(self._filter)


class _ContextFilter(logging.Filter):
    def __init__(self, fields: dict):
        super().__init__()
        self.fields = fields

    def filter(self, record: logging.LogRecord) -> bool:
        for k, v in self.fields.items():
            setattr(record, k, v)
        return True


# ─────────────────────────────────────────────
# 模块加载时自动初始化 (可选)
# ─────────────────────────────────────────────
# 取消注释以在 import 时自动配置:
# setup_logging()


# ─────────────────────────────────────────────
# 兼容别名：外部代码使用 setup_logger 导入
# ─────────────────────────────────────────────

def setup_logger(*args, **kwargs):
    """setup_logging 的兼容别名"""
    return setup_logging(*args, **kwargs)



# ─────────────────────────────────────────────
# 回测日志模式（V5 P2 新增）
# ─────────────────────────────────────────────
# 目标：消除回测高并发/高频率日志（日志风暴）对主线程的阻塞。
# 手段：
#   1. 日志降级  : 回测模式自动提升 root 级别（默认 WARNING），抑制海量 INFO/DEBUG
#   2. 异步队列  : root 的 handler 移交 QueueListener 后台线程写盘，主线程仅入队（O(1)）
#   3. 采样输出  : 对高频 logger 前缀按"每 N 条输出 1 条 / 每 N 秒输出 1 条"采样
#   4. 静音清单  : 彻底静音指定 logger 前缀
#   5. 队列保护  : 队列满时丢弃日志而非阻塞主线程（日志可丢，回测不卡）
# 隔离红线：只作用于 root logger 的 level/handlers/filters；
#           trade / data_quality 专用 logger 保持原样（交易记录、告警不可丢）。

# 回测模式默认常量
DEFAULT_QUEUE_MAXSIZE: int = 10000
"""异步日志队列最大容量（超出即丢弃，绝不阻塞主线程）"""
DEFAULT_BACKTEST_LEVEL: int = logging.WARNING
"""回测模式默认降级级别"""
DEFAULT_SAMPLE_RATE: int = 1
"""默认采样率：每 N 条输出 1 条（1=不采样）"""
DEFAULT_QUEUE_FULL_WARN_INTERVAL: float = 5.0
"""队列满时向 stderr 告警的最小间隔（秒）"""


class SamplingFilter(logging.Filter):
    """高频日志采样过滤器。

    对匹配 logger 名前缀的日志记录按"计数采样"或"时间采样"放行，
    不匹配前缀的记录全部放行（不影响其他模块）。

    Attributes:
        sample_rate: 每 N 条输出 1 条；1 表示不按计数采样。
        sample_interval: 每 N 秒最多输出 1 条；0 表示不按时间采样。
        match_prefixes: 需要采样的 logger 名前缀元组；None/空 表示采样所有。
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        sample_interval: float = 0.0,
        match_prefixes: Optional[Sequence[str]] = None,
    ) -> None:
        super().__init__()
        if sample_rate < 1:
            raise ValueError(f"sample_rate 必须 >= 1，收到: {sample_rate}")
        if sample_interval < 0:
            raise ValueError(f"sample_interval 必须 >= 0，收到: {sample_interval}")
        self.sample_rate: int = int(sample_rate)
        self.sample_interval: float = float(sample_interval)
        self.match_prefixes: Tuple[str, ...] = (
            tuple(match_prefixes) if match_prefixes else ()
        )
        self._count: int = 0
        self._last_emit_ts: float = 0.0
        self._skipped: int = 0
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        # 未配置采样前缀或记录名匹配前缀才采样；否则全部放行
        if self.match_prefixes and not record.name.startswith(self.match_prefixes):
            return True

        with self._lock:
            # 计数采样：每 sample_rate 条放行 1 条
            if self.sample_rate > 1:
                self._count += 1
                if self._count % self.sample_rate != 0:
                    self._skipped += 1
                    return False
            # 时间采样：距上次放行不足 interval 秒则丢弃
            if self.sample_interval > 0:
                now = time.monotonic()
                if now - self._last_emit_ts < self.sample_interval:
                    self._skipped += 1
                    return False
                self._last_emit_ts = now
        return True

    @property
    def skipped(self) -> int:
        """被采样丢弃的记录数。"""
        with self._lock:
            return self._skipped


class MuteFilter(logging.Filter):
    """静音过滤器：匹配前缀的 logger 记录全部丢弃。

    Attributes:
        prefixes: 需要静音的 logger 名前缀元组。
    """

    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        if not prefixes:
            raise ValueError("prefixes 不能为空")
        self.prefixes: Tuple[str, ...] = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(self.prefixes)


class _BacktestQueueHandler(logging.handlers.QueueHandler):
    """回测异步队列处理器：队列满时丢弃日志而非阻塞主线程。

    日志风暴场景下宁可丢弃部分日志，也绝不让主线程卡在入队上。
    丢弃量通过 get_backtest_logging_stats() 可查；队列满告警每
    DEFAULT_QUEUE_FULL_WARN_INTERVAL 秒向 stderr 输出一次
    （不走日志系统，避免递归入队）。
    """

    def __init__(self, q: queue.Queue) -> None:
        super().__init__(q)
        self._dropped: int = 0
        self._lock = threading.Lock()
        self._last_warn_ts: float = 0.0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            now = time.monotonic()
            with self._lock:
                self._dropped += 1
                if now - self._last_warn_ts >= DEFAULT_QUEUE_FULL_WARN_INTERVAL:
                    self._last_warn_ts = now
                    try:
                        sys.stderr.write(
                            f"[logger] 异步日志队列已满，已丢弃 {self._dropped} 条"
                            "（为不阻塞回测主线程，日志被丢弃）\n"
                        )
                    except Exception:
                        pass

    @property
    def dropped(self) -> int:
        """队列满时被丢弃的记录数。"""
        with self._lock:
            return self._dropped


# 模块级 logger（回测模式内部自用）
logger = logging.getLogger(__name__)


# 回测日志模式全局状态（线程安全）
_BT_STATE_LOCK = threading.RLock()
_bt_enabled: bool = False
_bt_refcount: int = 0
_bt_orig_level: Optional[int] = None
_bt_orig_handlers: Optional[List[logging.Handler]] = None
_bt_orig_filters: Optional[List[logging.Filter]] = None
_bt_queue_handler: Optional[_BacktestQueueHandler] = None
_bt_listener: Optional[logging.handlers.QueueListener] = None
_bt_mute_filter: Optional[MuteFilter] = None
_bt_sample_filter: Optional[SamplingFilter] = None
_bt_queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE
_bt_filtered_handlers: Optional[List[logging.Handler]] = None


def enable_backtest_logging(
    *,
    level: int = DEFAULT_BACKTEST_LEVEL,
    async_queue: bool = True,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    sample_interval: float = 0.0,
    sample_prefixes: Optional[Sequence[str]] = None,
    quiet_loggers: Sequence[str] = (),
    queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
) -> None:
    """开启回测日志模式（日志降级 + 异步队列 + 采样 + 静音）。

    只作用于 root logger 的 level/handlers/filters；
    trade / data_quality 专用 logger 不受影响。
    支持嵌套：已开启时再次调用仅递增引用计数。

    Args:
        level: 降级后的 root 级别，默认 WARNING。
        async_queue: 是否启用异步队列（QueueHandler + QueueListener），默认 True。
        sample_rate: 计数采样率，每 N 条输出 1 条；1 表示不采样。
        sample_interval: 时间采样间隔（秒），每 N 秒最多输出 1 条；0 表示不按时间采样。
        sample_prefixes: 需要采样的 logger 名前缀；None/空 表示对所有记录采样。
        quiet_loggers: 彻底静音的 logger 名前缀序列。
        queue_maxsize: 异步队列最大容量，默认 10000。

    Raises:
        ValueError: 参数非法。
    """
    global _bt_refcount, _bt_enabled, _bt_orig_level, _bt_orig_handlers, _bt_orig_filters
    global _bt_queue_handler, _bt_listener, _bt_mute_filter, _bt_sample_filter, _bt_queue_maxsize
    global _bt_filtered_handlers

    if not (logging.DEBUG <= level <= logging.CRITICAL):
        raise ValueError(f"level 必须在 DEBUG..CRITICAL 之间，收到: {level}")
    if sample_rate < 1:
        raise ValueError(f"sample_rate 必须 >= 1，收到: {sample_rate}")
    if sample_interval < 0:
        raise ValueError(f"sample_interval 必须 >= 0，收到: {sample_interval}")
    if queue_maxsize < 1:
        raise ValueError(f"queue_maxsize 必须 >= 1，收到: {queue_maxsize}")

    with _BT_STATE_LOCK:
        if _bt_enabled:
            # 嵌套：仅递增引用计数，不重复配置
            _bt_refcount += 1
            logger.info("回测日志模式已处于开启状态（引用计数 %d）", _bt_refcount)
            return

        root = logging.getLogger()

        # 记录原状态
        _bt_orig_level = root.level
        _bt_orig_handlers = list(root.handlers)
        _bt_orig_filters = list(root.filters)
        _bt_queue_maxsize = queue_maxsize

        # 1. 日志降级
        root.setLevel(level)
        logger.info("回测日志模式：root 级别降级为 %s", logging.getLevelName(level))

        # 2. 异步队列：root 挂 QueueHandler，原 handler 移交 QueueListener 后台线程
        if async_queue:
            log_queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
            _bt_queue_handler = _BacktestQueueHandler(log_queue)
            root.handlers.clear()
            root.addHandler(_bt_queue_handler)
            _bt_listener = logging.handlers.QueueListener(
                log_queue, *_bt_orig_handlers, respect_handler_level=True
            )
            _bt_listener.start()
            logger.info("回测日志模式：已启用异步队列（容量 %d）", queue_maxsize)

        # 3. 采样过滤器（挂到 root 的 handler 上：logger 层 filter 只作用于
        #    发出记录的 logger 自身，对子 logger 产生的记录无效，故必须挂在 handler 层）
        if sample_rate > 1 or sample_interval > 0:
            _bt_sample_filter = SamplingFilter(
                sample_rate=sample_rate,
                sample_interval=sample_interval,
                match_prefixes=sample_prefixes,
            )
            for h in root.handlers:
                h.addFilter(_bt_sample_filter)
            _bt_filtered_handlers = list(root.handlers)
            logger.info(
                "回测日志模式：启用采样（每 %d 条 / 每 %.1f 秒 输出 1 条，前缀=%s）",
                sample_rate, sample_interval, sample_prefixes,
            )

        # 4. 静音过滤器（同样挂 handler 层）
        if quiet_loggers:
            _bt_mute_filter = MuteFilter(quiet_loggers)
            for h in root.handlers:
                h.addFilter(_bt_mute_filter)
            _bt_filtered_handlers = list(root.handlers)
            logger.info("回测日志模式：静音 logger 前缀 %s", quiet_loggers)

        _bt_enabled = True
        _bt_refcount = 1


def disable_backtest_logging(timeout: float = 5.0) -> None:
    """关闭回测日志模式并恢复原始配置。

    Args:
        timeout: 等待异步队列排空的最长时间（秒）。

    Raises:
        ValueError: timeout 非法。
    """
    global _bt_refcount, _bt_enabled, _bt_orig_level, _bt_orig_handlers, _bt_orig_filters
    global _bt_queue_handler, _bt_listener, _bt_mute_filter, _bt_sample_filter
    global _bt_filtered_handlers

    if timeout < 0:
        raise ValueError(f"timeout 必须 >= 0，收到: {timeout}")

    with _BT_STATE_LOCK:
        if not _bt_enabled:
            return
        if _bt_refcount > 1:
            # 嵌套未全部退出：仅递减引用计数
            _bt_refcount -= 1
            logger.info("回测日志模式引用计数 %d，暂不恢复配置", _bt_refcount)
            return

        root = logging.getLogger()

        # 先停掉异步 listener（stop 会排空队列中的剩余记录）
        if _bt_listener is not None:
            _bt_listener.stop()
            _bt_listener = None

        # 移除 QueueHandler，恢复原 handler
        root.handlers.clear()
        if _bt_orig_handlers:
            for h in _bt_orig_handlers:
                root.addHandler(h)

        # 从 handler 移除回测期间添加的采样/静音 filter
        if _bt_filtered_handlers:
            for h in _bt_filtered_handlers:
                if _bt_sample_filter is not None:
                    h.removeFilter(_bt_sample_filter)
                if _bt_mute_filter is not None:
                    h.removeFilter(_bt_mute_filter)

        # 恢复原 filters
        root.filters.clear()
        if _bt_orig_filters:
            for f in _bt_orig_filters:
                root.addFilter(f)

        # 恢复原级别
        if _bt_orig_level is not None:
            root.setLevel(_bt_orig_level)

        _bt_queue_handler = None
        _bt_mute_filter = None
        _bt_sample_filter = None
        _bt_enabled = False
        _bt_refcount = 0
        _bt_orig_level = None
        _bt_orig_handlers = None
        _bt_orig_filters = None
        _bt_filtered_handlers = None
        logger.info("回测日志模式已关闭，root 日志配置已恢复")


def is_backtest_logging_enabled() -> bool:
    """查询回测日志模式是否开启。"""
    with _BT_STATE_LOCK:
        return _bt_enabled


def flush_logging(timeout: float = 5.0) -> None:
    """等待异步日志队列清空（近似 flush），不改变回测模式状态。

    回测中途需要确保日志已写盘时调用。

    Args:
        timeout: 最长等待时间（秒）。
    """
    if timeout < 0:
        raise ValueError(f"timeout 必须 >= 0，收到: {timeout}")
    with _BT_STATE_LOCK:
        qh = _bt_queue_handler
        listener = _bt_listener
    if qh is None or listener is None:
        return
    log_queue = qh.queue
    deadline = time.monotonic() + timeout
    while not log_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.005)
    # 队列空后，给后台线程一点时间处理最后一批
    time.sleep(0.01)


def shutdown_logging(timeout: float = 5.0) -> None:
    """关闭回测日志模式并恢复原始配置（disable_backtest_logging 别名）。

    回测结束/进程退出前调用，确保异步队列中的日志全部落盘。
    """
    disable_backtest_logging(timeout=timeout)


@contextmanager
def backtest_logging_context(
    *,
    level: int = DEFAULT_BACKTEST_LEVEL,
    async_queue: bool = True,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    sample_interval: float = 0.0,
    sample_prefixes: Optional[Sequence[str]] = None,
    quiet_loggers: Sequence[str] = (),
    queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    timeout: float = 5.0,
) -> Iterator[None]:
    """回测日志模式上下文管理器。

    进入时开启回测日志模式（降级/异步/采样/静音），退出时关闭并恢复。
    支持嵌套（引用计数），内层退出不会破坏外层。

    Example:
        >>> with backtest_logging_context(level=logging.WARNING, sample_rate=100):
        ...     run_backtest()
    """
    enable_backtest_logging(
        level=level,
        async_queue=async_queue,
        sample_rate=sample_rate,
        sample_interval=sample_interval,
        sample_prefixes=sample_prefixes,
        quiet_loggers=quiet_loggers,
        queue_maxsize=queue_maxsize,
    )
    try:
        yield
    finally:
        disable_backtest_logging(timeout=timeout)


def get_backtest_logging_stats() -> Dict[str, Any]:
    """获取回测日志模式状态统计。

    Returns:
        包含 enabled/refcount/级别/队列大小/丢弃数/采样丢弃数的字典。
    """
    with _BT_STATE_LOCK:
        qh = _bt_queue_handler
        listener = _bt_listener
        sample_filter = _bt_sample_filter
        queue_maxsize = _bt_queue_maxsize
        enabled = _bt_enabled
        refcount = _bt_refcount
        current_level: Optional[int] = None
        if enabled:
            current_level = logging.getLogger().level
    return {
        "enabled": enabled,
        "refcount": refcount,
        "current_level": current_level,
        "async_queue": listener is not None,
        "queue_maxsize": queue_maxsize,
        "queue_size": qh.queue.qsize() if qh is not None else 0,
        "dropped_records": qh.dropped if qh is not None else 0,
        "sampling_skipped": sample_filter.skipped if sample_filter is not None else 0,
    }


# ─────────────────────────────────────────────
# 回测日志模式 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import shutil
    import tempfile
    from pathlib import Path as _Path

    print("=" * 60)
    print("  logger.py — 回测日志模式 自测 Demo")
    print("=" * 60)

    ok_count = [0]
    total_count = [0]

    def _ok(cond: bool, label: str) -> None:
        total_count[0] += 1
        mark = "✓" if cond else "✗"
        print(f"    {mark} {label}")
        if cond:
            ok_count[0] += 1

    tmp_dir = _Path(tempfile.mkdtemp(prefix="logger_bt_test_"))
    all_log_path: Optional[_Path] = None

    def _find_all_log() -> _Path:
        """定位全量日志文件（排除 error 文件）。"""
        files = [p for p in tmp_dir.glob("myquant_*.log") if "error" not in p.name]
        return files[0]

    try:
        # ── 初始化日志（文件模式，无控制台） ──
        setup_logging(
            log_dir=tmp_dir, console=False, file_output=True,
            trade_log=False, data_quality_log=False,
        )
        all_log_path = _find_all_log()
        print(f"\n[初始化] 全量日志文件: {all_log_path.name}")

        # ── 测试 1: 日志降级（level=WARNING 时 INFO 不落盘，WARNING 落盘） ──
        print("\n[1] 日志降级测试")
        enable_backtest_logging(level=logging.WARNING, async_queue=False)
        _ok(is_backtest_logging_enabled(), "回测模式已开启")
        stats = get_backtest_logging_stats()
        _ok(stats["current_level"] == logging.WARNING, "root 级别已降级为 WARNING")

        logging.getLogger("myquant.backtest.engine").info("降级测试-INFO 不应出现")
        logging.getLogger("myquant.backtest.engine").warning("降级测试-WARNING 应出现")
        disable_backtest_logging()
        _ok(is_backtest_logging_enabled() is False, "关闭后回测模式解除")
        content1 = all_log_path.read_text(encoding="utf-8")
        _ok("降级测试-WARNING 应出现" in content1, "WARNING 已落盘")
        _ok("降级测试-INFO 不应出现" not in content1, "INFO 被降级拦截")
        print(f"    文件新增行数: {len([line for line in content1.splitlines() if '降级测试' in line])}")

        # ── 测试 2: 异步队列 + 采样输出 ──
        print("\n[2] 异步队列 + 采样测试")
        before_lines = len(all_log_path.read_text(encoding="utf-8").splitlines())
        enable_backtest_logging(
            level=logging.INFO, async_queue=True,
            sample_rate=10, sample_prefixes=("myquant.backtest",),
        )
        eng_logger = logging.getLogger("myquant.backtest.engine")
        for i in range(1000):
            eng_logger.info(f"采样测试-序号-{i}")
        # 未 flush 时文件不应有完整内容（异步待写）——只验证能快速返回
        stats_mid = get_backtest_logging_stats()
        _ok(stats_mid["async_queue"] is True, "异步队列已启用")
        _ok(stats_mid["queue_size"] >= 0, "队列状态可查")
        flush_logging(timeout=5.0)
        disable_backtest_logging(timeout=5.0)

        after_lines = len(all_log_path.read_text(encoding="utf-8").splitlines())
        added = after_lines - before_lines
        print(f"    采样 1000 条 INFO（每 10 条输出 1 条），新增日志行: {added}")
        _ok(90 <= added <= 110, "采样输出量约为 1000/10=100 条（±10 容差）")

        # ── 测试 3: 静音清单 ──
        print("\n[3] 静音清单测试")
        before_lines = len(all_log_path.read_text(encoding="utf-8").splitlines())
        enable_backtest_logging(
            level=logging.INFO, async_queue=False,
            quiet_loggers=("noisy.",),
        )
        logging.getLogger("noisy.metric").info("静音测试-不应出现")
        logging.getLogger("myquant.backtest").info("静音测试-正常出现")
        disable_backtest_logging()
        after_lines = len(all_log_path.read_text(encoding="utf-8").splitlines())
        added = after_lines - before_lines
        content3 = all_log_path.read_text(encoding="utf-8")
        _ok("静音测试-不应出现" not in content3, "静音前缀日志被拦截")
        _ok("静音测试-正常出现" in content3, "非静音日志正常落盘")
        print(f"    新增日志行: {added}")

        # ── 测试 4: 嵌套上下文 ──
        print("\n[4] 嵌套上下文")
        with backtest_logging_context(level=logging.WARNING, async_queue=False):
            _ok(is_backtest_logging_enabled(), "外层开启")
            with backtest_logging_context(level=logging.INFO, async_queue=False):
                _ok(is_backtest_logging_enabled(), "内层仍开启（嵌套）")
            _ok(is_backtest_logging_enabled(), "内层退出后外层仍开启")
        _ok(is_backtest_logging_enabled() is False, "外层退出后全部关闭")

        # ── 测试 5: 异步性能对比 ──
        print("\n[5] 异步 vs 同步性能对比")
        n = 5000
        # 同步基准（回测模式关闭状态）
        t0 = time.perf_counter()
        for i in range(n):
            logging.getLogger("myquant.backtest.engine").info(f"同步-{i}")
        sync_cost = time.perf_counter() - t0
        # 清空已写内容，避免影响计数（重新初始化不必要，直接继续）
        # 异步
        enable_backtest_logging(level=logging.INFO, async_queue=True, sample_rate=1)
        t0 = time.perf_counter()
        for i in range(n):
            logging.getLogger("myquant.backtest.engine").info(f"异步-{i}")
        async_enqueue_cost = time.perf_counter() - t0
        disable_backtest_logging(timeout=10.0)
        print(f"    同步写 {n} 条: {sync_cost:.3f}s")
        print(f"    异步入队 {n} 条: {async_enqueue_cost:.3f}s")
        _ok(async_enqueue_cost <= sync_cost, "异步入队不慢于同步写盘")

        # ── 测试 6: 参数校验 ──
        print("\n[6] 参数校验")
        try:
            enable_backtest_logging(sample_rate=0)
            _ok(False, "sample_rate=0 应抛 ValueError")
        except ValueError:
            _ok(True, "sample_rate=0 正确抛 ValueError")
        try:
            disable_backtest_logging(timeout=-1)
            _ok(False, "timeout=-1 应抛 ValueError")
        except ValueError:
            _ok(True, "timeout=-1 正确抛 ValueError")

        # ── 测试 7: 统计信息 ──
        print("\n[7] 统计信息")
        enable_backtest_logging(level=logging.INFO, async_queue=True, sample_rate=5)
        for i in range(100):
            logging.getLogger("myquant.backtest.engine").info(f"统计-{i}")
        stats7 = get_backtest_logging_stats()
        print(f"    stats: {stats7}")
        _ok(stats7["enabled"] is True, "stats 含 enabled")
        _ok(stats7["sampling_skipped"] >= 70, "采样丢弃计数正确（100 条应丢约 80）")
        disable_backtest_logging()
        stats7b = get_backtest_logging_stats()
        _ok(stats7b["enabled"] is False, "关闭后 stats 反映关闭状态")

    finally:
        disable_backtest_logging(timeout=5.0)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"\n[清理] 临时日志目录已清理: {tmp_dir}")

    print("\n" + "=" * 60)
    print(f"  自测结果: {ok_count[0]}/{total_count[0]} 项通过")
    if ok_count[0] == total_count[0]:
        print("  ✓ logger.py 回测日志模式 全部自测通过")
    else:
        print(f"  ✗ 有 {total_count[0] - ok_count[0]} 项未通过，请检查！")
    print("=" * 60)

