# 文件名：myquant/live/__init__.py
"""
live - 实盘交易模块
====================
实盘交易核心模块，协调实时行情、策略信号、券商执行、风控拦截。

模块结构:
  - engine.py      实盘引擎（协调数据/策略/执行/风控）
  - data_feed.py   实时行情接入（订阅/重连/分发/清洗）
  - portfolio.py   实时持仓管理（市值/可用冻结资金）
  - scheduler.py   定时任务/巡检/日报（任务22已完成）
  - state_store.py 状态持久化/崩溃恢复（任务23已完成）
  - brokers/       券商适配层（base_broker/paper_broker/真实券商）

调度/巡检/日报子模块导出（任务22）:
  - CronExpression   轻量 5 字段 cron 表达式（匹配/next_after）
  - ScheduledTask    调度任务数据类
  - LiveScheduler    定时任务调度器（interval/cron，后台守护线程）
  - HealthPatrol     健康巡检器（healthy/degraded/down）
  - DailyReport      每日报告生成器（Markdown + JSON 落盘）
  - attach_engine    实盘引擎便捷挂载（自动注册巡检+日报任务）
  - PATROL_HEALTHY / PATROL_DEGRADED / PATROL_DOWN  巡检健康状态常量

状态持久化/崩溃恢复子模块导出（任务23）:
  - StateStore       状态存储核心（版本管理/原子写/checksum校验/崩溃恢复）
  - StateSnapshot    状态快照数据类
  - capture_live_state  实盘状态采集（engine/portfolio/broker/scheduler）
  - restore_live_state  快照状态回写（持仓/资金重建）
  - attach_snapshot     定时快照挂载（与任务22调度器联动）
  - StateStoreError / SnapshotNotFoundError / SnapshotIntegrityError / SnapshotVersionError

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数，信号T日生成T+1成交
"""

from myquant.live.scheduler import (
    CronExpression,
    DailyReport,
    HealthPatrol,
    LiveScheduler,
    ScheduledTask,
    attach_engine,
    PATROL_DEGRADED,
    PATROL_DOWN,
    PATROL_HEALTHY,
)
from myquant.live.state_store import (
    StateSnapshot,
    StateStore,
    StateStoreError,
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotVersionError,
    attach_snapshot,
    capture_live_state,
    restore_live_state,
)

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

__all__ = [
    # 调度/巡检/日报（任务22）
    "CronExpression",
    "ScheduledTask",
    "LiveScheduler",
    "HealthPatrol",
    "DailyReport",
    "attach_engine",
    "PATROL_HEALTHY",
    "PATROL_DEGRADED",
    "PATROL_DOWN",
    # 状态持久化/崩溃恢复（任务23）
    "StateStore",
    "StateSnapshot",
    "capture_live_state",
    "restore_live_state",
    "attach_snapshot",
    "StateStoreError",
    "SnapshotNotFoundError",
    "SnapshotIntegrityError",
    "SnapshotVersionError",
]
