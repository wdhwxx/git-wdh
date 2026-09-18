# 文件名：myquant/utils/__init__.py
"""
utils - 通用工具包
==================
量化建模系统的底层地基，按领域拆分为：
  - time_utils  : 日期时间处理、时区转换、交易日历
  - file_utils  : 路径处理、文件读写、压缩解压
  - math_utils  : 数学统计、数值格式化、性能计算辅助
  - data_fingerprint : 数据指纹模块 (缓存失效、增量回测底层基础)
  - memory_watcher   : 内存水位监控器 (OOM防护、自动降级)
  - cache_sync       : 缓存同步管理器 (数据更新后自动更新指纹+失效缓存)
  - memory_pool      : 线程安全对象池 (事件对象复用、对象池线程安全)
  - pickle_checker   : pickle 序列化预检工具 (多进程任务提交前预检、提前抛出友好错误)
  - parallel         : 并行执行管理器 (I/O线程池+计算进程池分离、parallel_factor_context自动关闭L1缓存)
  - timer            : 分阶段计时器 (阶段耗时统计、耗时报告，回测/参数优化性能定位)
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

# ✅ 使用相对导入，避免 ModuleNotFoundError
from .time_utils import (
    TradingCalendar,
    now,
    today,
    to_timestamp,
    from_timestamp,
    tz_convert,
    is_trading_day,
    get_trading_days,
)
from .file_utils import (
    FileUtils,
    ensure_dir,
    read_json,
    write_json,
    read_csv,
    write_csv,
)
from .math_utils import (
    calc_sharpe_ratio,
    calc_max_drawdown,
    calc_annualized_return,
    fmt_number,
    fmt_percent,
)
from .data_fingerprint import (
    DataFingerprint,
    FingerprintStore,
    compute_fingerprint,
    compute_fingerprint_from_file,
    fingerprints_equal,
    save_fingerprint,
    load_fingerprint,
)
from .memory_watcher import (
    MemoryWatcher,
    MemoryLevel,
    get_memory_watcher,
    reset_memory_watcher,
    memory_guard,
    DEFAULT_WARNING_PCT,
    DEFAULT_CRITICAL_PCT,
)
from .cache_sync import (
    CacheSyncManager,
    SyncResult,
)
from .memory_pool import (
    ObjectPool,
    EventObjectPool,
    get_event_pool,
    reset_event_pool,
    acquire_event,
    release_event,
)
from .pickle_checker import (
    UnpicklableItem,
    PickleCheckResult,
    PickleCheckError,
    check_picklable,
    is_picklable,
    assert_picklable,
    pickle_guard,
    precheck_task,
    assert_task_picklable,
)
from .parallel import (
    ParallelManager,
    parallel_factor_context,
    run_io_parallel,
    run_cpu_parallel,
    get_parallel_manager,
    reset_parallel_manager,
)
from .timer import (
    StageTimer,
    format_duration,
    get_stage_timer,
    reset_stage_timers,
)
from .performance_config import (
    PerformanceConfig,
    apply_performance,
    resolve_engine_mode,
)

__all__ = [
    "TradingCalendar",
    "now",
    "today",
    "to_timestamp",
    "from_timestamp",
    "tz_convert",
    "is_trading_day",
    "get_trading_days",
    "FileUtils",
    "ensure_dir",
    "read_json",
    "write_json",
    "read_csv",
    "write_csv",
    "calc_sharpe_ratio",
    "calc_max_drawdown",
    "calc_annualized_return",
    "fmt_number",
    "fmt_percent",
    "DataFingerprint",
    "FingerprintStore",
    "compute_fingerprint",
    "compute_fingerprint_from_file",
    "fingerprints_equal",
    "save_fingerprint",
    "load_fingerprint",
    "MemoryWatcher",
    "MemoryLevel",
    "get_memory_watcher",
    "reset_memory_watcher",
    "memory_guard",
    "DEFAULT_WARNING_PCT",
    "DEFAULT_CRITICAL_PCT",
    "CacheSyncManager",
    "SyncResult",
    "ObjectPool",
    "EventObjectPool",
    "get_event_pool",
    "reset_event_pool",
    "acquire_event",
    "release_event",
    "UnpicklableItem",
    "PickleCheckResult",
    "PickleCheckError",
    "check_picklable",
    "is_picklable",
    "assert_picklable",
    "pickle_guard",
    "precheck_task",
    "assert_task_picklable",
    "ParallelManager",
    "parallel_factor_context",
    "run_io_parallel",
    "run_cpu_parallel",
    "get_parallel_manager",
    "reset_parallel_manager",
    "StageTimer",
    "format_duration",
    "get_stage_timer",
    "reset_stage_timers",
    "PerformanceConfig",
    "apply_performance",
    "resolve_engine_mode",
]
