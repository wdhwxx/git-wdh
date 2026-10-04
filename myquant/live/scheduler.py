# 文件名：myquant/live/scheduler.py
"""
scheduler.py - 定时任务 / 巡检 / 日报（任务22：实盘增强）
==========================================================
实盘自动化三件套：定时任务调度器 + 健康巡检 + 每日报告。

核心能力:
  1. 定时任务调度 — 支持固定间隔（interval）与 cron 表达式（cron）两种调度方式
  2. cron 表达式解析 — 轻量 5 字段（分/时/日/月/周），支持 * / */n / a,b,c / a-b
  3. 后台线程调度 — 守护线程逐 tick 检查到期任务，异常隔离，单任务失败不中断调度
  4. 任务生命周期 — 添加/删除/启停/暂停/恢复，手动立即执行
  5. 超时保护 — 单任务超时自动标记，不阻塞后续任务
  6. 健康巡检 — 检查引擎/行情源/券商/持仓/资金/风控/错误统计，输出 healthy/degraded/down
  7. 每日报告 — 汇总信号/订单/成交/持仓/资金/收益/健康状态，输出 Markdown + JSON 落盘
  8. 实盘便捷对接 — attach_engine() 一行挂载引擎，自动注册巡检 + 收盘日报任务

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（后台线程/任务可插拔/日报落盘）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只基于当前时钟判断调度，巡检/日报只读取已发生状态

防未来函数说明:
  - 调度触发仅依据当前系统时钟（now），判断任务是否到期，不做任何市场预测
  - next_run 由当前时间推算（interval 加固定秒 / cron 计算下一次匹配时刻），不引用未来行情
  - 巡检报告只读取引擎/行情源/券商/持仓的"当前已发生"状态
  - 日报只统计截至生成时刻已发生的信号/订单/成交/资金数据，日期取生成当日
  - 无任何预测方法、无硬编码未来日期

依赖: typing, time, threading, datetime, pathlib, myquant.core.logger
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("live.scheduler")

# 调度器默认配置
_DEFAULT_TICK_INTERVAL = 1.0      # 默认调度 tick（秒）
_DEFAULT_TASK_TIMEOUT = 60.0      # 默认单任务超时（秒）
_DEFAULT_PATROL_INTERVAL = 60     # 默认巡检间隔（秒）
_DEFAULT_DAILY_REPORT_CRON = "30 15 * * 1-5"  # 默认日报时间：周一~周五 15:30
# KP-P3-01：默认日终 T+1 结算时间（日报后执行，周一~周五 15:30 释放当日买入冻结）
_DEFAULT_T1_SETTLE_CRON = "30 15 * * 1-5"
# KP-P4-01：默认盘后账实对账时间（日报/T+1 结算之后，周一~周五 15:50）
_DEFAULT_RECONCILE_CRON = "50 15 * * 1-5"

# cron 扫描上限：覆盖 5 年（含 2 月 29 日等罕见组合）
_MAX_SCAN_MINUTES = 5 * 366 * 24 * 60


# ═══════════════════════════════════════════════
#  CronExpression - 轻量 5 字段 cron 表达式
# ═══════════════════════════════════════════════

class CronExpression:
    """
    轻量 5 字段 cron 表达式解析与匹配。

    字段顺序: 分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-7, 0和7均为周日)

    支持语法:
      *        通配（任意值）
      */n      每隔 n 个单位
      a,b,c    枚举列表
      a-b      区间
      a-b/n    区间内每隔 n 个单位

    防未来函数: 仅做纯时间匹配，不涉及任何市场数据或预测。

    使用方式:
        cron = CronExpression("30 15 * * 1-5")   # 周一~周五 15:30
        cron.match(datetime(2026, 8, 28, 15, 30))  # True
        cron.next_after(datetime(2026, 8, 28, 15, 31))  # 下一次触发时刻
    """

    # 各字段范围
    _RANGES = {
        "minute": (0, 59),
        "hour": (0, 23),
        "dom": (1, 31),
        "month": (1, 12),
        "dow": (0, 7),  # 0 和 7 均代表周日，解析后归一到 0-6
    }

    def __init__(self, expr: str):
        """
        解析 cron 表达式。

        Parameters
        ----------
        expr : str
            5 字段 cron 表达式，如 "30 15 * * 1-5"

        Raises
        ------
        ValueError : 表达式字段数不对或语法错误
        """
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError("cron 表达式不能为空")
        tokens = expr.split()
        if len(tokens) != 5:
            raise ValueError(f"cron 表达式必须为 5 字段，实际 {len(tokens)} 个: {expr!r}")

        self._expr = " ".join(tokens)

        self._minute = self._parse_field(tokens[0], *self._RANGES["minute"])
        self._hour = self._parse_field(tokens[1], *self._RANGES["hour"])
        self._dom = self._parse_field(tokens[2], *self._RANGES["dom"])
        self._month = self._parse_field(tokens[3], *self._RANGES["month"])
        dow = self._parse_field(tokens[4], *self._RANGES["dow"])
        # 归一周日：7 -> 0
        self._dow = {0 if v == 7 else v for v in dow}

        # 是否"受限"（非通配）：用于 day-of-month 与 day-of-week 的 POSIX OR 语义
        self._dom_restricted = len(self._dom) < (self._RANGES["dom"][1] - self._RANGES["dom"][0] + 1)
        self._dow_restricted = len(self._dow) < 7

        logger.debug(f"cron 表达式已解析: {self._expr}")

    @staticmethod
    def _parse_field(token: str, lo: int, hi: int) -> set:
        """解析单个字段 token 为允许值集合"""
        values: set = set()
        for part in token.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"cron 字段存在空项: {token!r}")
            if part == "*":
                values.update(range(lo, hi + 1))
            elif part.startswith("*/"):
                step = int(part[2:])
                if step <= 0:
                    raise ValueError(f"非法步长: {part!r}")
                values.update(range(lo, hi + 1, step))
            elif "/" in part:
                base, step_str = part.split("/", 1)
                step = int(step_str)
                if step <= 0:
                    raise ValueError(f"非法步长: {part!r}")
                if base == "*":
                    values.update(range(lo, hi + 1, step))
                elif "-" in base:
                    a_str, b_str = base.split("-", 1)
                    a, b = int(a_str), int(b_str)
                    if a < lo or b > hi:
                        raise ValueError(f"字段值越界 [{lo}-{hi}]: {part!r}")
                    values.update(range(a, b + 1, step))
                else:
                    a = int(base)
                    if a < lo or a > hi:
                        raise ValueError(f"字段值越界 [{lo}-{hi}]: {part!r}")
                    values.update(range(a, hi + 1, step))
            elif "-" in part:
                a_str, b_str = part.split("-", 1)
                a, b = int(a_str), int(b_str)
                if a < lo or b > hi:
                    raise ValueError(f"字段值越界 [{lo}-{hi}]: {part!r}")
                values.update(range(a, b + 1))
            else:
                v = int(part)
                if v < lo or v > hi:
                    raise ValueError(f"字段值越界 [{lo}-{hi}]: {part!r}")
                values.add(v)
        return values

    # ─────────────────────────────────────────
    #  属性
    # ─────────────────────────────────────────

    @property
    def expr(self) -> str:
        """原始表达式（规范化后的 5 字段）"""
        return self._expr

    @property
    def fields(self) -> Dict[str, set]:
        """各字段允许值集合（只读）"""
        return {
            "minute": set(self._minute),
            "hour": set(self._hour),
            "day_of_month": set(self._dom),
            "month": set(self._month),
            "day_of_week": set(self._dow),
        }

    # ─────────────────────────────────────────
    #  匹配
    # ─────────────────────────────────────────

    def match(self, dt: datetime) -> bool:
        """
        判断给定时刻是否匹配该 cron 表达式。

        POSIX 语义: 当日字段（日/周）均受限时取 OR，否则只约束受限者。

        Parameters
        ----------
        dt : datetime
            待判断时刻

        Returns
        -------
        bool : True=匹配
        """
        if dt.minute not in self._minute:
            return False
        if dt.hour not in self._hour:
            return False
        if dt.month not in self._month:
            return False

        # 周字段：datetime.weekday() 周一=0..周日=6 → cron 周日=0..周六=6
        cron_dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self._dom
        dow_ok = cron_dow in self._dow

        if self._dom_restricted and self._dow_restricted:
            # 两者均受限：命中任一即可（POSIX OR 语义）
            day_ok = dom_ok or dow_ok
        elif self._dom_restricted:
            day_ok = dom_ok
        elif self._dow_restricted:
            day_ok = dow_ok
        else:
            day_ok = True

        return day_ok

    def next_after(self, dt: datetime) -> Optional[datetime]:
        """
        计算 dt 之后的下一匹配时刻（严格大于 dt，按分钟粒度）。

        Parameters
        ----------
        dt : datetime
            起始时刻

        Returns
        -------
        datetime | None : 下一匹配时刻；扫描上限内未找到返回 None
        """
        candidate = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_MAX_SCAN_MINUTES):
            if self.match(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None

    def next_timestamp(self, ts: float) -> Optional[float]:
        """以时间戳为入参的 next_after 便捷封装"""
        nxt = self.next_after(datetime.fromtimestamp(ts))
        return nxt.timestamp() if nxt else None

    def __repr__(self) -> str:
        return f"CronExpression({self._expr!r})"


# ═══════════════════════════════════════════════
#  ScheduledTask - 调度任务
# ═══════════════════════════════════════════════

@dataclass
class ScheduledTask:
    """
    调度任务。

    schedule_type = "interval"：按固定间隔触发（interval_seconds）
    schedule_type = "cron"    ：按 cron 表达式触发（cron_expr）
    """
    task_id: str
    name: str
    func: Callable[..., Any]
    schedule_type: str                            # "interval" | "cron"
    interval_seconds: Optional[float] = None      # interval 任务间隔（秒）
    cron_expr: Optional[str] = None               # cron 任务表达式
    kwargs: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    run_once: bool = False                        # True：执行一次后自动禁用
    created_at: float = field(default_factory=time.time)
    last_run: Optional[float] = None              # 最近一次执行时间戳
    next_run: Optional[float] = None              # 下一次触发时间戳
    run_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    last_duration: Optional[float] = None         # 最近一次执行耗时（秒）
    last_result: Any = None

    def is_due(self, now: float) -> bool:
        """
        判断任务是否到期。

        - 禁用任务永不触发
        - next_run 为空（尚未排程）视为到期（由调度器立即排程）
        - now >= next_run 视为到期

        Parameters
        ----------
        now : float
            当前时间戳

        Returns
        -------
        bool : True=到期
        """
        if not self.enabled:
            return False
        if self.next_run is None:
            return True
        return now >= self.next_run

    def to_dict(self) -> Dict[str, Any]:
        """任务状态快照（JSON 安全）"""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "schedule_type": self.schedule_type,
            "interval_seconds": self.interval_seconds,
            "cron_expr": self.cron_expr,
            "enabled": self.enabled,
            "run_once": self.run_once,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_duration": self.last_duration,
        }

    def __repr__(self) -> str:
        return (
            f"ScheduledTask({self.task_id}, name={self.name!r}, "
            f"type={self.schedule_type}, enabled={self.enabled}, "
            f"run_count={self.run_count}, error_count={self.error_count})"
        )


# ═══════════════════════════════════════════════
#  LiveScheduler - 定时任务调度器
# ═══════════════════════════════════════════════

class LiveScheduler:
    """
    定时任务调度器（后台守护线程）。

    支持固定间隔与 cron 两种调度方式；任务异常隔离；超时保护；
    可独立运行，也可通过 attach_engine() 与实盘引擎便捷对接。

    使用方式:
        from myquant.live.scheduler import LiveScheduler

        scheduler = LiveScheduler(tick_interval=1.0)
        scheduler.add_interval_task(my_func, name="巡检", interval_seconds=60)
        scheduler.add_cron_task(my_report, name="日报", cron_expr="30 15 * * 1-5")
        scheduler.start()

        # 手动触发一次
        scheduler.run_task_now("巡检")

        scheduler.stop()
    """

    def __init__(
        self,
        tick_interval: float = _DEFAULT_TICK_INTERVAL,
        task_timeout: float = _DEFAULT_TASK_TIMEOUT,
    ):
        """
        初始化调度器。

        Parameters
        ----------
        tick_interval : float
            调度循环 tick 间隔（秒），默认 1.0
        task_timeout : float
            单任务执行超时（秒），超时标记为 TimeoutError 并继续；<=0 表示不启用
        """
        self._tick_interval = float(tick_interval)
        self._task_timeout = float(task_timeout)

        self._tasks: Dict[str, ScheduledTask] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 调度器状态
        self._running = False
        self._paused = False
        self._start_time: Optional[float] = None
        self._total_dispatch = 0
        self._total_errors = 0

        logger.info(
            f"调度器初始化: tick={tick_interval}s, "
            f"任务超时={'启用(' + str(task_timeout) + 's)' if task_timeout > 0 else '未启用'}"
        )

    # ═══════════════════════════════════════════
    #  生命周期管理
    # ═══════════════════════════════════════════

    def start(self) -> bool:
        """
        启动调度线程。

        Returns
        -------
        bool : True=启动成功
        """
        if self._running:
            logger.warning("[scheduler] 调度器已在运行中")
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="live-scheduler",
            daemon=True,
        )
        self._thread.start()
        self._running = True
        self._paused = False
        self._start_time = time.time()
        logger.info(
            f"[scheduler] 调度器已启动: 任务数={len(self._tasks)}, "
            f"tick={self._tick_interval}s"
        )
        return True

    def stop(self) -> None:
        """停止调度线程（等待当前 tick 结束）。"""
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(2.0, self._tick_interval + 2.0))
        self._thread = None
        logger.info(
            f"[scheduler] 调度器已停止: 累计派发={self._total_dispatch}, "
            f"累计错误={self._total_errors}"
        )

    def pause(self) -> None:
        """暂停调度（循环继续，但不再派发到期任务）。"""
        self._paused = True
        logger.info("[scheduler] 调度器已暂停")

    def resume(self) -> None:
        """恢复调度。"""
        self._paused = False
        logger.info("[scheduler] 调度器已恢复")

    @property
    def is_running(self) -> bool:
        """是否运行中"""
        return self._running

    @property
    def is_paused(self) -> bool:
        """是否已暂停"""
        return self._paused

    # ═══════════════════════════════════════════
    #  任务注册
    # ═══════════════════════════════════════════

    def add_interval_task(
        self,
        func: Callable[..., Any],
        name: str,
        interval_seconds: float,
        kwargs: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
        run_once: bool = False,
        task_id: Optional[str] = None,
        run_immediately: bool = True,
    ) -> str:
        """
        添加固定间隔任务。

        Parameters
        ----------
        func : Callable
            任务函数
        name : str
            任务名称
        interval_seconds : float
            执行间隔（秒），必须 > 0
        kwargs : dict | None
            传递给任务函数的参数
        enabled : bool
            是否启用（默认 True）
        run_once : bool
            是否只执行一次后自动禁用（默认 False）
        task_id : str | None
            自定义任务 ID（默认自动生成）
        run_immediately : bool
            首次是否立即到期（默认 True，启动后首个 tick 即执行）

        Returns
        -------
        str : 任务 ID
        """
        if interval_seconds is None or interval_seconds <= 0:
            raise ValueError(f"interval_seconds 必须 > 0，实际 {interval_seconds!r}")

        tid = self._next_task_id(task_id, name)
        task = ScheduledTask(
            task_id=tid,
            name=name,
            func=func,
            schedule_type="interval",
            interval_seconds=float(interval_seconds),
            kwargs=dict(kwargs or {}),
            enabled=enabled,
            run_once=run_once,
            next_run=time.time() if run_immediately else time.time() + float(interval_seconds),
        )
        with self._lock:
            if tid in self._tasks:
                raise ValueError(f"任务 ID 已存在: {tid}")
            self._tasks[tid] = task
        logger.info(f"[scheduler] 已添加间隔任务: {tid} name={name} interval={interval_seconds}s")
        return tid

    def add_cron_task(
        self,
        func: Callable[..., Any],
        name: str,
        cron_expr: str,
        kwargs: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
        run_once: bool = False,
        task_id: Optional[str] = None,
    ) -> str:
        """
        添加 cron 定时任务。

        Parameters
        ----------
        func : Callable
            任务函数
        name : str
            任务名称
        cron_expr : str
            5 字段 cron 表达式
        kwargs : dict | None
            传递给任务函数的参数
        enabled : bool
            是否启用（默认 True）
        run_once : bool
            是否只执行一次后自动禁用（默认 False）
        task_id : str | None
            自定义任务 ID（默认自动生成）

        Returns
        -------
        str : 任务 ID
        """
        cron = CronExpression(cron_expr)  # 提前校验语法，非法立即抛错
        tid = self._next_task_id(task_id, name)
        task = ScheduledTask(
            task_id=tid,
            name=name,
            func=func,
            schedule_type="cron",
            cron_expr=cron.expr,
            kwargs=dict(kwargs or {}),
            enabled=enabled,
            run_once=run_once,
            next_run=cron.next_timestamp(time.time()),
        )
        with self._lock:
            if tid in self._tasks:
                raise ValueError(f"任务 ID 已存在: {tid}")
            self._tasks[tid] = task
        logger.info(f"[scheduler] 已添加 cron 任务: {tid} name={name} expr={cron.expr}")
        return tid

    def remove_task(self, task_id: str) -> bool:
        """
        删除任务。

        Parameters
        ----------
        task_id : str
            任务 ID

        Returns
        -------
        bool : True=删除成功
        """
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                logger.info(f"[scheduler] 已删除任务: {task_id}")
                return True
        logger.warning(f"[scheduler] 任务不存在: {task_id}")
        return False

    def enable_task(self, task_id: str) -> bool:
        """启用任务（重新排程 next_run）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.enabled = True
            if task.next_run is None:
                task.next_run = self._compute_next_run(task, time.time())
        return True

    def disable_task(self, task_id: str) -> bool:
        """禁用任务（不再触发，保留记录）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.enabled = False
        return True

    # ═══════════════════════════════════════════
    #  任务执行
    # ═══════════════════════════════════════════

    def run_task_now(self, task_id: str) -> bool:
        """
        立即手动执行任务（无论是否到期/启用）。

        Parameters
        ----------
        task_id : str
            任务 ID

        Returns
        -------
        bool : True=已派发执行
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning(f"[scheduler] 任务不存在，无法手动执行: {task_id}")
                return False
        try:
            self._execute_task(task, time.time())
            return True
        except Exception as e:
            logger.error(f"[scheduler] 手动执行任务异常 {task_id}: {e}")
            self._total_errors += 1
            return False

    def run_due_tasks(self, now: Optional[float] = None) -> int:
        """
        派发所有到期任务（单次扫描）。

        在锁内"认领"到期任务（置 next_run 为无限远防重复派发），
        释放锁后逐个执行，执行完毕再重排 next_run。
        单任务异常被隔离，不影响其他任务。

        Parameters
        ----------
        now : float | None
            当前时间戳（None 取 time.time()）

        Returns
        -------
        int : 本次派发任务数
        """
        if self._paused:
            return 0
        now = time.time() if now is None else now

        due: List[ScheduledTask] = []
        with self._lock:
            for task in self._tasks.values():
                if task.is_due(now):
                    # 认领：置为无限远，防止本 tick 内重复派发
                    task.next_run = float("inf")
                    due.append(task)
                    self._total_dispatch += 1

        for task in due:
            try:
                self._execute_task(task, now)
            except Exception as e:
                self._total_errors += 1
                logger.error(f"[scheduler] 任务执行失败 {task.task_id} ({task.name}): {e}")
                # _execute_task 内部已记录错误，这里兜底计数
        return len(due)

    def _run_loop(self) -> None:
        """后台调度循环：逐 tick 检查并派发到期任务。"""
        logger.debug("[scheduler] 调度循环已启动")
        while not self._stop_event.is_set():
            try:
                self.run_due_tasks()
            except Exception as e:
                # 调度循环本身异常不应导致线程退出
                self._total_errors += 1
                logger.error(f"[scheduler] 调度循环异常: {e}")
            self._stop_event.wait(self._tick_interval)

    def _execute_task(self, task: ScheduledTask, now: float) -> None:
        """
        执行单个任务（含超时保护、异常隔离、结果统计、重排 next_run）。

        执行前先置 next_run 为无限远（认领），finally 中重排，
        保证即使执行异常也不会反复触发。
        """
        task.last_run = now
        task.next_run = float("inf")  # 认领，防重复派发
        start = time.monotonic()
        try:
            if self._task_timeout and self._task_timeout > 0:
                result = self._run_with_timeout(task.func, task.kwargs, self._task_timeout, task)
            else:
                result = task.func(**task.kwargs)
            task.last_result = result
            task.last_error = None
        except Exception as e:
            task.error_count += 1
            task.last_error = f"{type(e).__name__}: {e}"
            self._total_errors += 1
            logger.error(f"[scheduler] 任务异常 {task.task_id} ({task.name}): {e}")
        finally:
            task.run_count += 1
            task.last_duration = time.monotonic() - start
            # 重排下一次触发
            task.next_run = self._compute_next_run(task, time.time())
            if task.run_once and task.run_count >= 1:
                task.enabled = False
                logger.info(f"[scheduler] 任务已按 run_once 自动禁用: {task.task_id} ({task.name})")

    @staticmethod
    def _run_with_timeout(
        func: Callable[..., Any],
        kwargs: Dict[str, Any],
        timeout: float,
        task: ScheduledTask,
    ) -> Any:
        """
        在线程中执行任务并等待结果，超时抛出 TimeoutError。

        守护线程 + join(timeout)：超时后无法强制终止线程，但立即返回并标记，
        避免阻塞调度循环。
        """
        box: Dict[str, Any] = {}

        def _runner() -> None:
            try:
                box["result"] = func(**kwargs)
            except Exception as e:  # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=_runner, name=f"task-{task.task_id}", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"任务 {task.task_id} ({task.name}) 执行超时（>{timeout}s）")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _compute_next_run(self, task: ScheduledTask, now: float) -> Optional[float]:
        """
        计算任务下一次触发时间（仅基于当前时间，不引用未来数据）。

        - interval 任务：now + interval
        - cron 任务：cron.next_timestamp(now)
        """
        if task.schedule_type == "interval":
            return now + float(task.interval_seconds)
        if task.schedule_type == "cron":
            cron = CronExpression(task.cron_expr)
            return cron.next_timestamp(now)
        return None

    def _next_task_id(self, task_id: Optional[str], name: str) -> str:
        """生成唯一任务 ID（指定或自动生成）。"""
        if task_id:
            return task_id
        base = name or "task"
        with self._lock:
            i = 1
            while f"{base}-{i}" in self._tasks:
                i += 1
            return f"{base}-{i}"

    # ═══════════════════════════════════════════
    #  查询
    # ═══════════════════════════════════════════

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """获取任务对象"""
        with self._lock:
            return self._tasks.get(task_id)

    def get_tasks(self) -> List[ScheduledTask]:
        """获取全部任务（按注册顺序）"""
        with self._lock:
            return list(self._tasks.values())

    def get_summary(self) -> Dict[str, Any]:
        """任务总览（JSON 安全）"""
        with self._lock:
            tasks = [t.to_dict() for t in self._tasks.values()]
            enabled = sum(1 for t in self._tasks.values() if t.enabled)
        return {
            "running": self._running,
            "paused": self._paused,
            "total_tasks": len(tasks),
            "enabled_tasks": enabled,
            "total_dispatch": self._total_dispatch,
            "total_errors": self._total_errors,
            "tasks": tasks,
        }

    def get_status(self) -> Dict[str, Any]:
        """调度器状态快照"""
        return {
            "running": self._running,
            "paused": self._paused,
            "tick_interval": self._tick_interval,
            "task_timeout": self._task_timeout,
            "start_time": self._start_time,
            "uptime_seconds": (time.time() - self._start_time) if self._start_time else 0.0,
            "total_dispatch": self._total_dispatch,
            "total_errors": self._total_errors,
        }

    def __repr__(self) -> str:
        return (
            f"LiveScheduler(running={self._running}, paused={self._paused}, "
            f"tasks={len(self._tasks)})"
        )


# ═══════════════════════════════════════════════
#  HealthPatrol - 健康巡检
# ═══════════════════════════════════════════════

# 巡检健康状态
PATROL_HEALTHY = "healthy"
PATROL_DEGRADED = "degraded"
PATROL_DOWN = "down"


class HealthPatrol:
    """
    健康巡检器。

    对引擎 / 行情源 / 券商 / 持仓 / 资金 / 风控 / 错误统计等做逐项健康检查，
    输出每项 healthy / degraded / down 与汇总健康状态。
    全部检查项均可通过 add_check() 扩展；内置检查基于 duck-typing，
    组件缺失时该项标记 down 并注明"未接入"，不影响整体判定逻辑。

    防未来函数: 只读取各组件"当前已发生"的状态，不涉及任何预测。

    使用方式:
        from myquant.live.scheduler import HealthPatrol

        patrol = HealthPatrol()
        report = patrol.build_report(engine=engine, portfolio=portfolio)
        # report["status"] == "healthy" | "degraded" | "down"
    """

    def __init__(self):
        self._extra_checks: Dict[str, Callable[[], Any]] = {}
        logger.debug("巡检器初始化")

    def add_check(self, name: str, func: Callable[[], Any]) -> None:
        """
        注册自定义检查项。

        Parameters
        ----------
        name : str
            检查项名称
        func : Callable[[], Any]
            无参函数，返回 (status, detail) 或 status 或 detail dict
            - status 为 "healthy" / "degraded" / "down"
            - detail 为任意可 JSON 化的补充信息
        """
        if not name or not callable(func):
            raise ValueError("检查项必须提供非空 name 与可调用 func")
        self._extra_checks[name] = func

    def remove_check(self, name: str) -> bool:
        """移除自定义检查项"""
        return self._extra_checks.pop(name, None) is not None

    # ─────────────────────────────────────────
    #  内置检查项
    # ─────────────────────────────────────────

    @staticmethod
    def _check_engine(engine: Any) -> tuple:
        """引擎检查：状态 / 运行时长 / 信号 / 成交 / 错误"""
        if engine is None:
            return PATROL_DOWN, {"reason": "引擎未接入"}
        try:
            status_obj = engine.get_status()
            state = status_obj.get("state", "UNKNOWN")
            stats = status_obj.get("stats", {})
            errors = stats.get("errors", 0)
            if state == "ERROR":
                return PATROL_DOWN, {"state": state, "errors": errors}
            if state != "RUNNING":
                return PATROL_DEGRADED, {"state": state, "reason": "引擎未在运行", "errors": errors}
            if errors > 0:
                return PATROL_DEGRADED, {
                    "state": state, "errors": errors,
                    "signals": stats.get("total_signals", 0),
                    "trades": stats.get("total_trades", 0),
                }
            return PATROL_HEALTHY, {
                "state": state,
                "signals": stats.get("total_signals", 0),
                "trades": stats.get("total_trades", 0),
                "orders": stats.get("total_orders", 0),
                "errors": errors,
            }
        except Exception as e:  # noqa: BLE001
            return PATROL_DOWN, {"error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _check_data_feed(data_feed: Any) -> tuple:
        """行情源检查：连接 / 运行 / 订阅数"""
        if data_feed is None:
            return PATROL_DOWN, {"reason": "行情源未接入"}
        try:
            connected = bool(data_feed.is_connected()) if hasattr(data_feed, "is_connected") else True
            running = bool(data_feed.is_running()) if hasattr(data_feed, "is_running") else None
            symbols = []
            if hasattr(data_feed, "get_subscribed_symbols"):
                symbols = data_feed.get_subscribed_symbols()
            if not connected:
                return PATROL_DOWN, {"connected": connected, "reason": "行情源未连接"}
            if running is False:
                return PATROL_DEGRADED, {"connected": connected, "running": running, "reason": "行情源未在轮询"}
            detail = {"connected": connected, "subscribed": len(symbols), "symbols": symbols}
            if running is not None:
                detail["running"] = running
            return PATROL_HEALTHY, detail
        except Exception as e:  # noqa: BLE001
            return PATROL_DOWN, {"error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _check_broker(broker: Any) -> tuple:
        """券商检查：连接状态"""
        if broker is None:
            return PATROL_DOWN, {"reason": "券商未接入"}
        try:
            if hasattr(broker, "is_connected"):
                connected = bool(broker.is_connected)
            else:
                connected = bool(broker.is_connected()) if callable(getattr(broker, "is_connected", None)) else True
            if not connected:
                return PATROL_DOWN, {"connected": connected, "reason": "券商未连接"}
            return PATROL_HEALTHY, {"connected": connected}
        except Exception as e:  # noqa: BLE001
            return PATROL_DOWN, {"error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _check_portfolio(portfolio: Any) -> tuple:
        """持仓/资金检查：账户总览"""
        if portfolio is None:
            return PATROL_DOWN, {"reason": "持仓管理未接入"}
        try:
            account = portfolio.get_account()
            cash = account.get("available_cash", 0.0)
            if cash < 0:
                return PATROL_DEGRADED, {"reason": "可用资金为负", "account": account}
            return PATROL_HEALTHY, {"account": account}
        except Exception as e:  # noqa: BLE001
            return PATROL_DOWN, {"error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _check_risk(risk_manager: Any) -> tuple:
        """风控检查：风控状态与健康状态"""
        if risk_manager is None:
            return PATROL_DOWN, {"reason": "风控未接入"}
        try:
            if hasattr(risk_manager, "get_summary"):
                summary = risk_manager.get_summary()
                status_map = {
                    "NORMAL": PATROL_HEALTHY,
                    "WARNING": PATROL_DEGRADED,
                    "PAUSED": PATROL_DOWN,
                }
                risk_state = summary.get("state") or summary.get("status", "NORMAL")
                status = status_map.get(risk_state, PATROL_DEGRADED)
                return status, {"risk_state": risk_state, "summary": summary}
            return PATROL_HEALTHY, {"note": "风控无 get_summary，跳过详细检查"}
        except Exception as e:  # noqa: BLE001
            return PATROL_DOWN, {"error": f"{type(e).__name__}: {e}"}

    # ─────────────────────────────────────────
    #  报告构建
    # ─────────────────────────────────────────

    def build_report(
        self,
        engine: Any = None,
        data_feed: Any = None,
        broker: Any = None,
        portfolio: Any = None,
        risk_manager: Any = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        构建巡检报告。

        Parameters
        ----------
        engine : LiveEngine | None
            实盘引擎
        data_feed : MarketDataFeed | None
            行情源
        broker : BaseBroker | None
            券商
        portfolio : LivePortfolio | None
            持仓管理
        risk_manager : RiskManager | None
            风控管理器
        now : float | None
            巡检时间戳（None 取当前时间）

        Returns
        -------
        Dict[str, Any] :
            {
                "timestamp": float,
                "status": "healthy" | "degraded" | "down",
                "checks": {名称: {"status": ..., "detail": ...}},
            }
        """
        ts = time.time() if now is None else now
        checks: Dict[str, Dict[str, Any]] = {}

        # 内置检查（带组件参数）
        builtin = {
            "engine": (self._check_engine, engine),
            "data_feed": (self._check_data_feed, data_feed),
            "broker": (self._check_broker, broker),
            "portfolio": (self._check_portfolio, portfolio),
            "risk": (self._check_risk, risk_manager),
        }
        for name, (func, comp) in builtin.items():
            try:
                result = func(comp)
            except Exception as e:  # noqa: BLE001
                result = (PATROL_DOWN, {"error": f"{type(e).__name__}: {e}"})
            status, detail = self._normalize_result(result, name)
            checks[name] = {"status": status, "detail": detail}

        # 自定义检查（无参函数，闭包携带数据）
        for name, func in self._extra_checks.items():
            try:
                result = func()
            except Exception as e:  # noqa: BLE001
                result = (PATROL_DOWN, {"error": f"{type(e).__name__}: {e}"})
            status, detail = self._normalize_result(result, name)
            checks[name] = {"status": status, "detail": detail}

        # 汇总：down 优先，其次 degraded，否则 healthy
        status = self.evaluate_status(checks)
        report = {
            "timestamp": ts,
            "status": status,
            "checks": checks,
        }
        logger.info(
            f"[patrol] 巡检完成: status={status}, "
            f"healthy={self._count(checks, PATROL_HEALTHY)}, "
            f"degraded={self._count(checks, PATROL_DEGRADED)}, "
            f"down={self._count(checks, PATROL_DOWN)}"
        )
        return report

    def run(self, **kwargs) -> Dict[str, Any]:
        """build_report 的别名，便于作为调度任务直接注册。"""
        return self.build_report(**kwargs)

    # ─────────────────────────────────────────
    #  辅助
    # ─────────────────────────────────────────

    @staticmethod
    def _normalize_result(result: Any, name: str) -> tuple:
        """
        将检查函数返回值规范化为 (status, detail)。

        支持三种返回形态:
          - (status, detail) 二元组
          - status 字符串
          - {"status": ..., ...} 字典
        """
        if isinstance(result, tuple) and len(result) == 2:
            status, detail = result
        elif isinstance(result, str):
            status, detail = result, {}
        elif isinstance(result, dict):
            status = result.get("status", PATROL_DEGRADED)
            detail = result
        else:
            status, detail = PATROL_DEGRADED, {"raw": result}
        if status not in (PATROL_HEALTHY, PATROL_DEGRADED, PATROL_DOWN):
            logger.warning(f"[patrol] 检查项 {name} 返回非法状态 {status!r}，降级为 degraded")
            status = PATROL_DEGRADED
        return status, detail

    @staticmethod
    def evaluate_status(checks: Dict[str, Dict[str, Any]]) -> str:
        """
        汇总多项检查为单一健康状态。

        任一 down → down；任一 degraded → degraded；否则 healthy。
        """
        statuses = [c.get("status", PATROL_DEGRADED) for c in checks.values()]
        if any(s == PATROL_DOWN for s in statuses):
            return PATROL_DOWN
        if any(s == PATROL_DEGRADED for s in statuses):
            return PATROL_DEGRADED
        return PATROL_HEALTHY

    @staticmethod
    def _count(checks: Dict[str, Dict[str, Any]], status: str) -> int:
        return sum(1 for c in checks.values() if c.get("status") == status)

    def __repr__(self) -> str:
        return f"HealthPatrol(extra_checks={list(self._extra_checks)})"


# ═══════════════════════════════════════════════
#  DailyReport - 每日报告
# ═══════════════════════════════════════════════

class DailyReport:
    """
    每日报告生成器。

    汇总截至生成时刻已发生的引擎状态 / 信号 / 订单 / 成交 / 持仓 / 资金 / 收益 / 健康状态，
    输出为 dict（JSON 安全），并可生成 Markdown 与 JSON 落盘到 outputs/daily_reports/。

    防未来函数: 只统计"已发生"数据；报告日期取生成当日（可指定）；不引用任何未来行情。

    使用方式:
        from myquant.live.scheduler import DailyReport

        reporter = DailyReport()
        report = reporter.build(engine=engine, portfolio=portfolio)
        md = reporter.to_markdown(report)
        path = reporter.save(report)  # 同时落盘 .md 与 .json
    """

    def __init__(self, output_dir: Optional[Path] = None):
        """
        初始化日报生成器。

        Parameters
        ----------
        output_dir : Path | None
            报告输出目录（默认 项目根/outputs/daily_reports）
        """
        if output_dir is None:
            output_dir = Path(__file__).resolve().parent.parent.parent / "outputs" / "daily_reports"
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"日报生成器初始化: 输出目录={self._output_dir}")

    @property
    def output_dir(self) -> Path:
        """报告输出目录"""
        return self._output_dir

    # ─────────────────────────────────────────
    #  报告构建
    # ─────────────────────────────────────────

    def build(
        self,
        engine: Any = None,
        portfolio: Any = None,
        broker: Any = None,
        data_feed: Any = None,
        risk_manager: Any = None,
        date: Optional[str] = None,
        generated_at: Optional[float] = None,
        patrol: Optional[HealthPatrol] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        构建每日报告。

        Parameters
        ----------
        engine : LiveEngine | None
            实盘引擎（提供状态与统计）
        portfolio : LivePortfolio | None
            持仓管理（提供账户与持仓）
        broker : BaseBroker | None
            券商（提供账户兜底）
        data_feed : MarketDataFeed | None
            行情源
        risk_manager : RiskManager | None
            风控管理器
        date : str | None
            报告日期（YYYY-MM-DD，默认生成当日）
        generated_at : float | None
            生成时间戳（None 取当前时间）
        patrol : HealthPatrol | None
            巡检器（默认新建，用于嵌入健康状态）
        extra : dict | None
            额外补充字段（自定义扩展）

        Returns
        -------
        Dict[str, Any] :
            {
                "report_date": str,
                "generated_at": float,
                "engine": {...},
                "account": {...},
                "positions": [...],
                "patrol": {...},
                "summary": {...},
            }
        """
        ts = time.time() if generated_at is None else generated_at
        report_date = date or datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

        # 引擎状态与统计
        engine_section: Dict[str, Any] = {}
        if engine is not None:
            try:
                status_obj = engine.get_status()
                engine_section = {
                    "engine_id": status_obj.get("engine_id"),
                    "state": status_obj.get("state"),
                    "is_running": status_obj.get("is_running"),
                    "run_seconds": round(status_obj.get("run_seconds", 0), 2),
                    "strategies": status_obj.get("strategies", []),
                    "watch_symbols": status_obj.get("watch_symbols", []),
                    "stats": status_obj.get("stats", {}),
                }
            except Exception as e:  # noqa: BLE001
                engine_section = {"error": f"{type(e).__name__}: {e}"}

        # 账户与持仓
        account: Dict[str, Any] = {}
        positions: List[Dict[str, Any]] = []
        try:
            if portfolio is not None and hasattr(portfolio, "get_account"):
                account = portfolio.get_account()
                for pos in portfolio.get_all_positions():
                    positions.append(self._position_to_dict(pos))
            elif broker is not None and hasattr(broker, "get_account"):
                account = broker.get_account()
                for pos in broker.get_positions():
                    positions.append(self._position_to_dict(pos))
        except Exception as e:  # noqa: BLE001
            account = {"error": f"{type(e).__name__}: {e}"}

        # 健康巡检
        patrol_obj = patrol or HealthPatrol()
        patrol_report = patrol_obj.build_report(
            engine=engine,
            data_feed=data_feed,
            broker=broker,
            portfolio=portfolio,
            risk_manager=risk_manager,
            now=ts,
        )

        # 汇总
        summary = self._build_summary(engine_section, account, positions, patrol_report)

        report: Dict[str, Any] = {
            "report_date": report_date,
            "generated_at": ts,
            "engine": engine_section,
            "account": account,
            "positions": positions,
            "patrol": patrol_report,
            "summary": summary,
        }
        if extra:
            report["extra"] = dict(extra)

        return report

    @staticmethod
    def _position_to_dict(pos: Any) -> Dict[str, Any]:
        """
        Position 对象 → JSON 安全 dict。

        兼容真实 myquant.models.trade.Position（avg_cost / cost_value / market_value 属性）
        与通用 duck 对象（avg_price / current_price / cost），字段缺失补默认。
        """
        g = lambda name, default=None: getattr(pos, name, default)  # noqa: E731
        quantity = g("quantity", 0)
        avg_price = g("avg_cost", g("avg_price", 0.0))
        market_price = g("market_price", g("current_price", 0.0))
        market_value = g("market_value", quantity * market_price)
        cost = g("cost_value", g("cost", quantity * avg_price))
        unrealized = g("unrealized_pnl", market_value - cost)
        return {
            "symbol": g("symbol"),
            "quantity": quantity,
            "avg_price": avg_price,
            "current_price": market_price,
            "market_value": market_value,
            "cost": cost,
            "unrealized_pnl": unrealized,
            "realized_pnl": g("realized_pnl", 0.0),
            "pnl_ratio": g("pnl_ratio", 0.0),
        }

    @staticmethod
    def _build_summary(
        engine_section: Dict[str, Any],
        account: Dict[str, Any],
        positions: List[Dict[str, Any]],
        patrol_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        """汇总核心指标（只汇总已存在字段，缺失不编造）。"""
        summary: Dict[str, Any] = {
            "position_count": len(positions),
            "patrol_status": patrol_report.get("status", "unknown"),
        }
        # 资金/收益（优先取 account）
        for key in ("total_assets", "equity", "total_pnl", "total_pnl_ratio",
                    "cash", "available_cash", "market_value", "realized_pnl", "unrealized_pnl"):
            if key in account:
                summary[key] = account[key]
        # 交易统计（取引擎 stats）
        stats = engine_section.get("stats", {})
        for key in ("total_bars", "total_signals", "buy_signals", "sell_signals",
                    "total_orders", "filled_orders", "rejected_orders",
                    "total_trades", "risk_rejected", "errors"):
            if key in stats:
                summary[key] = stats[key]
        return summary

    # ─────────────────────────────────────────
    #  输出
    # ─────────────────────────────────────────

    def to_markdown(self, report: Dict[str, Any]) -> str:
        """
        将报告转换为 Markdown 文本。

        Parameters
        ----------
        report : Dict
            build() 的返回值

        Returns
        -------
        str : Markdown 文本
        """
        lines: List[str] = []
        lines.append("# 实盘每日报告")
        lines.append("")
        lines.append(f"- **报告日期**：{report.get('report_date', '-')}")
        lines.append(f"- **生成时间**：{datetime.fromtimestamp(report.get('generated_at', 0)).strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        # 健康状态
        patrol = report.get("patrol", {})
        status = patrol.get("status", "unknown")
        lines.append(f"## 健康状态：{status}")
        checks = patrol.get("checks", {})
        if checks:
            lines.append("")
            lines.append("| 检查项 | 状态 | 说明 |")
            lines.append("| --- | --- | --- |")
            for name, c in checks.items():
                detail = c.get("detail", {})
                desc = self._detail_to_text(detail)
                lines.append(f"| {name} | {c.get('status', 'unknown')} | {desc} |")
        lines.append("")

        # 账户
        account = report.get("account", {})
        lines.append("## 账户概览")
        if account and "error" not in account:
            lines.append("")
            lines.append("| 指标 | 数值 |")
            lines.append("| --- | --- |")
            for k, v in account.items():
                lines.append(f"| {k} | {self._fmt(v)} |")
        else:
            lines.append("")
            lines.append(f"- 账户数据不可用：{account.get('error', '未接入')}")
        lines.append("")

        # 持仓
        positions = report.get("positions", [])
        lines.append(f"## 持仓明细（{len(positions)}）")
        if positions:
            lines.append("")
            lines.append("| 标的 | 数量 | 成本价 | 现价 | 市值 | 浮动盈亏 | 收益率 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for p in positions:
                lines.append(
                    f"| {p.get('symbol', '-')} | {self._fmt(p.get('quantity', 0))} "
                    f"| {self._fmt(p.get('avg_price', 0))} | {self._fmt(p.get('current_price', 0))} "
                    f"| {self._fmt(p.get('market_value', 0))} | {self._fmt(p.get('unrealized_pnl', 0))} "
                    f"| {self._fmt(p.get('pnl_ratio', 0))} |"
                )
        else:
            lines.append("")
            lines.append("- 无持仓")
        lines.append("")

        # 引擎统计
        engine = report.get("engine", {})
        stats = engine.get("stats", {})
        if stats:
            lines.append("## 引擎统计")
            lines.append("")
            lines.append("| 指标 | 数值 |")
            lines.append("| --- | --- |")
            for k, v in stats.items():
                lines.append(f"| {k} | {self._fmt(v)} |")
            lines.append("")

        # 汇总
        summary = report.get("summary", {})
        lines.append("## 汇总")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        for k, v in summary.items():
            lines.append(f"| {k} | {self._fmt(v)} |")
        lines.append("")

        return "\n".join(lines)

    def save(
        self,
        report: Dict[str, Any],
        output_dir: Optional[Path] = None,
    ) -> Path:
        """
        保存报告（同时落盘 Markdown 与 JSON）。

        Parameters
        ----------
        report : Dict
            build() 的返回值
        output_dir : Path | None
            输出目录（默认初始化时目录）

        Returns
        -------
        Path : 保存的 Markdown 文件路径
        """
        out_dir = Path(output_dir) if output_dir is not None else self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        date = report.get("report_date", datetime.now().strftime("%Y-%m-%d"))

        md_path = out_dir / f"{date}_日报.md"
        md_path.write_text(self.to_markdown(report), encoding="utf-8")

        json_path = out_dir / f"{date}_日报.json"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        logger.info(f"[report] 日报已保存: {md_path.name} + {json_path.name}")
        return md_path

    def build_and_save(
        self,
        engine: Any = None,
        portfolio: Any = None,
        broker: Any = None,
        data_feed: Any = None,
        risk_manager: Any = None,
        date: Optional[str] = None,
        output_dir: Optional[Path] = None,
    ) -> Path:
        """
        构建并保存日报（适合直接注册为调度任务）。

        Parameters
        ----------
        engine / portfolio / broker / data_feed / risk_manager : 同 build()
        date : str | None
            报告日期
        output_dir : Path | None
            输出目录

        Returns
        -------
        Path : 保存的 Markdown 文件路径
        """
        report = self.build(
            engine=engine,
            portfolio=portfolio,
            broker=broker,
            data_feed=data_feed,
            risk_manager=risk_manager,
            date=date,
        )
        return self.save(report, output_dir=output_dir)

    # ─────────────────────────────────────────
    #  辅助
    # ─────────────────────────────────────────

    @staticmethod
    def _fmt(v: Any) -> str:
        """数值格式化（保留 2 位小数），非数值原样返回。"""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f"{v:,.2f}" if abs(v) >= 1000 else f"{v:.2f}"
        return str(v)

    @staticmethod
    def _detail_to_text(detail: Any) -> str:
        """检查项 detail 转单行说明文本。"""
        if isinstance(detail, dict):
            if "error" in detail:
                return detail["error"]
            if "reason" in detail:
                return str(detail["reason"])
            return ", ".join(f"{k}={v}" for k, v in detail.items())
        return str(detail)

    def __repr__(self) -> str:
        return f"DailyReport(output_dir={str(self._output_dir)!r})"


# ═══════════════════════════════════════════════
#  attach_engine - 实盘便捷对接
# ═══════════════════════════════════════════════

def attach_engine(
    scheduler: LiveScheduler,
    engine: Any = None,
    portfolio: Any = None,
    broker: Any = None,
    data_feed: Any = None,
    risk_manager: Any = None,
    patrol_interval: float = _DEFAULT_PATROL_INTERVAL,
    daily_report_cron: str = _DEFAULT_DAILY_REPORT_CRON,
    report_output_dir: Optional[Path] = None,
    enable_patrol: bool = True,
    enable_daily_report: bool = True,
    enable_t1_settle: bool = True,
    t1_settle_cron: str = _DEFAULT_T1_SETTLE_CRON,
    enable_reconcile: bool = True,
    reconcile_cron: str = _DEFAULT_RECONCILE_CRON,
    enable_resend_pending: bool = False,
    resend_pending_interval: float = 60.0,
) -> Dict[str, str]:
    """
    将实盘引擎一键挂载到调度器：自动注册巡检任务 + 收盘日报任务 + 断线补单对账任务。

    Parameters
    ----------
    scheduler : LiveScheduler
        目标调度器
    engine : LiveEngine | None
        实盘引擎
    portfolio / broker / data_feed / risk_manager : 各组件（巡检/日报数据源）
    patrol_interval : float
        巡检间隔（秒），默认 60
    daily_report_cron : str
        日报 cron 表达式，默认 "30 15 * * 1-5"（周一~周五 15:30）
    report_output_dir : Path | None
        日报输出目录
    enable_patrol : bool
        是否注册巡检任务
    enable_daily_report : bool
        是否注册日报任务
    enable_t1_settle : bool
        KP-P3-01：是否注册日终 T+1 日切结算任务（broker 为 BrokerManager 时
        调用 settle_t1_all()，为单券商时调用 settle_t1()；两者皆无则跳过）
    t1_settle_cron : str
        日终 T+1 结算 cron 表达式，默认 "30 15 * * 1-5"
    enable_reconcile : bool
        KP-P4-01：是否注册盘后账实对账任务（portfolio 与 broker 均存在时生效），
        对账差异超阈值时 critical 告警（禁止静默覆盖）
    reconcile_cron : str
        盘后账实对账 cron 表达式，默认 "50 15 * * 1-5"
    enable_resend_pending : bool
        P3-D11-001 整改：是否注册断线补单对账周期任务（默认关闭，保守；
        需 engine 非空且有 broker_manager 时生效），对账调用 engine.resend_pending_orders()
    resend_pending_interval : float
        断线补单对账间隔（秒），默认 60

    Returns
    -------
    Dict[str, str] : {"patrol": task_id, "daily_report": task_id, "t1_settle": task_id,
                      "reconcile": task_id, "resend_pending": task_id}
    """
    task_ids: Dict[str, str] = {}

    if enable_patrol:
        patrol = HealthPatrol()

        def _patrol_job(**kw) -> None:  # noqa: ARG001
            report = patrol.build_report(
                engine=engine,
                data_feed=data_feed,
                broker=broker,
                portfolio=portfolio,
                risk_manager=risk_manager,
            )
            logger.info(
                f"[attach] 巡检: status={report['status']}, "
                f"checks={len(report['checks'])}"
            )
            if report["status"] != PATROL_HEALTHY:
                logger.warning(f"[attach] 巡检非健康: {report['status']}")

            # KP-P5-03: 监控告警闭环——HealthChecker 巡检非 healthy 经告警管理器 alert
            try:
                from myquant.core.constants import SeverityLevel
                from myquant.monitoring.alerting import get_alert_manager
                from myquant.monitoring.health_check import get_health_checker
                hc = get_health_checker()
                hc.run_all()
                summary = hc.get_summary()
                if not summary.get("is_healthy", True):
                    level = (
                        SeverityLevel.CRITICAL
                        if summary.get("is_critical")
                        else SeverityLevel.WARNING
                    )
                    get_alert_manager().alert(
                        level=level,
                        message=(
                            "健康检查非健康: "
                            f"{summary.get('overall_status')} "
                            f"(degraded={summary.get('degraded_count')}, "
                            f"unhealthy={summary.get('unhealthy_count')})"
                        ),
                        source="live.scheduler",
                        category="health",
                        details=summary,
                        deduplication_key=f"health:{summary.get('overall_status')}",
                    )
            except Exception as e:
                logger.warning(f"[attach] 健康告警闭环异常: {e}")

        task_ids["patrol"] = scheduler.add_interval_task(
            _patrol_job,
            name="健康巡检",
            interval_seconds=patrol_interval,
        )

    if enable_daily_report:
        reporter = DailyReport(output_dir=report_output_dir)

        def _daily_job(**kw) -> None:  # noqa: ARG001
            path = reporter.build_and_save(
                engine=engine,
                portfolio=portfolio,
                broker=broker,
                data_feed=data_feed,
                risk_manager=risk_manager,
            )
            logger.info(f"[attach] 日报已生成: {path}")

        task_ids["daily_report"] = scheduler.add_cron_task(
            _daily_job,
            name="每日报告",
            cron_expr=daily_report_cron,
        )

    if enable_t1_settle and broker is not None:
        # KP-P3-01：日终 T+1 日切结算（日报后执行，释放当日买入冻结）。
        # broker 为 BrokerManager → settle_t1_all()（遍历各券商）；为单券商 → settle_t1()；
        # 两者皆无（券商无日切实现）→ 不注册该任务。
        if hasattr(broker, "settle_t1_all"):
            _settle = broker.settle_t1_all
        elif hasattr(broker, "settle_t1"):
            _settle = broker.settle_t1
        else:
            _settle = None
        if _settle is not None:
            def _t1_settle_job(**kw) -> None:  # noqa: ARG001
                result = _settle()
                logger.info(f"[attach] T+1日切结算完成: {result}")

            task_ids["t1_settle"] = scheduler.add_cron_task(
                _t1_settle_job,
                name="T+1日切结算",
                cron_expr=t1_settle_cron,
            )

    if enable_reconcile and portfolio is not None and broker is not None:
        # KP-P4-01：盘后账实对账（日报/T+1 结算后执行）。只对账不重建：
        # 调 portfolio.reconcile_vs_broker 产出差异，超阈值 critical 告警（禁止静默覆盖）。
        if hasattr(portfolio, "reconcile_vs_broker"):
            def _reconcile_job(**kw) -> None:  # noqa: ARG001
                diff = portfolio.reconcile_vs_broker(broker)
                if diff.exceeds_threshold:
                    logger.critical(f"[attach] 账实对账差异: {diff}")
                else:
                    logger.info(
                        f"[attach] 账实对账一致: 现金差={diff.cash_diff:.2f} "
                        f"持仓差异数={len(diff.position_diffs)}"
                    )

            task_ids["reconcile"] = scheduler.add_cron_task(
                _reconcile_job,
                name="账实对账",
                cron_expr=reconcile_cron,
            )

    if enable_resend_pending and engine is not None:
        # P3-D11-001 整改：断线补单对账自动接线（默认关闭，开启后按周期调用
        # engine.resend_pending_orders()；引擎无 broker_manager 时该方法安全返回 error，不抛异常）
        def _resend_job(**kw) -> None:  # noqa: ARG001
            result = engine.resend_pending_orders()
            if isinstance(result, dict) and result.get("error"):
                logger.warning(f"[attach] 断线补单对账跳过: {result['error']}")
            else:
                logger.info(f"[attach] 断线补单对账完成: {result}")

        task_ids["resend_pending"] = scheduler.add_interval_task(
            _resend_job,
            name="断线补单对账",
            interval_seconds=resend_pending_interval,
        )

    logger.info(
        f"[attach] 实盘引擎已挂载: patrol={task_ids.get('patrol')}, "
        f"daily_report={task_ids.get('daily_report')}, "
        f"t1_settle={task_ids.get('t1_settle')}, "
        f"reconcile={task_ids.get('reconcile')}, "
        f"resend_pending={task_ids.get('resend_pending')}"
    )
    return task_ids
